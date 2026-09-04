"""Single-owner mutation boundary for job-carried browser authority.

``_browser_lease_binding`` remains the only mutable browser-authority fact on a
production job mapping.  This module owns every mutation of that fact and binds
the durable Broker token to the physical browser generation and application
session that are otherwise process-local.

The serialized shape is backwards-readable by ``BrowserLeaseBundle``.  Legacy
version-1 bundle mappings are accepted and upgraded on their next mutation so
an interrupted turn can be reconstructed after a launcher restart.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

from applypilot.apply.browser_broker import (
    BrowserContinuityError,
    BrowserLeaseBundle,
    StalePageBinding,
)
from applypilot.apply.contracts import application_actor_id

BROWSER_LEASE_BINDING_KEY = "_browser_lease_binding"
_BINDING_SCHEMA_VERSION = "2"
_MUTATION_LOCK = threading.RLock()


class BrowserAuthorityBroker(Protocol):
    """Broker operations required by ``BrowserAuthorityHandle``."""

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
    ) -> BrowserLeaseBundle: ...

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
    ) -> BrowserLeaseBundle: ...

    def heartbeat(
        self,
        bundle: BrowserLeaseBundle,
        *,
        ttl_seconds: float | None = None,
    ) -> BrowserLeaseBundle: ...

    def advance_page(
        self,
        bundle: BrowserLeaseBundle,
        *,
        expected_page_epoch: int,
    ) -> BrowserLeaseBundle: ...

    def release_scope(
        self,
        scope_id: str,
        *,
        owner_id: str | None = None,
        attempt_id: str | None = None,
        runtime_id: str | None = None,
        expected_bundles: tuple[BrowserLeaseBundle, ...] | None = None,
    ) -> None: ...


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("browser_generation must be an integer")
    if value < 1:
        raise ValueError("browser_generation must be positive")
    return value


@dataclass(frozen=True, slots=True)
class BrowserAuthorityIdentity:
    """Physical/application identity that must travel with one lease bundle."""

    browser_generation: int
    application_session_id: str
    actor_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        _generation(self.browser_generation)
        for name in ("application_session_id", "actor_id", "attempt_id"):
            _required(getattr(self, name), name)
        if self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("browser authority actor/attempt identity is not canonical")


def _legacy_identity(
    job: Mapping[str, object], bundle: BrowserLeaseBundle
) -> BrowserAuthorityIdentity:
    """Derive the minimum truthful identity for a legacy version-1 mapping."""

    raw_context = job.get("_application_context_bundle")
    context = raw_context if isinstance(raw_context, Mapping) else {}
    session_id = str(
        context.get("application_session_id")
        or job.get("_application_session_id")
        or f"legacy:{bundle.profile.attempt_id}"
    ).strip()
    generation = _generation(context.get("browser_generation", 1))
    return BrowserAuthorityIdentity(
        browser_generation=generation,
        application_session_id=session_id,
        actor_id=bundle.profile.owner_id,
        attempt_id=bundle.profile.attempt_id,
    )


def _serialized_identity(
    job: Mapping[str, object],
    raw: Mapping[str, object],
    bundle: BrowserLeaseBundle,
) -> BrowserAuthorityIdentity:
    names = (
        "browser_generation",
        "application_session_id",
        "actor_id",
        "attempt_id",
    )
    present = tuple(name in raw for name in names)
    if not any(present):
        return _legacy_identity(job, bundle)
    if not all(present):
        raise BrowserContinuityError("browser authority identity is incomplete")
    return BrowserAuthorityIdentity(
        browser_generation=_generation(raw["browser_generation"]),
        application_session_id=_required(
            raw["application_session_id"], "application_session_id"
        ),
        actor_id=_required(raw["actor_id"], "actor_id"),
        attempt_id=_required(raw["attempt_id"], "attempt_id"),
    )


def _fixed_bundle_identity(bundle: BrowserLeaseBundle) -> tuple[object, ...]:
    profile = bundle.profile
    page = bundle.page
    binding = bundle.page_binding
    return (
        profile.lease_id,
        profile.resource_kind,
        profile.resource_id,
        profile.owner_id,
        profile.scope_id,
        profile.attempt_id,
        profile.runtime_id,
        profile.epoch,
        profile.capabilities,
        profile.schema_version,
        page.lease_id,
        page.resource_kind,
        page.resource_id,
        page.owner_id,
        page.scope_id,
        page.attempt_id,
        page.runtime_id,
        page.epoch,
        page.capabilities,
        page.schema_version,
        binding.page_id,
        binding.page_lease_id,
        binding.page_lease_epoch,
        binding.profile_lease_id,
        binding.owner_id,
        binding.attempt_id,
        binding.runtime_id,
    )


class BrowserAuthorityHandle:
    """CAS-like owner for one job's serialized browser authority binding."""

    def __init__(
        self,
        job: MutableMapping[str, object],
        *,
        identity: BrowserAuthorityIdentity,
        broker: BrowserAuthorityBroker | None = None,
        bundle: BrowserLeaseBundle | None = None,
        snapshot: Mapping[str, object] | None = None,
    ) -> None:
        self._job = job
        self.identity = identity
        self._broker = broker
        self._bundle = bundle
        self._snapshot = deepcopy(dict(snapshot)) if snapshot is not None else None
        job_attempt_id = str(job.get("_attempt_id") or "").strip()
        if job_attempt_id and job_attempt_id != identity.attempt_id:
            raise BrowserContinuityError(
                "job attempt does not match browser authority"
            )
        if bundle is not None:
            self._validate_bundle(bundle)

    @classmethod
    def create(
        cls,
        job: MutableMapping[str, object],
        *,
        browser_generation: int,
        application_session_id: str,
        actor_id: str,
        attempt_id: str,
        broker: BrowserAuthorityBroker | None = None,
    ) -> BrowserAuthorityHandle:
        """Create or resume a handle with caller-proven process/session identity."""

        identity = BrowserAuthorityIdentity(
            browser_generation=browser_generation,
            application_session_id=application_session_id,
            actor_id=actor_id,
            attempt_id=attempt_id,
        )
        raw = job.get(BROWSER_LEASE_BINDING_KEY)
        if raw is None:
            return cls(job, identity=identity, broker=broker)
        if not isinstance(raw, Mapping):
            raise TypeError("browser lease binding must be a mapping")
        bundle = BrowserLeaseBundle.from_mapping(raw)
        recorded = _serialized_identity(job, raw, bundle)
        if recorded != identity:
            raise BrowserContinuityError(
                "browser generation, application session, actor, or attempt changed"
            )
        return cls(
            job,
            identity=identity,
            broker=broker,
            bundle=bundle,
            snapshot=raw,
        )

    @classmethod
    def rebuild(
        cls,
        job: MutableMapping[str, object],
        *,
        broker: BrowserAuthorityBroker | None = None,
    ) -> BrowserAuthorityHandle:
        """Rebuild a handle from a serialized v2 or legacy v1 binding."""

        raw = job.get(BROWSER_LEASE_BINDING_KEY)
        if not isinstance(raw, Mapping):
            raise BrowserContinuityError("job has no browser lease binding")
        bundle = BrowserLeaseBundle.from_mapping(raw)
        identity = _serialized_identity(job, raw, bundle)
        return cls(
            job,
            identity=identity,
            broker=broker,
            bundle=bundle,
            snapshot=raw,
        )

    @property
    def bundle(self) -> BrowserLeaseBundle:
        if self._bundle is None:
            raise BrowserContinuityError("browser authority is not installed")
        return self._bundle

    def _require_broker(self) -> BrowserAuthorityBroker:
        if self._broker is None:
            raise BrowserContinuityError("browser authority broker is unavailable")
        return self._broker

    def _validate_bundle(self, bundle: BrowserLeaseBundle) -> None:
        identity = self.identity
        actual = (
            bundle.profile.owner_id,
            bundle.profile.attempt_id,
            bundle.page.owner_id,
            bundle.page.attempt_id,
            bundle.page_binding.owner_id,
            bundle.page_binding.attempt_id,
        )
        expected = (
            identity.actor_id,
            identity.attempt_id,
            identity.actor_id,
            identity.attempt_id,
            identity.actor_id,
            identity.attempt_id,
        )
        if actual != expected:
            raise BrowserContinuityError(
                "browser bundle crossed its actor or attempt authority"
            )

    def _assert_snapshot_current(self) -> None:
        raw = self._job.get(BROWSER_LEASE_BINDING_KEY)
        if self._snapshot is None:
            if raw is not None:
                raise StalePageBinding("browser authority was installed by another handle")
            return
        if not isinstance(raw, Mapping) or dict(raw) != self._snapshot:
            raise StalePageBinding("browser authority handle is stale")

    def _record(self, bundle: BrowserLeaseBundle) -> dict[str, object]:
        record = bundle.as_dict()
        record.update(
            {
                "schema_version": _BINDING_SCHEMA_VERSION,
                "browser_generation": self.identity.browser_generation,
                "application_session_id": self.identity.application_session_id,
                "actor_id": self.identity.actor_id,
                "attempt_id": self.identity.attempt_id,
            }
        )
        return record

    def _install(
        self,
        bundle: BrowserLeaseBundle,
        *,
        allow_replacement: bool = False,
    ) -> BrowserLeaseBundle:
        """Install a Broker result if this handle still owns the job snapshot."""

        self._validate_bundle(bundle)
        with _MUTATION_LOCK:
            self._assert_snapshot_current()
            if self._bundle is not None and not allow_replacement:
                if _fixed_bundle_identity(bundle) != _fixed_bundle_identity(self._bundle):
                    raise BrowserContinuityError(
                        "browser lease identity changed outside Broker continuation"
                    )
                if bundle.page_binding.page_epoch < self._bundle.page_binding.page_epoch:
                    raise StalePageBinding("browser page epoch moved backwards")
            record = self._record(bundle)
            self._job[BROWSER_LEASE_BINDING_KEY] = record
            self._snapshot = deepcopy(record)
            self._bundle = bundle
        return bundle

    def install(self, bundle: BrowserLeaseBundle) -> BrowserLeaseBundle:
        """Install a same-lease Broker result such as heartbeat or page CAS."""

        return self._install(bundle)

    def acquire_or_continue(
        self,
        *,
        profile_id: str,
        page_id: str,
        scope_id: str,
        runtime_id: str,
        submit_started: bool,
        resume_existing_page: bool,
        ttl_seconds: float | None = None,
    ) -> BrowserLeaseBundle:
        """Acquire/continue through the Broker and publish the exact result."""

        broker = self._require_broker()
        with _MUTATION_LOCK:
            self._assert_snapshot_current()
            if self._bundle is None:
                bundle = broker.acquire_bundle(
                    profile_id=profile_id,
                    page_id=page_id,
                    owner_id=self.identity.actor_id,
                    scope_id=scope_id,
                    attempt_id=self.identity.attempt_id,
                    runtime_id=runtime_id,
                    ttl_seconds=ttl_seconds,
                )
            else:
                bundle = broker.continue_bundle(
                    self._bundle,
                    profile_id=profile_id,
                    page_id=page_id,
                    owner_id=self.identity.actor_id,
                    scope_id=scope_id,
                    attempt_id=self.identity.attempt_id,
                    runtime_id=runtime_id,
                    submit_started=submit_started,
                    resume_existing_page=resume_existing_page,
                    ttl_seconds=ttl_seconds,
                )
            return self._install(bundle, allow_replacement=True)

    def reacquire_current(
        self, *, ttl_seconds: float | None = None
    ) -> BrowserLeaseBundle:
        """Reconstruct a durable Broker instance from the serialized exact identity."""

        current = self.bundle
        broker = self._require_broker()
        with _MUTATION_LOCK:
            self._assert_snapshot_current()
            bundle = broker.acquire_bundle(
                profile_id=current.profile.resource_id,
                page_id=current.page.resource_id,
                owner_id=self.identity.actor_id,
                scope_id=current.profile.scope_id,
                attempt_id=self.identity.attempt_id,
                runtime_id=current.profile.runtime_id,
                ttl_seconds=ttl_seconds,
            )
            # The durable Broker may return a new lease token only after the
            # prior process-bound lease has expired.  Its acquire CAS is the
            # authority for this crash/process-restart replacement.
            return self._install(bundle, allow_replacement=True)

    def heartbeat(
        self, *, ttl_seconds: float | None = None
    ) -> BrowserLeaseBundle:
        broker = self._require_broker()
        with _MUTATION_LOCK:
            self._assert_snapshot_current()
            return self.install(
                broker.heartbeat(self.bundle, ttl_seconds=ttl_seconds)
            )

    def advance_page(self, *, expected_page_epoch: int) -> BrowserLeaseBundle:
        broker = self._require_broker()
        with _MUTATION_LOCK:
            self._assert_snapshot_current()
            return self.install(
                broker.advance_page(
                    self.bundle,
                    expected_page_epoch=expected_page_epoch,
                )
            )

    def adopt(self, candidate: BrowserLeaseBundle) -> BrowserLeaseBundle:
        """Adopt a child repair result without accepting cross-talk or stale state."""

        self._validate_bundle(candidate)
        current = self.bundle
        if _fixed_bundle_identity(candidate) != _fixed_bundle_identity(current):
            raise BrowserContinuityError("repair browser lease identity changed")
        if candidate.page_binding.page_epoch < current.page_binding.page_epoch:
            raise StalePageBinding("repair browser page epoch is stale")
        return self.install(candidate)

    def adopt_from_job(self, candidate_job: Mapping[str, object]) -> BrowserLeaseBundle:
        raw = candidate_job.get(BROWSER_LEASE_BINDING_KEY)
        if not isinstance(raw, Mapping):
            raise BrowserContinuityError("repair job has no browser lease binding")
        candidate = BrowserLeaseBundle.from_mapping(raw)
        recorded = _serialized_identity(candidate_job, raw, candidate)
        if recorded != self.identity:
            raise BrowserContinuityError(
                "repair browser generation, session, actor, or attempt changed"
            )
        return self.adopt(candidate)

    def release(self) -> None:
        """Release only the exact Broker bundle and then remove the owned mapping."""

        broker = self._require_broker()
        current = self.bundle
        with _MUTATION_LOCK:
            self._assert_snapshot_current()
            broker.release_scope(
                current.profile.scope_id,
                owner_id=self.identity.actor_id,
                attempt_id=self.identity.attempt_id,
                runtime_id=current.profile.runtime_id,
                expected_bundles=(current,),
            )
            self._assert_snapshot_current()
            self._job.pop(BROWSER_LEASE_BINDING_KEY, None)
            self._snapshot = None
            self._bundle = None


def adopt_browser_authority(
    job: MutableMapping[str, object], candidate_job: Mapping[str, object]
) -> BrowserLeaseBundle:
    """Production repair-adoption entry point without granting Broker operations."""

    return BrowserAuthorityHandle.rebuild(job).adopt_from_job(candidate_job)


__all__ = [
    "BROWSER_LEASE_BINDING_KEY",
    "BrowserAuthorityBroker",
    "BrowserAuthorityHandle",
    "BrowserAuthorityIdentity",
    "adopt_browser_authority",
]
