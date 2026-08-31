"""In-process foundation for restart-safe browser/profile/page ownership.

The broker deliberately grants only observation capabilities.  Existing
SubmissionGate, manifest, ledger, and receipt paths remain the sole sources of
submission authority.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from applypilot.apply.page_binding import PageBinding

FORBIDDEN_CAPABILITIES = frozenset(
    {"page_write", "submit", "apply_form_patch", "upload_artifact"}
)
OBSERVATION_CAPABILITIES = ("observe_form", "read_page_identity")


class BrowserBrokerError(RuntimeError):
    """Base class for fail-closed browser broker rejections."""


class BrowserLeaseConflict(BrowserBrokerError):
    """A live profile or page already has a different owner."""


class BrowserLeaseExpired(BrowserBrokerError):
    """A caller supplied an expired, released, or replaced lease."""


class StalePageBinding(BrowserBrokerError):
    """An optimistic page epoch no longer names the current page state."""


class BrowserAuthorityDenied(BrowserBrokerError):
    """A broker caller requested authority this layer cannot grant."""


class BrowserContinuityError(BrowserBrokerError):
    """A runtime/profile transition violated continuity constraints."""


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


@dataclass(frozen=True, slots=True)
class BrowserLease:
    """One exclusive, epoch-bound lease over a profile or logical page."""

    lease_id: str
    resource_kind: str
    resource_id: str
    owner_id: str
    scope_id: str
    attempt_id: str
    runtime_id: str
    epoch: int
    issued_at: float
    heartbeat_at: float
    expires_at: float
    capabilities: tuple[str, ...] = OBSERVATION_CAPABILITIES
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in (
            "lease_id",
            "resource_kind",
            "resource_id",
            "owner_id",
            "scope_id",
            "attempt_id",
            "runtime_id",
            "schema_version",
        ):
            _required(getattr(self, name), name)
        if self.resource_kind not in {"profile", "page"}:
            raise ValueError("resource_kind must be profile or page")
        if isinstance(self.epoch, bool) or self.epoch < 1:
            raise ValueError("epoch must be a positive integer")
        if self.expires_at <= self.heartbeat_at or self.heartbeat_at < self.issued_at:
            raise ValueError("lease timestamps are inconsistent")
        if FORBIDDEN_CAPABILITIES.intersection(self.capabilities):
            raise BrowserAuthorityDenied(
                "browser leases cannot grant page-write or submit authority"
            )

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["capabilities"] = list(self.capabilities)
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BrowserLease:
        raw_capabilities = value.get("capabilities") or OBSERVATION_CAPABILITIES
        if not isinstance(raw_capabilities, (list, tuple)):
            raise TypeError("lease capabilities must be an array")
        return cls(
            lease_id=str(value.get("lease_id") or ""),
            resource_kind=str(value.get("resource_kind") or ""),
            resource_id=str(value.get("resource_id") or ""),
            owner_id=str(value.get("owner_id") or ""),
            scope_id=str(value.get("scope_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            runtime_id=str(value.get("runtime_id") or ""),
            epoch=int(value.get("epoch") or 0),
            issued_at=float(value.get("issued_at") or 0),
            heartbeat_at=float(value.get("heartbeat_at") or 0),
            expires_at=float(value.get("expires_at") or 0),
            capabilities=tuple(str(item) for item in raw_capabilities),
            schema_version=str(value.get("schema_version") or "1"),
        )


@dataclass(frozen=True, slots=True)
class BrowserLeaseBundle:
    """Profile and page leases that must remain owned as one continuity unit."""

    profile: BrowserLease
    page: BrowserLease
    page_binding: PageBinding

    def __post_init__(self) -> None:
        if self.profile.resource_kind != "profile" or self.page.resource_kind != "page":
            raise BrowserBrokerError("browser lease bundle resource kinds are invalid")
        profile_identity = (
            self.profile.owner_id,
            self.profile.scope_id,
            self.profile.attempt_id,
            self.profile.runtime_id,
        )
        page_identity = (
            self.page.owner_id,
            self.page.scope_id,
            self.page.attempt_id,
            self.page.runtime_id,
        )
        if profile_identity != page_identity:
            raise BrowserBrokerError("profile and page leases have different owners")
        if (
            self.page_binding.page_id != self.page.resource_id
            or self.page_binding.page_lease_id != self.page.lease_id
            or self.page_binding.page_lease_epoch != self.page.epoch
            or self.page_binding.profile_lease_id != self.profile.lease_id
            or self.page_binding.owner_id != self.page.owner_id
            or self.page_binding.attempt_id != self.page.attempt_id
            or self.page_binding.runtime_id != self.page.runtime_id
        ):
            raise BrowserBrokerError("page binding does not match its lease bundle")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1",
            "profile": self.profile.as_dict(),
            "page": self.page.as_dict(),
            "page_binding": self.page_binding.as_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BrowserLeaseBundle:
        raw_profile = value.get("profile")
        raw_page = value.get("page")
        raw_binding = value.get("page_binding")
        if not all(isinstance(item, Mapping) for item in (raw_profile, raw_page, raw_binding)):
            raise TypeError("browser lease bundle is incomplete")
        return cls(
            profile=BrowserLease.from_mapping(raw_profile),
            page=BrowserLease.from_mapping(raw_page),
            page_binding=PageBinding.from_mapping(raw_binding),
        )


class BrowserBroker:
    """Exclusive profile/page lease registry with optimistic page epochs."""

    def __init__(
        self,
        *,
        default_ttl_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        self._default_ttl_seconds = float(default_ttl_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._active: dict[tuple[str, str], BrowserLease] = {}
        self._epochs: dict[tuple[str, str], int] = {}
        self._page_epochs: dict[str, int] = {}

    def _current(self, lease: BrowserLease, *, now: float) -> BrowserLease:
        current = self._active.get((lease.resource_kind, lease.resource_id))
        if (
            current is None
            or current.lease_id != lease.lease_id
            or current.epoch != lease.epoch
        ):
            raise BrowserLeaseExpired("lease was released or replaced")
        if now >= current.expires_at:
            del self._active[(current.resource_kind, current.resource_id)]
            raise BrowserLeaseExpired("lease expired before use")
        return current

    def validate(self, lease: BrowserLease) -> BrowserLease:
        with self._lock:
            return self._current(lease, now=self._clock())

    def _acquire(
        self,
        resource_kind: str,
        resource_id: str,
        *,
        owner_id: str,
        scope_id: str,
        attempt_id: str,
        runtime_id: str,
        ttl_seconds: float | None,
    ) -> BrowserLease:
        for value, name in (
            (resource_id, "resource_id"),
            (owner_id, "owner_id"),
            (scope_id, "scope_id"),
            (attempt_id, "attempt_id"),
            (runtime_id, "runtime_id"),
        ):
            _required(value, name)
        ttl = self._default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = self._clock()
        key = (resource_kind, resource_id)
        current = self._active.get(key)
        if current is not None and now >= current.expires_at:
            del self._active[key]
            current = None
        if current is not None:
            same_owner = (
                current.owner_id == owner_id
                and current.scope_id == scope_id
                and current.attempt_id == attempt_id
                and current.runtime_id == runtime_id
            )
            if not same_owner:
                raise BrowserLeaseConflict(
                    f"{resource_kind} already has a live single writer"
                )
            renewed = BrowserLease(
                lease_id=current.lease_id,
                resource_kind=current.resource_kind,
                resource_id=current.resource_id,
                owner_id=current.owner_id,
                scope_id=current.scope_id,
                attempt_id=current.attempt_id,
                runtime_id=current.runtime_id,
                epoch=current.epoch,
                issued_at=current.issued_at,
                heartbeat_at=now,
                expires_at=now + ttl,
                capabilities=current.capabilities,
            )
            self._active[key] = renewed
            return renewed
        epoch = self._epochs.get(key, 0) + 1
        self._epochs[key] = epoch
        lease = BrowserLease(
            lease_id=f"{resource_kind}-lease-{uuid.uuid4()}",
            resource_kind=resource_kind,
            resource_id=resource_id,
            owner_id=owner_id,
            scope_id=scope_id,
            attempt_id=attempt_id,
            runtime_id=runtime_id,
            epoch=epoch,
            issued_at=now,
            heartbeat_at=now,
            expires_at=now + ttl,
        )
        self._active[key] = lease
        return lease

    def acquire_bundle(
        self,
        *,
        profile_id: str,
        page_id: str,
        owner_id: str,
        scope_id: str,
        attempt_id: str,
        runtime_id: str,
        ttl_seconds: float | None = None,
    ) -> BrowserLeaseBundle:
        """Acquire profile then page atomically for one single writer."""
        with self._lock:
            profile_key = ("profile", profile_id)
            previous_profile = self._active.get(profile_key)
            profile = self._acquire(
                "profile",
                profile_id,
                owner_id=owner_id,
                scope_id=scope_id,
                attempt_id=attempt_id,
                runtime_id=runtime_id,
                ttl_seconds=ttl_seconds,
            )
            try:
                page = self._acquire(
                    "page",
                    page_id,
                    owner_id=owner_id,
                    scope_id=scope_id,
                    attempt_id=attempt_id,
                    runtime_id=runtime_id,
                    ttl_seconds=ttl_seconds,
                )
            except Exception:
                if previous_profile is None:
                    current = self._active.get(profile_key)
                    if current is not None and current.lease_id == profile.lease_id:
                        del self._active[profile_key]
                else:
                    self._active[profile_key] = previous_profile
                raise
            binding = self._page_binding(page, profile)
            return BrowserLeaseBundle(profile=profile, page=page, page_binding=binding)

    def _page_binding(
        self, page: BrowserLease, profile: BrowserLease
    ) -> PageBinding:
        return PageBinding(
            page_id=page.resource_id,
            page_lease_id=page.lease_id,
            page_lease_epoch=page.epoch,
            page_epoch=self._page_epochs.get(page.resource_id, 0),
            profile_lease_id=profile.lease_id,
            owner_id=page.owner_id,
            attempt_id=page.attempt_id,
            runtime_id=page.runtime_id,
        )

    def heartbeat(
        self,
        bundle: BrowserLeaseBundle,
        *,
        ttl_seconds: float | None = None,
    ) -> BrowserLeaseBundle:
        """Renew both leases only if the entire page token is still current."""
        with self._lock:
            self.validate(bundle.profile)
            self.validate_page(bundle.page_binding)
            profile = self._acquire(
                "profile",
                bundle.profile.resource_id,
                owner_id=bundle.profile.owner_id,
                scope_id=bundle.profile.scope_id,
                attempt_id=bundle.profile.attempt_id,
                runtime_id=bundle.profile.runtime_id,
                ttl_seconds=ttl_seconds,
            )
            page = self._acquire(
                "page",
                bundle.page.resource_id,
                owner_id=bundle.page.owner_id,
                scope_id=bundle.page.scope_id,
                attempt_id=bundle.page.attempt_id,
                runtime_id=bundle.page.runtime_id,
                ttl_seconds=ttl_seconds,
            )
            return BrowserLeaseBundle(
                profile=profile,
                page=page,
                page_binding=self._page_binding(page, profile),
            )

    def validate_page(self, binding: PageBinding) -> PageBinding:
        with self._lock:
            current = self._active.get(("page", binding.page_id))
            if current is None:
                raise BrowserLeaseExpired("page lease is not active")
            self._current(current, now=self._clock())
            if (
                current.lease_id != binding.page_lease_id
                or current.epoch != binding.page_lease_epoch
                or current.owner_id != binding.owner_id
                or current.attempt_id != binding.attempt_id
                or current.runtime_id != binding.runtime_id
            ):
                raise BrowserLeaseExpired("page lease identity changed")
            page_epoch = self._page_epochs.get(binding.page_id, 0)
            if binding.page_epoch != page_epoch:
                raise StalePageBinding(
                    f"stale page epoch {binding.page_epoch}; current epoch is {page_epoch}"
                )
            return binding

    def advance_page(
        self,
        bundle: BrowserLeaseBundle,
        *,
        expected_page_epoch: int,
    ) -> BrowserLeaseBundle:
        """Record an externally observed page change using optimistic CAS."""
        with self._lock:
            self.validate_page(bundle.page_binding)
            current_epoch = self._page_epochs.get(bundle.page.resource_id, 0)
            if current_epoch != expected_page_epoch:
                raise StalePageBinding(
                    f"stale page epoch {expected_page_epoch}; current epoch is {current_epoch}"
                )
            self._page_epochs[bundle.page.resource_id] = current_epoch + 1
            return BrowserLeaseBundle(
                profile=self.validate(bundle.profile),
                page=self.validate(bundle.page),
                page_binding=self._page_binding(bundle.page, bundle.profile),
            )

    def require_operation(
        self,
        binding: PageBinding,
        operation: str,
    ) -> PageBinding:
        """Validate observation operations and reject all write/submit requests."""
        if operation not in OBSERVATION_CAPABILITIES:
            raise BrowserAuthorityDenied(
                f"BrowserBroker does not grant operation authority: {operation}"
            )
        return self.validate_page(binding)

    def continue_bundle(
        self,
        previous: BrowserLeaseBundle,
        *,
        profile_id: str,
        page_id: str,
        owner_id: str,
        scope_id: str,
        attempt_id: str,
        runtime_id: str,
        submit_started: bool,
        resume_existing_page: bool,
        ttl_seconds: float | None = None,
    ) -> BrowserLeaseBundle:
        """Continue or safely replace a pre-submit runtime/profile lease."""
        same_identity = (
            previous.profile.resource_id == profile_id
            and previous.page.resource_id == page_id
            and previous.profile.owner_id == owner_id
            and previous.profile.scope_id == scope_id
            and previous.profile.attempt_id == attempt_id
            and previous.profile.runtime_id == runtime_id
        )
        try:
            self.validate_page(previous.page_binding)
            self.validate(previous.profile)
        except StalePageBinding:
            raise
        except BrowserLeaseExpired:
            if submit_started or resume_existing_page:
                raise
            same_identity = False
        if same_identity:
            return self.heartbeat(previous, ttl_seconds=ttl_seconds)
        if submit_started:
            raise BrowserContinuityError(
                "runtime/profile switch is forbidden after submit_started"
            )
        if resume_existing_page:
            raise BrowserContinuityError(
                "runtime/profile switch cannot resume an existing page"
            )
        self.release_scope(scope_id)
        return self.acquire_bundle(
            profile_id=profile_id,
            page_id=page_id,
            owner_id=owner_id,
            scope_id=scope_id,
            attempt_id=attempt_id,
            runtime_id=runtime_id,
            ttl_seconds=ttl_seconds,
        )

    def release_scope(self, scope_id: str) -> None:
        with self._lock:
            for key, lease in tuple(self._active.items()):
                if lease.scope_id == scope_id:
                    del self._active[key]

    def close(self) -> None:
        with self._lock:
            self._active.clear()


class LeaseHeartbeat:
    """Daemon heartbeat that can cancel an owned runtime on lease failure."""

    def __init__(
        self,
        broker: BrowserBroker,
        bundle: BrowserLeaseBundle,
        *,
        interval_seconds: float = 30.0,
        on_failure: Callable[[BrowserBrokerError], None] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._broker = broker
        self._bundle = bundle
        self._interval_seconds = interval_seconds
        self._on_failure = on_failure
        self._stop = threading.Event()
        self._failure: BrowserBrokerError | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def bundle(self) -> BrowserLeaseBundle:
        return self._bundle

    def start(self) -> LeaseHeartbeat:
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._bundle = self._broker.heartbeat(self._bundle)
            except BrowserBrokerError as exc:
                self._failure = exc
                if self._on_failure is not None:
                    self._on_failure(exc)
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=min(1.0, self._interval_seconds))

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure
