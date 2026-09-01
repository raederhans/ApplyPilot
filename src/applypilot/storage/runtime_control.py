"""Versioned durable browser leases and runtime-turn lineage."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from applypilot.apply.contracts import application_actor_id


class ResourceLeaseConflictError(RuntimeError):
    """A profile or page is already leased by another owner."""


class StaleResourceLeaseError(RuntimeError):
    """A lease operation used an obsolete lease or page epoch."""


class ResourceLeaseExpiredError(RuntimeError):
    """A lease operation targeted a lease that is no longer active."""


class RuntimeTurnConflictError(RuntimeError):
    """A canonical actor already has a different running turn."""


CONTROL_PLANE_SCHEMA_VERSION = 1
_TERMINAL = {
    "blocked",
    "cancelled",
    "closed",
    "completed",
    "failed",
    "timed_out",
    "unknown",
}
_RESUME_MODES = {"root", "resume", "receipt_only"}


@dataclass(frozen=True, slots=True)
class BrowserResourceLease:
    lease_id: str
    resource_kind: str
    scope_id: str
    profile_id: str
    page_target_id: str | None
    owner_id: str
    actor_id: str
    attempt_id: str
    runtime_id: str
    lease_epoch: int
    page_epoch: int
    heartbeat_at: str
    expires_at: str
    status: str
    process_id: int | None
    process_birth_time: int | None
    created_at: str
    released_at: str | None


@dataclass(frozen=True, slots=True)
class BrowserResourceLeaseToken:
    """Exact caller-held CAS token for an atomic scope release."""

    lease_id: str
    profile_id: str
    page_target_id: str
    lease_epoch: int
    page_epoch: int


@dataclass(frozen=True, slots=True)
class AgentRuntimeTurn:
    turn_id: str
    actor_id: str
    attempt_id: str
    parent_turn_id: str | None
    checkpoint_id: str | None
    runtime_id: str
    profile_id: str
    runtime_backend: str
    model: str | None
    provider_session_id: str | None
    process_id: int | None
    process_birth_time: int | None
    resume_mode: str
    submit_started: int
    status: str
    started_at: str
    terminal_at: str | None
    failure_code: str | None
    exit_code: int | None
    tool_surface_hash: str
    prompt_contract_hash: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RuntimeTurnToken:
    """Exact immutable turn identity plus its current process binding."""

    turn_id: str
    actor_id: str
    attempt_id: str
    parent_turn_id: str | None
    checkpoint_id: str | None
    runtime_id: str
    profile_id: str
    runtime_backend: str
    model: str | None
    provider_session_id: str | None
    process_id: int | None
    process_birth_time: int | None
    resume_mode: str
    submit_started: int
    started_at: str
    tool_surface_hash: str
    prompt_contract_hash: str
    idempotency_key: str


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control-plane timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


@contextmanager
def _write(connection: sqlite3.Connection) -> Iterator[None]:
    owns = not connection.in_transaction
    if owns:
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.execute("SAVEPOINT runtime_control_write")
    try:
        yield
        if owns:
            connection.commit()
        else:
            connection.execute("RELEASE SAVEPOINT runtime_control_write")
    except ResourceLeaseExpiredError:
        if owns:
            connection.commit()
        else:
            connection.execute("RELEASE SAVEPOINT runtime_control_write")
        raise
    except Exception:
        if owns and connection.in_transaction:
            connection.rollback()
        elif not owns:
            connection.execute("ROLLBACK TO SAVEPOINT runtime_control_write")
            connection.execute("RELEASE SAVEPOINT runtime_control_write")
        raise


@contextmanager
def _migration(connection: sqlite3.Connection) -> Iterator[None]:
    nested = connection.in_transaction
    if nested:
        connection.execute("SAVEPOINT runtime_control_migration")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            connection.execute("RELEASE SAVEPOINT runtime_control_migration")
        else:
            connection.commit()
    except Exception:
        if nested:
            connection.execute("ROLLBACK TO SAVEPOINT runtime_control_migration")
            connection.execute("RELEASE SAVEPOINT runtime_control_migration")
        elif connection.in_transaction:
            connection.rollback()
        raise


def _migration_v1(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE browser_resource_leases (
            lease_id TEXT PRIMARY KEY, resource_kind TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            profile_id TEXT NOT NULL, page_target_id TEXT, owner_id TEXT NOT NULL,
            actor_id TEXT NOT NULL, attempt_id TEXT NOT NULL, runtime_id TEXT NOT NULL,
            lease_epoch INTEGER NOT NULL CHECK(lease_epoch > 0),
            page_epoch INTEGER NOT NULL CHECK(page_epoch >= 0),
            heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','expired','released')),
            process_id INTEGER, process_birth_time INTEGER,
            created_at TEXT NOT NULL, released_at TEXT,
            CHECK((process_id IS NULL AND process_birth_time IS NULL) OR
                  (process_id > 0 AND process_birth_time > 0)))""",
        """CREATE UNIQUE INDEX idx_browser_leases_active_profile
            ON browser_resource_leases(profile_id) WHERE status='active'""",
        """CREATE UNIQUE INDEX idx_browser_leases_active_page
            ON browser_resource_leases(page_target_id)
            WHERE status='active' AND page_target_id IS NOT NULL""",
        """CREATE INDEX idx_browser_leases_profile_epoch
            ON browser_resource_leases(profile_id, lease_epoch DESC)""",
        """CREATE INDEX idx_browser_leases_owner
            ON browser_resource_leases(owner_id, status, expires_at)""",
        """CREATE INDEX idx_browser_leases_scope_binding
            ON browser_resource_leases(
                scope_id, owner_id, actor_id, attempt_id, runtime_id,
                process_id, process_birth_time, status
            )""",
        """CREATE TABLE agent_runtime_turns (
            turn_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
            parent_turn_id TEXT REFERENCES agent_runtime_turns(turn_id),
            checkpoint_id TEXT, runtime_id TEXT NOT NULL, profile_id TEXT NOT NULL,
            runtime_backend TEXT NOT NULL, model TEXT, provider_session_id TEXT,
            process_id INTEGER, process_birth_time INTEGER,
            resume_mode TEXT NOT NULL CHECK(resume_mode IN ('root','resume','receipt_only')),
            submit_started INTEGER NOT NULL CHECK(submit_started IN (0,1)),
            status TEXT NOT NULL CHECK(status IN
                ('running','blocked','cancelled','closed','completed','failed',
                 'timed_out','unknown')),
            started_at TEXT NOT NULL, terminal_at TEXT, failure_code TEXT, exit_code INTEGER,
            tool_surface_hash TEXT NOT NULL, prompt_contract_hash TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            CHECK((process_id IS NULL AND process_birth_time IS NULL) OR
                  (process_id > 0 AND process_birth_time > 0)),
            CHECK((parent_turn_id IS NULL AND resume_mode='root' AND checkpoint_id IS NULL) OR
                  (parent_turn_id IS NOT NULL AND resume_mode!='root' AND
                   checkpoint_id IS NOT NULL)),
            CHECK((status='running' AND terminal_at IS NULL) OR
                  (status!='running' AND terminal_at IS NOT NULL)))""",
        """CREATE INDEX idx_runtime_turns_actor
            ON agent_runtime_turns(actor_id, started_at, turn_id)""",
        """CREATE UNIQUE INDEX idx_runtime_turns_one_running_actor
            ON agent_runtime_turns(actor_id) WHERE status='running'""",
        """CREATE INDEX idx_runtime_turns_parent
            ON agent_runtime_turns(parent_turn_id, started_at, turn_id)
            WHERE parent_turn_id IS NOT NULL""",
    )
    for statement in statements:
        connection.execute(statement)


_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (_migration_v1,)


def ensure_schema(connection: sqlite3.Connection) -> int:
    """Atomically migrate this component and return its explicit schema version."""
    with _migration(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS agent_control_schema_version (
            component TEXT PRIMARY KEY CHECK(component='durable_control_plane'),
            version INTEGER NOT NULL CHECK(version >= 0), updated_at TEXT NOT NULL)""")
        row = connection.execute(
            "SELECT version FROM agent_control_schema_version "
            "WHERE component='durable_control_plane'"
        ).fetchone()
        current = 0 if row is None else int(row[0])
        if current > len(_MIGRATIONS):
            raise RuntimeError(
                f"durable control-plane schema {current} is newer than supported "
                f"{len(_MIGRATIONS)}"
            )
        for version in range(current + 1, len(_MIGRATIONS) + 1):
            _MIGRATIONS[version - 1](connection)
            connection.execute(
                "INSERT INTO agent_control_schema_version VALUES"
                "('durable_control_plane',?,?) ON CONFLICT(component) DO UPDATE SET "
                "version=excluded.version,updated_at=excluded.updated_at",
                (version, _iso(datetime.now(UTC))),
            )
    return len(_MIGRATIONS)


_LEASE_COLUMNS = (
    "lease_id,resource_kind,scope_id,profile_id,page_target_id,owner_id,actor_id,attempt_id,"
    "runtime_id,lease_epoch,page_epoch,heartbeat_at,expires_at,status,process_id,"
    "process_birth_time,created_at,released_at"
)


def _lease(row: sqlite3.Row | tuple[object, ...] | None) -> BrowserResourceLease | None:
    return None if row is None else BrowserResourceLease(*tuple(row))


def _load_lease(connection: sqlite3.Connection, lease_id: str) -> BrowserResourceLease | None:
    return _lease(connection.execute(
        f"SELECT {_LEASE_COLUMNS} FROM browser_resource_leases WHERE lease_id=?",
        (lease_id,),
    ).fetchone())


def inspect_browser_resource_lease_state(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    page_target_id: str,
    now: datetime | None = None,
) -> tuple[BrowserResourceLease | None, int]:
    """Return the active conflicting row and latest profile epoch atomically."""
    _require(profile_id=profile_id, page_target_id=page_target_id)
    ensure_schema(connection)
    now_text = _iso(now or datetime.now(UTC))
    with _write(connection):
        connection.execute(
            "UPDATE browser_resource_leases SET status='expired',released_at=? "
            "WHERE status='active' AND expires_at<=? AND "
            "(profile_id=? OR page_target_id=?)",
            (now_text, now_text, profile_id, page_target_id),
        )
        row = connection.execute(
            f"SELECT {_LEASE_COLUMNS} FROM browser_resource_leases "
            "WHERE status='active' AND (profile_id=? OR page_target_id=?) "
            "ORDER BY created_at,lease_id LIMIT 1",
            (profile_id, page_target_id),
        ).fetchone()
        latest = int(
            connection.execute(
                "SELECT COALESCE(MAX(lease_epoch),0) FROM browser_resource_leases "
                "WHERE profile_id=?",
                (profile_id,),
            ).fetchone()[0]
        )
    return _lease(row), latest


def active_browser_resource_leases_for_scope(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    owner_id: str,
    actor_id: str,
    attempt_id: str,
    runtime_id: str,
    process_id: int,
    process_birth_time: int,
    now: datetime | None = None,
) -> tuple[BrowserResourceLease, ...]:
    """List only active rows under one complete scope/authority binding."""
    _require(
        scope_id=scope_id,
        owner_id=owner_id,
        actor_id=actor_id,
        attempt_id=attempt_id,
        runtime_id=runtime_id,
    )
    _validate_actor(actor_id, attempt_id)
    _validate_process_identity(process_id, process_birth_time)
    ensure_schema(connection)
    now_text = _iso(now or datetime.now(UTC))
    with _write(connection):
        connection.execute(
            "UPDATE browser_resource_leases SET status='expired',released_at=? "
            "WHERE status='active' AND expires_at<=? AND scope_id=?",
            (now_text, now_text, scope_id),
        )
        rows = connection.execute(
            f"SELECT {_LEASE_COLUMNS} FROM browser_resource_leases WHERE status='active' "
            "AND scope_id=? AND owner_id=? AND actor_id=? AND attempt_id=? "
            "AND runtime_id=? AND process_id=? AND process_birth_time=? "
            "ORDER BY created_at,lease_id",
            (
                scope_id,
                owner_id,
                actor_id,
                attempt_id,
                runtime_id,
                process_id,
                process_birth_time,
            ),
        ).fetchall()
        if not rows:
            conflicting = connection.execute(
                "SELECT 1 FROM browser_resource_leases "
                "WHERE status='active' AND scope_id=? LIMIT 1",
                (scope_id,),
            ).fetchone()
            if conflicting is not None:
                raise ResourceLeaseConflictError(
                    f"scope authority binding mismatch: {scope_id}"
                )
    return tuple(_lease(row) for row in rows if row is not None)


def _require(**values: str) -> None:
    blank = [name for name, value in values.items() if not str(value or "").strip()]
    if blank:
        raise ValueError(f"required identifiers are blank: {', '.join(blank)}")


def _validate_actor(actor_id: str, attempt_id: str) -> None:
    if actor_id != application_actor_id(attempt_id):
        raise ValueError("actor_id must be the canonical identity for attempt_id")


def _validate_process_identity(
    process_id: int | None,
    process_birth_time: int | None,
) -> None:
    if (process_id is None) != (process_birth_time is None):
        raise ValueError("process_id and process_birth_time must be supplied together")
    if process_id is not None and (process_id < 1 or process_birth_time < 1):
        raise ValueError("process identity values must be positive integers")


def acquire_browser_resource_lease(
    connection: sqlite3.Connection,
    *,
    lease_id: str,
    resource_kind: str,
    scope_id: str,
    profile_id: str,
    page_target_id: str | None,
    owner_id: str,
    actor_id: str,
    attempt_id: str,
    runtime_id: str,
    expected_lease_epoch: int | None = None,
    page_epoch: int = 0,
    lease_seconds: int = 300,
    process_id: int | None = None,
    process_birth_time: int | None = None,
    now: datetime | None = None,
) -> BrowserResourceLease:
    """Acquire one exclusive profile/page lease using a profile epoch CAS."""
    _require(
        lease_id=lease_id, resource_kind=resource_kind, scope_id=scope_id,
        profile_id=profile_id,
        owner_id=owner_id, actor_id=actor_id, attempt_id=attempt_id,
        runtime_id=runtime_id,
    )
    _validate_actor(actor_id, attempt_id)
    _validate_process_identity(process_id, process_birth_time)
    if lease_seconds < 1 or page_epoch < 0:
        raise ValueError("lease duration or page epoch is invalid")
    ensure_schema(connection)
    instant = now or datetime.now(UTC)
    now_text = _iso(instant)
    with _write(connection):
        replay = _load_lease(connection, lease_id)
        requested = (
            resource_kind, scope_id, profile_id, page_target_id, owner_id, actor_id, attempt_id,
            runtime_id, page_epoch, process_id, process_birth_time,
        )
        if replay is not None:
            persisted = (
                replay.resource_kind, replay.scope_id, replay.profile_id, replay.page_target_id,
                replay.owner_id, replay.actor_id, replay.attempt_id, replay.runtime_id,
                replay.page_epoch, replay.process_id, replay.process_birth_time,
            )
            if persisted != requested:
                raise ValueError(f"lease_id collision: {lease_id}")
            if replay.status != "active" or replay.expires_at <= now_text:
                if replay.status == "active":
                    cursor = connection.execute(
                        "UPDATE browser_resource_leases SET status='expired',released_at=? "
                        "WHERE lease_id=? AND status='active'",
                        (now_text, lease_id),
                    )
                    if cursor.rowcount != 1:
                        raise StaleResourceLeaseError(f"lease expiry CAS failed: {lease_id}")
                raise ResourceLeaseExpiredError(
                    f"lease replay is not active: {lease_id}"
                )
            if (
                expected_lease_epoch is not None
                and expected_lease_epoch != replay.lease_epoch - 1
            ):
                raise StaleResourceLeaseError(
                    f"stale acquisition epoch for replay: {lease_id}"
                )
            return replay
        connection.execute(
            "UPDATE browser_resource_leases SET status='expired',released_at=? "
            "WHERE status='active' AND expires_at<=? AND "
            "(profile_id=? OR (? IS NOT NULL AND page_target_id=?))",
            (now_text, now_text, profile_id, page_target_id, page_target_id),
        )
        latest = int(connection.execute(
            "SELECT COALESCE(MAX(lease_epoch),0) FROM browser_resource_leases "
            "WHERE profile_id=?", (profile_id,)
        ).fetchone()[0])
        if expected_lease_epoch is not None and expected_lease_epoch != latest:
            raise StaleResourceLeaseError(
                f"expected lease epoch {expected_lease_epoch}, current is {latest}"
            )
        conflict = connection.execute(
            "SELECT lease_id FROM browser_resource_leases WHERE status='active' AND "
            "(profile_id=? OR (? IS NOT NULL AND page_target_id=?)) LIMIT 1",
            (profile_id, page_target_id, page_target_id),
        ).fetchone()
        if conflict is not None:
            raise ResourceLeaseConflictError(f"resource already leased by {conflict[0]}")
        try:
            connection.execute(
                f"INSERT INTO browser_resource_leases({_LEASE_COLUMNS}) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    lease_id, resource_kind, scope_id, profile_id, page_target_id, owner_id,
                    actor_id, attempt_id, runtime_id, latest + 1, page_epoch, now_text,
                    _iso(instant + timedelta(seconds=lease_seconds)), "active", process_id,
                    process_birth_time, now_text,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ResourceLeaseConflictError("profile or page lease contention") from error
        acquired = _load_lease(connection, lease_id)
    assert acquired is not None
    return acquired


def _active(
    connection: sqlite3.Connection,
    lease_id: str,
    owner_id: str,
    actor_id: str,
    attempt_id: str,
    runtime_id: str,
    resource_kind: str,
    scope_id: str,
    profile_id: str,
    page_target_id: str | None,
    lease_epoch: int,
    page_epoch: int,
    process_id: int | None,
    process_birth_time: int | None,
    now_text: str,
) -> BrowserResourceLease:
    _validate_actor(actor_id, attempt_id)
    _validate_process_identity(process_id, process_birth_time)
    current = _load_lease(connection, lease_id)
    if current is None:
        raise StaleResourceLeaseError(f"unknown lease: {lease_id}")
    if current.status == "active" and current.expires_at <= now_text:
        cursor = connection.execute(
            "UPDATE browser_resource_leases SET status='expired',released_at=? "
            "WHERE lease_id=? AND status='active'", (now_text, lease_id)
        )
        if cursor.rowcount != 1:
            raise StaleResourceLeaseError(f"lease expiry CAS failed: {lease_id}")
        raise ResourceLeaseExpiredError(f"lease expired: {lease_id}")
    if current.status != "active":
        raise ResourceLeaseExpiredError(f"lease is {current.status}: {lease_id}")
    expected_binding = (
        owner_id,
        actor_id,
        attempt_id,
        runtime_id,
        resource_kind,
        scope_id,
        profile_id,
        page_target_id,
        process_id,
        process_birth_time,
    )
    current_binding = (
        current.owner_id,
        current.actor_id,
        current.attempt_id,
        current.runtime_id,
        current.resource_kind,
        current.scope_id,
        current.profile_id,
        current.page_target_id,
        current.process_id,
        current.process_birth_time,
    )
    if current_binding != expected_binding:
        raise ResourceLeaseConflictError(f"lease binding mismatch: {lease_id}")
    if current.lease_epoch != lease_epoch or current.page_epoch != page_epoch:
        raise StaleResourceLeaseError(f"stale lease/page epoch: {lease_id}")
    return current


def validate_browser_resource_lease(
    connection: sqlite3.Connection, *, lease_id: str, owner_id: str,
    expected_actor_id: str, expected_attempt_id: str, expected_runtime_id: str,
    expected_resource_kind: str, expected_scope_id: str, expected_profile_id: str,
    expected_page_target_id: str | None, expected_lease_epoch: int,
    expected_page_epoch: int,
    expected_process_id: int | None, expected_process_birth_time: int | None,
    now: datetime | None = None,
) -> BrowserResourceLease:
    ensure_schema(connection)
    with _write(connection):
        return _active(
            connection, lease_id, owner_id, expected_actor_id, expected_attempt_id,
            expected_runtime_id, expected_resource_kind, expected_scope_id,
            expected_profile_id, expected_page_target_id, expected_lease_epoch,
            expected_page_epoch, expected_process_id, expected_process_birth_time,
            _iso(now or datetime.now(UTC)),
        )


def _renew_or_advance(
    connection: sqlite3.Connection, *, lease_id: str, owner_id: str,
    expected_actor_id: str, expected_attempt_id: str, expected_runtime_id: str,
    expected_resource_kind: str, expected_scope_id: str, expected_profile_id: str,
    expected_page_target_id: str | None,
    expected_lease_epoch: int, expected_page_epoch: int, lease_seconds: int,
    expected_process_id: int | None, expected_process_birth_time: int | None,
    advance: bool, now: datetime | None,
) -> BrowserResourceLease:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    ensure_schema(connection)
    instant = now or datetime.now(UTC)
    now_text = _iso(instant)
    expires_text = _iso(instant + timedelta(seconds=lease_seconds))
    with _write(connection):
        current = _active(
            connection, lease_id, owner_id, expected_actor_id, expected_attempt_id,
            expected_runtime_id, expected_resource_kind, expected_scope_id,
            expected_profile_id, expected_page_target_id, expected_lease_epoch,
            expected_page_epoch, expected_process_id, expected_process_birth_time, now_text,
        )
        if now_text < current.heartbeat_at:
            raise StaleResourceLeaseError(
                f"lease heartbeat timeline moved backwards: {lease_id}"
            )
        if expires_text < current.expires_at:
            raise StaleResourceLeaseError(
                f"lease expiry timeline moved backwards: {lease_id}"
            )
        cursor = connection.execute(
            "UPDATE browser_resource_leases SET page_epoch=page_epoch+?,heartbeat_at=?,"
            "expires_at=? WHERE lease_id=? AND status='active' AND owner_id=? "
            "AND actor_id=? AND attempt_id=? AND runtime_id=? AND resource_kind=? "
            "AND scope_id=? AND profile_id=? AND page_target_id IS ? "
            "AND process_id IS ? AND process_birth_time IS ? "
            "AND lease_epoch=? AND page_epoch=? "
            "AND heartbeat_at=? AND expires_at=?",
            (
                int(advance), now_text, expires_text,
                lease_id, owner_id, expected_actor_id, expected_attempt_id,
                expected_runtime_id, expected_resource_kind, expected_scope_id,
                expected_profile_id, expected_page_target_id, expected_process_id,
                expected_process_birth_time, expected_lease_epoch, expected_page_epoch,
                current.heartbeat_at, current.expires_at,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleResourceLeaseError(f"lease update CAS failed: {lease_id}")
        updated = _load_lease(connection, lease_id)
    assert updated is not None
    return updated


def heartbeat_browser_resource_lease(
    connection: sqlite3.Connection,
    *,
    lease_id: str,
    owner_id: str,
    expected_actor_id: str,
    expected_attempt_id: str,
    expected_runtime_id: str,
    expected_resource_kind: str,
    expected_scope_id: str,
    expected_profile_id: str,
    expected_page_target_id: str | None,
    expected_lease_epoch: int,
    expected_page_epoch: int,
    expected_process_id: int | None,
    expected_process_birth_time: int | None,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> BrowserResourceLease:
    """Renew an exact live lease without changing its page epoch."""
    return _renew_or_advance(
        connection,
        lease_id=lease_id,
        owner_id=owner_id,
        expected_actor_id=expected_actor_id,
        expected_attempt_id=expected_attempt_id,
        expected_runtime_id=expected_runtime_id,
        expected_resource_kind=expected_resource_kind,
        expected_scope_id=expected_scope_id,
        expected_profile_id=expected_profile_id,
        expected_page_target_id=expected_page_target_id,
        expected_lease_epoch=expected_lease_epoch,
        expected_page_epoch=expected_page_epoch,
        expected_process_id=expected_process_id,
        expected_process_birth_time=expected_process_birth_time,
        lease_seconds=lease_seconds,
        advance=False,
        now=now,
    )


def advance_browser_page_epoch(
    connection: sqlite3.Connection,
    *,
    lease_id: str,
    owner_id: str,
    expected_actor_id: str,
    expected_attempt_id: str,
    expected_runtime_id: str,
    expected_resource_kind: str,
    expected_scope_id: str,
    expected_profile_id: str,
    expected_page_target_id: str | None,
    expected_lease_epoch: int,
    expected_page_epoch: int,
    expected_process_id: int | None,
    expected_process_birth_time: int | None,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> BrowserResourceLease:
    """Renew a live lease and invalidate every earlier page binding."""
    return _renew_or_advance(
        connection,
        lease_id=lease_id,
        owner_id=owner_id,
        expected_actor_id=expected_actor_id,
        expected_attempt_id=expected_attempt_id,
        expected_runtime_id=expected_runtime_id,
        expected_resource_kind=expected_resource_kind,
        expected_scope_id=expected_scope_id,
        expected_profile_id=expected_profile_id,
        expected_page_target_id=expected_page_target_id,
        expected_lease_epoch=expected_lease_epoch,
        expected_page_epoch=expected_page_epoch,
        expected_process_id=expected_process_id,
        expected_process_birth_time=expected_process_birth_time,
        lease_seconds=lease_seconds,
        advance=True,
        now=now,
    )


def release_browser_resource_lease(
    connection: sqlite3.Connection, *, lease_id: str, owner_id: str,
    expected_actor_id: str, expected_attempt_id: str, expected_runtime_id: str,
    expected_resource_kind: str, expected_scope_id: str, expected_profile_id: str,
    expected_page_target_id: str | None, expected_lease_epoch: int,
    expected_page_epoch: int,
    expected_process_id: int | None, expected_process_birth_time: int | None,
    now: datetime | None = None,
) -> BrowserResourceLease:
    """Release an exact live lease; an exact release replay is a no-op."""
    ensure_schema(connection)
    now_text = _iso(now or datetime.now(UTC))
    with _write(connection):
        current = _load_lease(connection, lease_id)
        if (
            current is not None and current.status == "released"
            and current.owner_id == owner_id
            and current.actor_id == expected_actor_id
            and current.attempt_id == expected_attempt_id
            and current.runtime_id == expected_runtime_id
            and current.resource_kind == expected_resource_kind
            and current.scope_id == expected_scope_id
            and current.profile_id == expected_profile_id
            and current.page_target_id == expected_page_target_id
            and current.lease_epoch == expected_lease_epoch
            and current.page_epoch == expected_page_epoch
            and current.process_id == expected_process_id
            and current.process_birth_time == expected_process_birth_time
        ):
            return current
        _active(
            connection, lease_id, owner_id, expected_actor_id, expected_attempt_id,
            expected_runtime_id, expected_resource_kind, expected_scope_id,
            expected_profile_id, expected_page_target_id, expected_lease_epoch,
            expected_page_epoch, expected_process_id, expected_process_birth_time,
            now_text,
        )
        cursor = connection.execute(
            "UPDATE browser_resource_leases SET status='released',released_at=? "
            "WHERE lease_id=? AND status='active' AND owner_id=? AND actor_id=? "
            "AND attempt_id=? AND runtime_id=? AND resource_kind=? AND scope_id=? "
            "AND profile_id=? AND page_target_id IS ? AND process_id IS ? "
            "AND process_birth_time IS ? AND lease_epoch=? AND page_epoch=?",
            (
                now_text, lease_id, owner_id, expected_actor_id, expected_attempt_id,
                expected_runtime_id, expected_resource_kind, expected_scope_id,
                expected_profile_id, expected_page_target_id, expected_process_id,
                expected_process_birth_time, expected_lease_epoch, expected_page_epoch,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleResourceLeaseError(f"lease release CAS failed: {lease_id}")
        released = _load_lease(connection, lease_id)
    assert released is not None
    return released


def release_browser_resource_scope(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    owner_id: str,
    expected_actor_id: str,
    expected_attempt_id: str,
    expected_runtime_id: str,
    expected_process_id: int,
    expected_process_birth_time: int,
    expected_tokens: tuple[BrowserResourceLeaseToken, ...],
    now: datetime | None = None,
) -> tuple[BrowserResourceLease, ...]:
    """Atomically validate and release the complete exact active scope."""
    _require(
        scope_id=scope_id,
        owner_id=owner_id,
        expected_actor_id=expected_actor_id,
        expected_attempt_id=expected_attempt_id,
        expected_runtime_id=expected_runtime_id,
    )
    _validate_actor(expected_actor_id, expected_attempt_id)
    _validate_process_identity(expected_process_id, expected_process_birth_time)
    if not expected_tokens:
        raise ValueError("scope release requires exact expected lease tokens")
    token_map = {token.lease_id: token for token in expected_tokens}
    if len(token_map) != len(expected_tokens):
        raise ValueError("scope release lease tokens must be unique")
    for token in expected_tokens:
        _require(
            lease_id=token.lease_id,
            profile_id=token.profile_id,
            page_target_id=token.page_target_id,
        )
        if token.lease_epoch < 1 or token.page_epoch < 0:
            raise ValueError("scope release token epochs are invalid")
    ensure_schema(connection)
    now_text = _iso(now or datetime.now(UTC))
    with _write(connection):
        active_rows = tuple(
            _lease(row)
            for row in connection.execute(
                f"SELECT {_LEASE_COLUMNS} FROM browser_resource_leases "
                "WHERE scope_id=? AND status='active' ORDER BY lease_id",
                (scope_id,),
            ).fetchall()
        )
        active = tuple(row for row in active_rows if row is not None)
        if active:
            authority = (
                owner_id,
                expected_actor_id,
                expected_attempt_id,
                expected_runtime_id,
                expected_process_id,
                expected_process_birth_time,
            )
            for row in active:
                if row.expires_at <= now_text:
                    raise ResourceLeaseExpiredError(
                        f"scope contains expired lease: {row.lease_id}"
                    )
                persisted_authority = (
                    row.owner_id,
                    row.actor_id,
                    row.attempt_id,
                    row.runtime_id,
                    row.process_id,
                    row.process_birth_time,
                )
                if persisted_authority != authority:
                    raise ResourceLeaseConflictError(
                        f"scope authority binding mismatch: {scope_id}"
                    )
            persisted_tokens = {
                row.lease_id: BrowserResourceLeaseToken(
                    lease_id=row.lease_id,
                    profile_id=row.profile_id,
                    page_target_id=str(row.page_target_id),
                    lease_epoch=row.lease_epoch,
                    page_epoch=row.page_epoch,
                )
                for row in active
            }
            if persisted_tokens != token_map:
                raise StaleResourceLeaseError(
                    f"scope lease token set changed: {scope_id}"
                )
            cursor = connection.execute(
                "UPDATE browser_resource_leases SET status='released',released_at=? "
                "WHERE scope_id=? AND status='active' AND owner_id=? AND actor_id=? "
                "AND attempt_id=? AND runtime_id=? AND process_id=? "
                "AND process_birth_time=?",
                (
                    now_text,
                    scope_id,
                    owner_id,
                    expected_actor_id,
                    expected_attempt_id,
                    expected_runtime_id,
                    expected_process_id,
                    expected_process_birth_time,
                ),
            )
            if cursor.rowcount != len(active):
                raise StaleResourceLeaseError(
                    f"scope release batch CAS failed: {scope_id}"
                )
        placeholders = ",".join("?" for _ in token_map)
        rows = tuple(
            row
            for row in (
                _lease(candidate)
                for candidate in connection.execute(
                    f"SELECT {_LEASE_COLUMNS} FROM browser_resource_leases "
                    f"WHERE lease_id IN ({placeholders}) ORDER BY lease_id",
                    tuple(token_map),
                ).fetchall()
            )
            if row is not None
        )
        if len(rows) != len(token_map):
            raise StaleResourceLeaseError(f"scope release token is unknown: {scope_id}")
        for row in rows:
            token = token_map[row.lease_id]
            if (
                row.status != "released"
                or row.scope_id != scope_id
                or row.owner_id != owner_id
                or row.actor_id != expected_actor_id
                or row.attempt_id != expected_attempt_id
                or row.runtime_id != expected_runtime_id
                or row.process_id != expected_process_id
                or row.process_birth_time != expected_process_birth_time
                or row.profile_id != token.profile_id
                or row.page_target_id != token.page_target_id
                or row.lease_epoch != token.lease_epoch
                or row.page_epoch != token.page_epoch
            ):
                if row.status in {"expired", "released"}:
                    raise ResourceLeaseExpiredError(
                        f"scope release token is no longer active: {row.lease_id}"
                    )
                raise StaleResourceLeaseError(
                    f"scope release token changed: {row.lease_id}"
                )
    return rows


_TURN_COLUMNS = (
    "turn_id,actor_id,attempt_id,parent_turn_id,checkpoint_id,runtime_id,profile_id,"
    "runtime_backend,model,provider_session_id,process_id,process_birth_time,resume_mode,"
    "submit_started,status,started_at,terminal_at,failure_code,exit_code,tool_surface_hash,"
    "prompt_contract_hash,idempotency_key"
)


def _turn(row: sqlite3.Row | tuple[object, ...] | None) -> AgentRuntimeTurn | None:
    return None if row is None else AgentRuntimeTurn(*tuple(row))


def token_from_turn(turn: AgentRuntimeTurn) -> RuntimeTurnToken:
    """Return the exact CAS token for the current persisted turn binding."""
    return RuntimeTurnToken(
        turn_id=turn.turn_id,
        actor_id=turn.actor_id,
        attempt_id=turn.attempt_id,
        parent_turn_id=turn.parent_turn_id,
        checkpoint_id=turn.checkpoint_id,
        runtime_id=turn.runtime_id,
        profile_id=turn.profile_id,
        runtime_backend=turn.runtime_backend,
        model=turn.model,
        provider_session_id=turn.provider_session_id,
        process_id=turn.process_id,
        process_birth_time=turn.process_birth_time,
        resume_mode=turn.resume_mode,
        submit_started=turn.submit_started,
        started_at=turn.started_at,
        tool_surface_hash=turn.tool_surface_hash,
        prompt_contract_hash=turn.prompt_contract_hash,
        idempotency_key=turn.idempotency_key,
    )


def _token_identity(token: RuntimeTurnToken) -> tuple[object, ...]:
    return (
        token.turn_id,
        token.actor_id,
        token.attempt_id,
        token.parent_turn_id,
        token.checkpoint_id,
        token.runtime_id,
        token.profile_id,
        token.runtime_backend,
        token.model,
        token.provider_session_id,
        token.resume_mode,
        token.submit_started,
        token.started_at,
        token.tool_surface_hash,
        token.prompt_contract_hash,
        token.idempotency_key,
    )


def _turn_identity(turn: AgentRuntimeTurn) -> tuple[object, ...]:
    token = token_from_turn(turn)
    return _token_identity(token)


def _validate_runtime_token(token: RuntimeTurnToken) -> None:
    _require(
        turn_id=token.turn_id,
        actor_id=token.actor_id,
        attempt_id=token.attempt_id,
        runtime_id=token.runtime_id,
        profile_id=token.profile_id,
        runtime_backend=token.runtime_backend,
        resume_mode=token.resume_mode,
        started_at=token.started_at,
        tool_surface_hash=token.tool_surface_hash,
        prompt_contract_hash=token.prompt_contract_hash,
        idempotency_key=token.idempotency_key,
    )
    _validate_actor(token.actor_id, token.attempt_id)
    _validate_process_identity(token.process_id, token.process_birth_time)
    if token.resume_mode not in _RESUME_MODES or token.submit_started not in (0, 1):
        raise ValueError("runtime turn token shape is invalid")


def get_runtime_turn(connection: sqlite3.Connection, turn_id: str) -> AgentRuntimeTurn | None:
    ensure_schema(connection)
    return _turn(connection.execute(
        f"SELECT {_TURN_COLUMNS} FROM agent_runtime_turns WHERE turn_id=?", (turn_id,)
    ).fetchone())


def running_runtime_turn_for_actor(
    connection: sqlite3.Connection,
    actor_id: str,
) -> AgentRuntimeTurn | None:
    """Return the single durable running turn for one canonical actor."""
    _require(actor_id=actor_id)
    ensure_schema(connection)
    return _turn(connection.execute(
        f"SELECT {_TURN_COLUMNS} FROM agent_runtime_turns "
        "WHERE actor_id=? AND status='running' LIMIT 1",
        (actor_id,),
    ).fetchone())


def latest_runtime_turn_for_actor(
    connection: sqlite3.Connection,
    actor_id: str,
) -> AgentRuntimeTurn | None:
    """Return the deterministically newest persisted turn for one actor."""
    _require(actor_id=actor_id)
    ensure_schema(connection)
    return _turn(connection.execute(
        f"SELECT {_TURN_COLUMNS} FROM agent_runtime_turns "
        "WHERE actor_id=? ORDER BY rowid DESC LIMIT 1",
        (actor_id,),
    ).fetchone())


def parent_runtime_turn(connection: sqlite3.Connection, turn_id: str) -> AgentRuntimeTurn | None:
    ensure_schema(connection)
    return _turn(connection.execute(
        f"SELECT {_TURN_COLUMNS} FROM agent_runtime_turns WHERE turn_id=(SELECT "
        "parent_turn_id FROM agent_runtime_turns WHERE turn_id=?)", (turn_id,)
    ).fetchone())


def start_runtime_turn(
    connection: sqlite3.Connection, *, turn_id: str, actor_id: str, attempt_id: str,
    runtime_id: str, profile_id: str, runtime_backend: str, resume_mode: str,
    submit_started: bool, tool_surface_hash: str, prompt_contract_hash: str,
    parent_turn_id: str | None = None, checkpoint_id: str | None = None,
    model: str | None = None, provider_session_id: str | None = None,
    process_id: int | None = None, process_birth_time: int | None = None,
    idempotency_key: str | None = None,
    started_at: datetime | None = None,
    require_new: bool = False,
) -> AgentRuntimeTurn:
    """Persist a running turn and optional same-actor parent lineage.

    ``require_new`` is the launch-reservation path: an exact persisted replay is
    a conflict so an old unbound row can never authorize another process spawn.
    """
    _require(
        turn_id=turn_id, actor_id=actor_id, attempt_id=attempt_id,
        runtime_id=runtime_id, profile_id=profile_id, runtime_backend=runtime_backend,
        resume_mode=resume_mode, tool_surface_hash=tool_surface_hash,
        prompt_contract_hash=prompt_contract_hash,
    )
    _validate_actor(actor_id, attempt_id)
    _validate_process_identity(process_id, process_birth_time)
    if resume_mode not in _RESUME_MODES:
        raise ValueError(f"unsupported resume_mode: {resume_mode}")
    if parent_turn_id is None:
        if resume_mode != "root" or checkpoint_id is not None:
            raise ValueError("root turn requires root mode, no parent, and no checkpoint")
    elif resume_mode == "root" or checkpoint_id is None:
        raise ValueError("child turn requires a checkpoint and non-root resume mode")
    key = idempotency_key or turn_id
    ensure_schema(connection)
    with _write(connection):
        parent = None if parent_turn_id is None else get_runtime_turn(connection, parent_turn_id)
        if parent_turn_id is not None and parent is None:
            raise ValueError(f"runtime parent turn is unknown: {parent_turn_id}")
        if parent is not None and (parent.actor_id, parent.attempt_id) != (actor_id, attempt_id):
            raise ValueError("runtime parent must use the same actor and attempt")
        if parent is not None and parent.status == "running":
            raise ValueError("runtime parent must be terminal or unknown before resume")
        if parent is not None and parent.submit_started and not submit_started:
            raise ValueError("submit_started cannot be downgraded by a child turn")
        if parent is not None and submit_started:
            if (runtime_id, profile_id) != (parent.runtime_id, parent.profile_id):
                raise ValueError("runtime/profile cannot change when submit starts")
            required_mode = "receipt_only" if parent.submit_started else "resume"
            if resume_mode != required_mode:
                if parent.submit_started:
                    raise ValueError("post-submit child turns require receipt_only mode")
                raise ValueError("prepare-to-submit child turns require resume mode")
        existing = _turn(connection.execute(
            f"SELECT {_TURN_COLUMNS} FROM agent_runtime_turns "
            "WHERE turn_id=? OR idempotency_key=?", (turn_id, key)
        ).fetchone())
        identity = (
            turn_id, actor_id, attempt_id, parent_turn_id, checkpoint_id, runtime_id,
            profile_id, runtime_backend, model, provider_session_id, process_id,
            process_birth_time, resume_mode, int(submit_started), tool_surface_hash,
            prompt_contract_hash, key,
        )
        if existing is not None:
            persisted = (
                existing.turn_id, existing.actor_id, existing.attempt_id,
                existing.parent_turn_id, existing.checkpoint_id, existing.runtime_id,
                existing.profile_id, existing.runtime_backend, existing.model,
                existing.provider_session_id, existing.process_id,
                existing.process_birth_time, existing.resume_mode, existing.submit_started,
                existing.tool_surface_hash, existing.prompt_contract_hash,
                existing.idempotency_key,
            )
            if persisted != identity:
                raise ValueError(f"runtime turn idempotency collision: {key}")
            if require_new:
                raise RuntimeTurnConflictError(
                    f"runtime turn reservation already exists: {existing.turn_id}"
                )
            return existing
        running = connection.execute(
            "SELECT turn_id FROM agent_runtime_turns "
            "WHERE actor_id=? AND status='running' LIMIT 1",
            (actor_id,),
        ).fetchone()
        if running is not None:
            raise RuntimeTurnConflictError(
                f"actor already has running turn: {running[0]}"
            )
        try:
            connection.execute(
                f"INSERT INTO agent_runtime_turns({_TURN_COLUMNS}) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'running',?,NULL,NULL,NULL,?,?,?)",
                (
                    turn_id, actor_id, attempt_id, parent_turn_id, checkpoint_id,
                    runtime_id, profile_id, runtime_backend, model, provider_session_id,
                    process_id, process_birth_time, resume_mode, int(submit_started),
                    _iso(started_at or datetime.now(UTC)), tool_surface_hash,
                    prompt_contract_hash, key,
                ),
            )
        except sqlite3.IntegrityError as error:
            running = connection.execute(
                "SELECT turn_id FROM agent_runtime_turns "
                "WHERE actor_id=? AND status='running' LIMIT 1",
                (actor_id,),
            ).fetchone()
            if running is not None:
                raise RuntimeTurnConflictError(
                    f"actor already has running turn: {running[0]}"
                ) from error
            raise
        created = get_runtime_turn(connection, turn_id)
    assert created is not None
    return created


def attach_runtime_turn_process(
    connection: sqlite3.Connection,
    *,
    token: RuntimeTurnToken,
    process_id: int,
    process_birth_time: int,
) -> AgentRuntimeTurn:
    """Bind a reserved running turn to the exact spawned process using CAS."""
    _validate_runtime_token(token)
    _validate_process_identity(process_id, process_birth_time)
    ensure_schema(connection)
    with _write(connection):
        current = get_runtime_turn(connection, token.turn_id)
        if current is None:
            raise KeyError(f"runtime turn does not exist: {token.turn_id}")
        if _turn_identity(current) != _token_identity(token):
            raise RuntimeTurnConflictError(
                f"runtime turn identity changed: {token.turn_id}"
            )
        requested_process = (process_id, process_birth_time)
        current_process = (current.process_id, current.process_birth_time)
        if current.status != "running":
            raise RuntimeTurnConflictError(
                f"runtime turn is not running: {token.turn_id}"
            )
        if current_process == requested_process:
            if (token.process_id, token.process_birth_time) not in (
                (None, None),
                requested_process,
            ):
                raise RuntimeTurnConflictError(
                    f"runtime turn process token is stale: {token.turn_id}"
                )
            return current
        if current_process != (None, None) or (
            token.process_id,
            token.process_birth_time,
        ) != (None, None):
            raise RuntimeTurnConflictError(
                f"runtime turn already has a different process: {token.turn_id}"
            )
        cursor = connection.execute(
            "UPDATE agent_runtime_turns SET process_id=?,process_birth_time=? "
            "WHERE turn_id=? AND status='running' AND process_id IS NULL "
            "AND process_birth_time IS NULL AND actor_id=? AND attempt_id=? "
            "AND parent_turn_id IS ? AND checkpoint_id IS ? AND runtime_id=? "
            "AND profile_id=? AND runtime_backend=? AND model IS ? "
            "AND provider_session_id IS ? AND resume_mode=? AND submit_started=? "
            "AND started_at=? AND tool_surface_hash=? AND prompt_contract_hash=? "
            "AND idempotency_key=?",
            (
                process_id,
                process_birth_time,
                token.turn_id,
                token.actor_id,
                token.attempt_id,
                token.parent_turn_id,
                token.checkpoint_id,
                token.runtime_id,
                token.profile_id,
                token.runtime_backend,
                token.model,
                token.provider_session_id,
                token.resume_mode,
                token.submit_started,
                token.started_at,
                token.tool_surface_hash,
                token.prompt_contract_hash,
                token.idempotency_key,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeTurnConflictError(
                f"runtime process attachment CAS failed: {token.turn_id}"
            )
        attached = get_runtime_turn(connection, token.turn_id)
    assert attached is not None
    return attached


def mark_runtime_turn_terminal(
    connection: sqlite3.Connection, *, status: str,
    token: RuntimeTurnToken | None = None, turn_id: str | None = None,
    failure_code: str | None = None, exit_code: int | None = None,
    terminal_at: datetime | None = None,
) -> AgentRuntimeTurn:
    """CAS an exact running turn token to terminal; exact retries are idempotent.

    ``turn_id`` remains a compatibility path for already process-bound turns. A
    pre-spawn reservation has no process authority and must therefore supply its
    explicit NULL-process token when recording a synchronous spawn failure.
    """
    normalized = status.casefold()
    if normalized not in _TERMINAL:
        raise ValueError(f"unsupported runtime terminal status: {status}")
    if normalized in {"completed", "cancelled"} and failure_code is not None:
        raise ValueError(f"{normalized} runtime turns cannot carry failure_code")
    if token is None and turn_id is None:
        raise ValueError("runtime terminal transition requires token or turn_id")
    if token is not None:
        _validate_runtime_token(token)
        if turn_id is not None and turn_id != token.turn_id:
            raise ValueError("turn_id does not match runtime turn token")
        turn_id = token.turn_id
    assert turn_id is not None
    ensure_schema(connection)
    with _write(connection):
        current = get_runtime_turn(connection, turn_id)
        if current is None:
            raise KeyError(f"runtime turn does not exist: {turn_id}")
        if token is None:
            if current.process_id is None:
                raise ValueError(
                    "unbound runtime reservation requires its exact NULL-process token"
                )
            token = token_from_turn(current)
        if (
            _turn_identity(current) != _token_identity(token)
            or (current.process_id, current.process_birth_time)
            != (token.process_id, token.process_birth_time)
        ):
            raise RuntimeTurnConflictError(
                f"runtime terminal token is stale: {turn_id}"
            )
        if current.status != "running":
            if (current.status, current.failure_code, current.exit_code) == (
                normalized,
                failure_code,
                exit_code,
            ):
                return current
            raise RuntimeError(f"runtime turn already terminal as {current.status}: {turn_id}")
        cursor = connection.execute(
            "UPDATE agent_runtime_turns SET status=?,terminal_at=?,failure_code=?,exit_code=? "
            "WHERE turn_id=? AND status='running' AND actor_id=? AND attempt_id=? "
            "AND parent_turn_id IS ? AND checkpoint_id IS ? AND runtime_id=? "
            "AND profile_id=? AND runtime_backend=? AND model IS ? "
            "AND provider_session_id IS ? AND process_id IS ? "
            "AND process_birth_time IS ? AND resume_mode=? AND submit_started=? "
            "AND started_at=? AND tool_surface_hash=? AND prompt_contract_hash=? "
            "AND idempotency_key=?",
            (
                normalized,
                _iso(terminal_at or datetime.now(UTC)),
                failure_code,
                exit_code,
                turn_id,
                token.actor_id,
                token.attempt_id,
                token.parent_turn_id,
                token.checkpoint_id,
                token.runtime_id,
                token.profile_id,
                token.runtime_backend,
                token.model,
                token.provider_session_id,
                token.process_id,
                token.process_birth_time,
                token.resume_mode,
                token.submit_started,
                token.started_at,
                token.tool_surface_hash,
                token.prompt_contract_hash,
                token.idempotency_key,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeTurnConflictError(f"runtime terminal CAS failed: {turn_id}")
        terminal = get_runtime_turn(connection, turn_id)
    assert terminal is not None
    return terminal
