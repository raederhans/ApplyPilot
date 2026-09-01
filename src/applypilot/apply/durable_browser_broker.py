"""SQLite-backed BrowserBroker facade with exact process-bound CAS semantics."""

from __future__ import annotations

import math
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from applypilot.apply.browser_broker import (
    OBSERVATION_CAPABILITIES,
    BrowserAuthorityDenied,
    BrowserBrokerError,
    BrowserContinuityError,
    BrowserLease,
    BrowserLeaseBundle,
    BrowserLeaseConflict,
    BrowserLeaseExpired,
    StalePageBinding,
)
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.page_binding import PageBinding
from applypilot.storage import runtime_control

ConnectionProvider = Callable[[], sqlite3.Connection]
ProcessIdentityProvider = Callable[[], tuple[int, int]]


class DurableBrowserBroker:
    """Preserve BrowserBroker's observation-only API over one durable lease row.

    Connections are borrowed by default.  Set ``close_connections=True`` only
    when the provider is a factory returning a fresh broker-owned connection on
    every call.  Heartbeat use requires the provider to return a connection
    valid for the calling thread; this facade never transfers one across threads.
    """

    def __init__(
        self,
        connection_provider: ConnectionProvider,
        *,
        default_ttl_seconds: float = 120.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lease_id_factory: Callable[[], str] = lambda: f"browser-lease-{uuid.uuid4()}",
        process_identity_provider: ProcessIdentityProvider,
        close_connections: bool = False,
    ) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        self._connection_provider = connection_provider
        self._default_ttl_seconds = float(default_ttl_seconds)
        self._clock = clock
        self._lease_id_factory = lease_id_factory
        self._process_identity_provider = process_identity_provider
        self._close_connections = bool(close_connections)
        self._known: dict[str, BrowserLeaseBundle] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise BrowserLeaseExpired("durable broker is closed")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("durable broker clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _ttl(self, ttl_seconds: float | None) -> int:
        value = self._default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        if value <= 0:
            raise ValueError("ttl_seconds must be positive")
        return max(1, math.ceil(value))

    def _process_identity(self) -> tuple[int, int]:
        process_id, birth = self._process_identity_provider()
        if (
            not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or not isinstance(birth, int)
            or isinstance(birth, bool)
        ):
            raise TypeError("process identity values must be integers")
        if process_id < 1 or birth < 1:
            raise ValueError("process identity values must be positive integers")
        return process_id, birth

    def _connection(self) -> sqlite3.Connection:
        connection = self._connection_provider()
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection_provider must return sqlite3.Connection")
        return connection

    def _release_connection(self, connection: sqlite3.Connection) -> None:
        """Close only factory-owned connections; borrowed connections stay caller-owned."""
        if self._close_connections:
            connection.close()

    @staticmethod
    def _translate(error: Exception, *, page: bool = False) -> BrowserBrokerError:
        if isinstance(error, runtime_control.ResourceLeaseConflictError):
            return BrowserLeaseConflict(str(error))
        if isinstance(error, runtime_control.ResourceLeaseExpiredError):
            return BrowserLeaseExpired(str(error))
        if isinstance(error, runtime_control.StaleResourceLeaseError):
            if page:
                return StalePageBinding(str(error))
            return BrowserLeaseExpired(str(error))
        raise error

    @staticmethod
    def _bundle(row: runtime_control.BrowserResourceLease) -> BrowserLeaseBundle:
        issued = datetime.fromisoformat(row.created_at).timestamp()
        heartbeat = datetime.fromisoformat(row.heartbeat_at).timestamp()
        expires = datetime.fromisoformat(row.expires_at).timestamp()
        common = {
            "lease_id": row.lease_id,
            "owner_id": row.owner_id,
            "scope_id": row.scope_id,
            "attempt_id": row.attempt_id,
            "runtime_id": row.runtime_id,
            "epoch": row.lease_epoch,
            "issued_at": issued,
            "heartbeat_at": heartbeat,
            "expires_at": expires,
        }
        profile = BrowserLease(
            resource_kind="profile",
            resource_id=row.profile_id,
            **common,
        )
        page = BrowserLease(
            resource_kind="page",
            resource_id=str(row.page_target_id),
            **common,
        )
        binding = PageBinding(
            page_id=page.resource_id,
            page_lease_id=row.lease_id,
            page_lease_epoch=row.lease_epoch,
            page_epoch=row.page_epoch,
            profile_lease_id=row.lease_id,
            owner_id=row.owner_id,
            attempt_id=row.attempt_id,
            runtime_id=row.runtime_id,
        )
        return BrowserLeaseBundle(profile=profile, page=page, page_binding=binding)

    def _remember(self, row: runtime_control.BrowserResourceLease) -> BrowserLeaseBundle:
        bundle = self._bundle(row)
        self._known[row.lease_id] = bundle
        return bundle

    @staticmethod
    def _same_lease_token(candidate: BrowserLease, current: BrowserLease) -> bool:
        """Compare immutable identity/CAS fields, not renewable wall-clock metadata."""
        return (
            candidate.lease_id,
            candidate.resource_kind,
            candidate.resource_id,
            candidate.owner_id,
            candidate.scope_id,
            candidate.attempt_id,
            candidate.runtime_id,
            candidate.epoch,
            candidate.capabilities,
            candidate.schema_version,
        ) == (
            current.lease_id,
            current.resource_kind,
            current.resource_id,
            current.owner_id,
            current.scope_id,
            current.attempt_id,
            current.runtime_id,
            current.epoch,
            current.capabilities,
            current.schema_version,
        )

    def _cas(
        self,
        bundle: BrowserLeaseBundle,
        process_id: int,
        process_birth_time: int,
    ) -> dict[str, object]:
        return {
            "lease_id": bundle.profile.lease_id,
            "owner_id": bundle.profile.owner_id,
            "expected_actor_id": application_actor_id(bundle.profile.attempt_id),
            "expected_attempt_id": bundle.profile.attempt_id,
            "expected_runtime_id": bundle.profile.runtime_id,
            "expected_resource_kind": "browser-profile-page",
            "expected_scope_id": bundle.profile.scope_id,
            "expected_profile_id": bundle.profile.resource_id,
            "expected_page_target_id": bundle.page.resource_id,
            "expected_lease_epoch": bundle.profile.epoch,
            "expected_page_epoch": bundle.page_binding.page_epoch,
            "expected_process_id": process_id,
            "expected_process_birth_time": process_birth_time,
        }

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
        with self._lock:
            self._ensure_open()
            now = self._now()
            ttl = self._ttl(ttl_seconds)
            process_id, birth = self._process_identity()
            actor_id = application_actor_id(attempt_id)
            connection = self._connection()
            try:
                active, latest = runtime_control.inspect_browser_resource_lease_state(
                    connection,
                    profile_id=profile_id,
                    page_target_id=page_id,
                    now=now,
                )
                if active is not None:
                    exact = (
                        active.resource_kind == "browser-profile-page"
                        and active.scope_id == scope_id
                        and active.profile_id == profile_id
                        and active.page_target_id == page_id
                        and active.owner_id == owner_id
                        and active.actor_id == actor_id
                        and active.attempt_id == attempt_id
                        and active.runtime_id == runtime_id
                        and active.process_id == process_id
                        and active.process_birth_time == birth
                    )
                    if not exact:
                        raise BrowserLeaseConflict("profile or page has a different live owner")
                    row = runtime_control.heartbeat_browser_resource_lease(
                        connection,
                        lease_id=active.lease_id,
                        owner_id=owner_id,
                        expected_actor_id=actor_id,
                        expected_attempt_id=attempt_id,
                        expected_runtime_id=runtime_id,
                        expected_resource_kind="browser-profile-page",
                        expected_scope_id=scope_id,
                        expected_profile_id=profile_id,
                        expected_page_target_id=page_id,
                        expected_lease_epoch=active.lease_epoch,
                        expected_page_epoch=active.page_epoch,
                        expected_process_id=process_id,
                        expected_process_birth_time=birth,
                        lease_seconds=ttl,
                        now=now,
                    )
                else:
                    lease_id = self._lease_id_factory()
                    expected_epoch = latest
                    page_epoch = 0
                    row = runtime_control.acquire_browser_resource_lease(
                        connection,
                        lease_id=lease_id,
                        resource_kind="browser-profile-page",
                        scope_id=scope_id,
                        profile_id=profile_id,
                        page_target_id=page_id,
                        owner_id=owner_id,
                        actor_id=actor_id,
                        attempt_id=attempt_id,
                        runtime_id=runtime_id,
                        expected_lease_epoch=expected_epoch,
                        page_epoch=page_epoch,
                        lease_seconds=ttl,
                        process_id=process_id,
                        process_birth_time=birth,
                        now=now,
                    )
            except runtime_control.StaleResourceLeaseError as error:
                raise BrowserLeaseConflict("lease acquisition lost its epoch CAS") from error
            except (
                runtime_control.ResourceLeaseConflictError,
                runtime_control.ResourceLeaseExpiredError,
            ) as error:
                raise self._translate(error) from error
            finally:
                self._release_connection(connection)
            return self._remember(row)

    def _known_bundle(self, lease_id: str) -> BrowserLeaseBundle:
        bundle = self._known.get(lease_id)
        if bundle is None:
            raise BrowserLeaseExpired("lease is not known to this broker instance")
        return bundle

    def validate(self, lease: BrowserLease) -> BrowserLease:
        with self._lock:
            self._ensure_open()
            bundle = self._known_bundle(lease.lease_id)
            expected = bundle.profile if lease.resource_kind == "profile" else bundle.page
            if not self._same_lease_token(lease, expected):
                raise BrowserLeaseExpired("lease identity changed")
            process_id, birth = self._process_identity()
            connection = self._connection()
            try:
                row = runtime_control.validate_browser_resource_lease(
                    connection,
                    **self._cas(bundle, process_id, birth),
                    now=self._now(),
                )
            except (
                runtime_control.ResourceLeaseConflictError,
                runtime_control.ResourceLeaseExpiredError,
                runtime_control.StaleResourceLeaseError,
            ) as error:
                raise self._translate(error) from error
            finally:
                self._release_connection(connection)
            current = self._remember(row)
            return current.profile if lease.resource_kind == "profile" else current.page

    def validate_page(self, binding: PageBinding) -> PageBinding:
        with self._lock:
            self._ensure_open()
            bundle = self._known_bundle(binding.page_lease_id)
            if binding != bundle.page_binding:
                if (
                    binding.page_lease_id == bundle.page_binding.page_lease_id
                    and binding.page_lease_epoch == bundle.page_binding.page_lease_epoch
                ):
                    raise StalePageBinding("stale page epoch or binding")
                raise BrowserLeaseExpired("page lease identity changed")
            process_id, birth = self._process_identity()
            connection = self._connection()
            try:
                row = runtime_control.validate_browser_resource_lease(
                    connection,
                    **self._cas(bundle, process_id, birth),
                    now=self._now(),
                )
            except (
                runtime_control.ResourceLeaseConflictError,
                runtime_control.ResourceLeaseExpiredError,
                runtime_control.StaleResourceLeaseError,
            ) as error:
                raise self._translate(error, page=True) from error
            finally:
                self._release_connection(connection)
            return self._remember(row).page_binding

    def heartbeat(
        self,
        bundle: BrowserLeaseBundle,
        *,
        ttl_seconds: float | None = None,
    ) -> BrowserLeaseBundle:
        with self._lock:
            self._ensure_open()
            process_id, birth = self._process_identity()
            connection = self._connection()
            try:
                row = runtime_control.heartbeat_browser_resource_lease(
                    connection,
                    **self._cas(bundle, process_id, birth),
                    lease_seconds=self._ttl(ttl_seconds),
                    now=self._now(),
                )
            except (
                runtime_control.ResourceLeaseConflictError,
                runtime_control.ResourceLeaseExpiredError,
                runtime_control.StaleResourceLeaseError,
            ) as error:
                raise self._translate(error, page=True) from error
            finally:
                self._release_connection(connection)
            return self._remember(row)

    def advance_page(
        self,
        bundle: BrowserLeaseBundle,
        *,
        expected_page_epoch: int,
    ) -> BrowserLeaseBundle:
        if expected_page_epoch != bundle.page_binding.page_epoch:
            raise StalePageBinding(
                f"stale page epoch {expected_page_epoch}; "
                f"bundle epoch is {bundle.page_binding.page_epoch}"
            )
        with self._lock:
            self._ensure_open()
            process_id, birth = self._process_identity()
            connection = self._connection()
            try:
                row = runtime_control.advance_browser_page_epoch(
                    connection,
                    **self._cas(bundle, process_id, birth),
                    lease_seconds=self._ttl(None),
                    now=self._now(),
                )
            except (
                runtime_control.ResourceLeaseConflictError,
                runtime_control.ResourceLeaseExpiredError,
                runtime_control.StaleResourceLeaseError,
            ) as error:
                raise self._translate(error, page=True) from error
            finally:
                self._release_connection(connection)
            return self._remember(row)

    def require_operation(self, binding: PageBinding, operation: str) -> PageBinding:
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
        known_previous = self._known.get(previous.profile.lease_id)
        if known_previous is None:
            raise BrowserLeaseExpired(
                "cross-instance continuation cannot prove the complete scope token set"
            )
        if (
            not self._same_lease_token(previous.profile, known_previous.profile)
            or not self._same_lease_token(previous.page, known_previous.page)
            or previous.page_binding != known_previous.page_binding
        ):
            raise StalePageBinding("previous continuation bundle is stale")
        previous_authority = (
            previous.profile.owner_id,
            previous.profile.attempt_id,
            previous.profile.runtime_id,
        )
        scope_bundles = tuple(
            bundle
            for bundle in self._known.values()
            if bundle.profile.scope_id == previous.profile.scope_id
        )
        if any(
            (
                bundle.profile.owner_id,
                bundle.profile.attempt_id,
                bundle.profile.runtime_id,
            )
            != previous_authority
            for bundle in scope_bundles
        ):
            raise BrowserLeaseConflict("scope continuation has mixed authority")
        self.release_scope(
            previous.profile.scope_id,
            owner_id=previous.profile.owner_id,
            attempt_id=previous.profile.attempt_id,
            runtime_id=previous.profile.runtime_id,
            expected_bundles=scope_bundles,
        )
        return self.acquire_bundle(
            profile_id=profile_id,
            page_id=page_id,
            owner_id=owner_id,
            scope_id=scope_id,
            attempt_id=attempt_id,
            runtime_id=runtime_id,
            ttl_seconds=ttl_seconds,
        )

    def release_scope(
        self,
        scope_id: str,
        *,
        owner_id: str | None = None,
        attempt_id: str | None = None,
        runtime_id: str | None = None,
        expected_bundles: tuple[BrowserLeaseBundle, ...] | None = None,
    ) -> None:
        with self._lock:
            self._ensure_open()
            supplied = (owner_id, attempt_id, runtime_id)
            if any(value is not None for value in supplied) and not all(
                value is not None for value in supplied
            ):
                raise ValueError("scope release requires owner, attempt, and runtime together")
            if all(value is not None for value in supplied):
                bundles = list(expected_bundles or ())
                if not bundles:
                    bundles = [
                        bundle
                        for bundle in self._known.values()
                        if bundle.profile.scope_id == scope_id
                    ]
            else:
                if expected_bundles is not None:
                    raise ValueError(
                        "ownerless scope release cannot accept cross-instance bundles"
                    )
                bundles = [
                    bundle
                    for bundle in self._known.values()
                    if bundle.profile.scope_id == scope_id
                ]
                if bundles:
                    owner_id = bundles[0].profile.owner_id
                    attempt_id = bundles[0].profile.attempt_id
                    runtime_id = bundles[0].profile.runtime_id
            if not bundles:
                if all(value is not None for value in supplied):
                    raise ValueError(
                        "cross-instance scope release requires exact expected bundles"
                    )
                return
            assert owner_id is not None
            assert attempt_id is not None
            assert runtime_id is not None
            for bundle in bundles:
                identity = (
                    bundle.profile.scope_id,
                    bundle.profile.owner_id,
                    bundle.profile.attempt_id,
                    bundle.profile.runtime_id,
                )
                if identity != (scope_id, owner_id, attempt_id, runtime_id):
                    raise BrowserLeaseConflict("scope release bundle authority mismatch")
            process_id, birth = self._process_identity()
            tokens = tuple(
                runtime_control.BrowserResourceLeaseToken(
                    lease_id=bundle.profile.lease_id,
                    profile_id=bundle.profile.resource_id,
                    page_target_id=bundle.page.resource_id,
                    lease_epoch=bundle.profile.epoch,
                    page_epoch=bundle.page_binding.page_epoch,
                )
                for bundle in bundles
            )
            connection = self._connection()
            try:
                runtime_control.release_browser_resource_scope(
                    connection,
                    scope_id=scope_id,
                    owner_id=owner_id,
                    expected_actor_id=application_actor_id(attempt_id),
                    expected_attempt_id=attempt_id,
                    expected_runtime_id=runtime_id,
                    expected_process_id=process_id,
                    expected_process_birth_time=birth,
                    expected_tokens=tokens,
                    now=self._now(),
                )
            except (
                runtime_control.ResourceLeaseConflictError,
                runtime_control.ResourceLeaseExpiredError,
                runtime_control.StaleResourceLeaseError,
            ) as error:
                raise self._translate(error, page=True) from error
            finally:
                self._release_connection(connection)
            for bundle in bundles:
                self._known.pop(bundle.profile.lease_id, None)

    def close(self) -> None:
        """Close this facade without altering durable active lease state."""
        with self._lock:
            self._closed = True
            self._known.clear()
