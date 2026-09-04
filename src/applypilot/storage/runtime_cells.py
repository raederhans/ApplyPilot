"""Durable, fail-closed Runtime Cell generations and application leases.

Expiry is observation, never takeover authority.  An expired open lease becomes
``suspect`` and continues to block its cell, application, actor, attempt and
exact hostname until the owning generation proves cleanup or is quarantined.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

RUNTIME_CELL_SCHEMA_VERSION = 3
_CELL_ACTIVE = {"active", "suspect", "draining"}
_LEASE_ACTIVE = {"open", "suspect", "draining"}


class RuntimeCellConflictError(RuntimeError):
    """A durable identity or exclusive lease is already owned."""


class StaleRuntimeCellTokenError(RuntimeError):
    """A mutation did not present the current exact lease token."""


class RuntimeCellQuarantinedError(RuntimeError):
    """A quarantined or closed generation cannot accept work."""


@dataclass(frozen=True, slots=True)
class RuntimeCellGeneration:
    cell_id: str
    generation: int
    runtime_id: str
    source_identity: str
    process_id: int
    process_birth_time: int
    status: str
    created_at: str
    updated_at: str
    quarantine_reason: str | None


@dataclass(frozen=True, slots=True)
class RuntimeCellLease:
    lease_id: str
    cell_id: str
    generation: int
    runtime_id: str
    application_id: str
    actor_id: str
    attempt_id: str
    hostname: str
    lease_epoch: int
    status: str
    heartbeat_at: str
    expires_at: str
    created_at: str
    released_at: str | None
    terminal_reason: str | None


@dataclass(frozen=True, slots=True)
class RuntimeCellLeaseToken:
    lease_id: str
    cell_id: str
    generation: int
    runtime_id: str
    application_id: str
    actor_id: str
    attempt_id: str
    hostname: str
    lease_epoch: int


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime cell timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _required(value: object, name: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"{name} is required")
    return text


def normalize_hostname(value: object) -> str:
    """Return one exact, lower-case hostname without accepting URL syntax."""

    host = _required(value, "hostname").casefold().rstrip(".")
    if any(item in host for item in ("/", "@", ":")) or host.startswith("."):
        raise ValueError("hostname must be an exact hostname")
    labels = host.split(".")
    if any(not label or len(label) > 63 for label in labels):
        raise ValueError("hostname must be an exact hostname")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    if any(set(label) - allowed or label.startswith("-") or label.endswith("-") for label in labels):
        raise ValueError("hostname must be an exact hostname")
    return host


@contextmanager
def _write(connection: sqlite3.Connection, name: str) -> Iterator[None]:
    owns = not connection.in_transaction
    if owns:
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.execute(f"SAVEPOINT {name}")
    try:
        yield
        if owns:
            connection.commit()
        else:
            connection.execute(f"RELEASE SAVEPOINT {name}")
    except Exception:
        if owns and connection.in_transaction:
            connection.rollback()
        elif not owns:
            connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            connection.execute(f"RELEASE SAVEPOINT {name}")
        raise


def _migration_v1(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE runtime_cell_generations (
            cell_id TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation > 0),
            runtime_id TEXT NOT NULL UNIQUE,
            source_identity TEXT NOT NULL,
            process_id INTEGER NOT NULL CHECK(process_id > 0),
            process_birth_time INTEGER NOT NULL CHECK(process_birth_time > 0),
            status TEXT NOT NULL CHECK(status IN
                ('active','suspect','draining','quarantined','closed')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            quarantine_reason TEXT,
            PRIMARY KEY(cell_id, generation))""",
        """CREATE UNIQUE INDEX idx_runtime_cell_one_live_generation
            ON runtime_cell_generations(cell_id)
            WHERE status IN ('active','suspect','draining')""",
        """CREATE UNIQUE INDEX idx_runtime_cell_process_identity
            ON runtime_cell_generations(process_id, process_birth_time)
            WHERE status IN ('active','suspect','draining')""",
        """CREATE TABLE runtime_cell_leases (
            lease_id TEXT PRIMARY KEY,
            cell_id TEXT NOT NULL, generation INTEGER NOT NULL,
            runtime_id TEXT NOT NULL,
            application_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL, hostname TEXT NOT NULL,
            lease_epoch INTEGER NOT NULL CHECK(lease_epoch > 0),
            status TEXT NOT NULL CHECK(status IN
                ('open','suspect','draining','released','quarantined')),
            heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL, released_at TEXT, terminal_reason TEXT,
            FOREIGN KEY(cell_id, generation)
                REFERENCES runtime_cell_generations(cell_id, generation))""",
        """CREATE UNIQUE INDEX idx_runtime_cell_one_open_cell_generation
            ON runtime_cell_leases(cell_id, generation)
            WHERE status IN ('open','suspect','draining')""",
        """CREATE UNIQUE INDEX idx_runtime_cell_one_open_application
            ON runtime_cell_leases(application_id)
            WHERE status IN ('open','suspect','draining')""",
        """CREATE UNIQUE INDEX idx_runtime_cell_one_open_actor
            ON runtime_cell_leases(actor_id)
            WHERE status IN ('open','suspect','draining')""",
        """CREATE UNIQUE INDEX idx_runtime_cell_one_open_attempt
            ON runtime_cell_leases(attempt_id)
            WHERE status IN ('open','suspect','draining')""",
        """CREATE UNIQUE INDEX idx_runtime_cell_one_open_hostname
            ON runtime_cell_leases(hostname)
            WHERE status IN ('open','suspect','draining')""",
        """CREATE INDEX idx_runtime_cell_lease_generation
            ON runtime_cell_leases(cell_id, generation, status, expires_at)""",
    )
    for statement in statements:
        connection.execute(statement)


def _migration_v2(connection: sqlite3.Connection) -> None:
    """Backfill a permanent identity registry without rewriting v1 history."""

    connection.execute(
        """CREATE TABLE runtime_cell_process_identities (
            process_id INTEGER NOT NULL CHECK(process_id > 0),
            process_birth_time INTEGER NOT NULL CHECK(process_birth_time > 0),
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY(process_id, process_birth_time))"""
    )
    connection.execute(
        """INSERT INTO runtime_cell_process_identities(
            process_id,process_birth_time,first_seen_at)
        SELECT process_id,process_birth_time,MIN(created_at)
        FROM runtime_cell_generations
        GROUP BY process_id,process_birth_time"""
    )


def _migration_v3(connection: sqlite3.Connection) -> None:
    """Index the global open-lease expiry scan by its filtering order."""

    connection.execute(
        """CREATE INDEX idx_runtime_cell_open_lease_expiry
        ON runtime_cell_leases(status, expires_at, cell_id, generation)"""
    )


_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migration_v1,
    _migration_v2,
    _migration_v3,
)


def ensure_schema(connection: sqlite3.Connection) -> int:
    with _write(connection, "runtime_cell_migration"):
        connection.execute(
            """CREATE TABLE IF NOT EXISTS runtime_cell_schema_version (
                component TEXT PRIMARY KEY CHECK(component='runtime_cells'),
                version INTEGER NOT NULL CHECK(version >= 0), updated_at TEXT NOT NULL)"""
        )
        row = connection.execute(
            "SELECT version FROM runtime_cell_schema_version WHERE component='runtime_cells'"
        ).fetchone()
        current = 0 if row is None else int(row[0])
        if current > len(_MIGRATIONS):
            raise RuntimeError(f"runtime cell schema {current} is newer than supported {len(_MIGRATIONS)}")
        for version in range(current + 1, len(_MIGRATIONS) + 1):
            _MIGRATIONS[version - 1](connection)
            connection.execute(
                "INSERT INTO runtime_cell_schema_version VALUES('runtime_cells',?,?) "
                "ON CONFLICT(component) DO UPDATE SET version=excluded.version,"
                "updated_at=excluded.updated_at",
                (version, _iso(datetime.now(UTC))),
            )
    return len(_MIGRATIONS)


_GEN_COLUMNS = (
    "cell_id,generation,runtime_id,source_identity,process_id,process_birth_time,"
    "status,created_at,updated_at,quarantine_reason"
)
_LEASE_COLUMNS = (
    "lease_id,cell_id,generation,runtime_id,application_id,actor_id,attempt_id,"
    "hostname,lease_epoch,status,heartbeat_at,expires_at,created_at,released_at,"
    "terminal_reason"
)


def _generation(row: object) -> RuntimeCellGeneration | None:
    return None if row is None else RuntimeCellGeneration(*tuple(row))  # type: ignore[arg-type]


def _lease(row: object) -> RuntimeCellLease | None:
    return None if row is None else RuntimeCellLease(*tuple(row))  # type: ignore[arg-type]


def get_generation(connection: sqlite3.Connection, cell_id: str, generation: int) -> RuntimeCellGeneration | None:
    ensure_schema(connection)
    return _generation(
        connection.execute(
            f"SELECT {_GEN_COLUMNS} FROM runtime_cell_generations WHERE cell_id=? AND generation=?",
            (cell_id, generation),
        ).fetchone()
    )


def register_generation(
    connection: sqlite3.Connection,
    *,
    cell_id: str,
    generation: int,
    runtime_id: str,
    source_identity: str,
    process_id: int,
    process_birth_time: int,
    now: datetime | None = None,
) -> RuntimeCellGeneration:
    cell_id = _required(cell_id, "cell_id")
    runtime_id = _required(runtime_id, "runtime_id")
    source_identity = _required(source_identity, "source_identity")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("generation must be a positive integer")
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (process_id, process_birth_time)):
        raise ValueError("process identity must contain positive integers")
    ensure_schema(connection)
    instant = _iso(now or datetime.now(UTC))
    with _write(connection, "runtime_cell_register"):
        existing = _generation(
            connection.execute(
                f"SELECT {_GEN_COLUMNS} FROM runtime_cell_generations WHERE cell_id=? AND generation=?",
                (cell_id, generation),
            ).fetchone()
        )
        if existing is not None:
            identity = (
                existing.runtime_id,
                existing.source_identity,
                existing.process_id,
                existing.process_birth_time,
            )
            if identity != (runtime_id, source_identity, process_id, process_birth_time):
                raise RuntimeCellConflictError("runtime cell generation identity cannot be reused")
            if existing.status not in _CELL_ACTIVE:
                raise RuntimeCellQuarantinedError("runtime cell generation is terminal")
            return existing
        try:
            connection.execute(
                "INSERT INTO runtime_cell_process_identities "
                "(process_id,process_birth_time,first_seen_at) VALUES(?,?,?)",
                (process_id, process_birth_time, instant),
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeCellConflictError("runtime process identity was already used by a Cell generation") from exc
        try:
            connection.execute(
                f"INSERT INTO runtime_cell_generations({_GEN_COLUMNS}) VALUES(?,?,?,?,?,?,'active',?,?,NULL)",
                (
                    cell_id,
                    generation,
                    runtime_id,
                    source_identity,
                    process_id,
                    process_birth_time,
                    instant,
                    instant,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeCellConflictError("runtime cell generation identity is already live") from exc
        created = get_generation(connection, cell_id, generation)
    assert created is not None
    return created


def _mark_expired_suspect(connection: sqlite3.Connection, *, now_text: str, cell_id: str | None = None) -> int:
    suffix = " AND cell_id=?" if cell_id is not None else ""
    params: tuple[object, ...] = (now_text, now_text)
    if cell_id is not None:
        params += (cell_id,)
    rows = connection.execute(
        "SELECT DISTINCT cell_id,generation FROM runtime_cell_leases WHERE status='open' AND expires_at<=?" + suffix,
        ((now_text, cell_id) if cell_id is not None else (now_text,)),
    ).fetchall()
    cursor = connection.execute(
        "UPDATE runtime_cell_leases SET status='suspect',terminal_reason='ttl_expired_suspect',"
        "heartbeat_at=? WHERE status='open' AND expires_at<=?" + suffix,
        params,
    )
    for candidate_cell, candidate_generation in rows:
        connection.execute(
            "UPDATE runtime_cell_generations SET status='suspect',updated_at=? "
            "WHERE cell_id=? AND generation=? AND status='active'",
            (now_text, candidate_cell, candidate_generation),
        )
    return cursor.rowcount


def mark_expired_suspect(connection: sqlite3.Connection, *, now: datetime | None = None) -> int:
    ensure_schema(connection)
    with _write(connection, "runtime_cell_expiry"):
        return _mark_expired_suspect(connection, now_text=_iso(now or datetime.now(UTC)))


def claim_lease(
    connection: sqlite3.Connection,
    *,
    lease_id: str,
    cell_id: str,
    generation: int,
    runtime_id: str,
    source_identity: str,
    application_id: str,
    actor_id: str,
    attempt_id: str,
    hostname: str,
    ttl_seconds: int = 300,
    expected_latest_epoch: int | None = None,
    now: datetime | None = None,
) -> RuntimeCellLease:
    """Claim all cell/application/domain identities in the caller transaction."""

    values = {
        "lease_id": lease_id,
        "cell_id": cell_id,
        "runtime_id": runtime_id,
        "source_identity": source_identity,
        "application_id": application_id,
        "actor_id": actor_id,
        "attempt_id": attempt_id,
    }
    values = {name: _required(value, name) for name, value in values.items()}
    hostname = normalize_hostname(hostname)
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
        raise ValueError("ttl_seconds must be a positive integer")
    ensure_schema(connection)
    instant = now or datetime.now(UTC)
    now_text = _iso(instant)
    with _write(connection, "runtime_cell_claim"):
        _mark_expired_suspect(connection, now_text=now_text)
        cell = _generation(
            connection.execute(
                f"SELECT {_GEN_COLUMNS} FROM runtime_cell_generations WHERE cell_id=? AND generation=?",
                (values["cell_id"], generation),
            ).fetchone()
        )
        if cell is None:
            raise KeyError("runtime cell generation does not exist")
        if cell.status != "active":
            raise RuntimeCellQuarantinedError(f"runtime cell generation cannot claim while {cell.status}")
        if (cell.runtime_id, cell.source_identity) != (
            values["runtime_id"],
            values["source_identity"],
        ):
            raise RuntimeCellConflictError("runtime/source identity does not match generation")
        replay = _lease(
            connection.execute(
                f"SELECT {_LEASE_COLUMNS} FROM runtime_cell_leases WHERE lease_id=?",
                (values["lease_id"],),
            ).fetchone()
        )
        requested = (
            values["cell_id"],
            generation,
            values["runtime_id"],
            values["application_id"],
            values["actor_id"],
            values["attempt_id"],
            hostname,
        )
        if replay is not None:
            persisted = (
                replay.cell_id,
                replay.generation,
                replay.runtime_id,
                replay.application_id,
                replay.actor_id,
                replay.attempt_id,
                replay.hostname,
            )
            if persisted != requested:
                raise RuntimeCellConflictError("lease_id collision")
            if replay.status != "open":
                raise RuntimeCellConflictError("lease replay is not open")
            if expected_latest_epoch is not None and expected_latest_epoch != replay.lease_epoch - 1:
                raise StaleRuntimeCellTokenError("stale expected lease epoch")
            return replay
        latest = int(
            connection.execute(
                "SELECT COALESCE(MAX(lease_epoch),0) FROM runtime_cell_leases WHERE cell_id=? AND generation=?",
                (values["cell_id"], generation),
            ).fetchone()[0]
        )
        if expected_latest_epoch is not None and expected_latest_epoch != latest:
            raise StaleRuntimeCellTokenError("stale expected lease epoch")
        try:
            connection.execute(
                f"INSERT INTO runtime_cell_leases({_LEASE_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,'open',?,?,?,NULL,NULL)",
                (
                    values["lease_id"],
                    values["cell_id"],
                    generation,
                    values["runtime_id"],
                    values["application_id"],
                    values["actor_id"],
                    values["attempt_id"],
                    hostname,
                    latest + 1,
                    now_text,
                    _iso(instant + timedelta(seconds=ttl_seconds)),
                    now_text,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeCellConflictError(
                "cell, application, actor, attempt, or hostname already has a live lease"
            ) from exc
        claimed = _lease(
            connection.execute(
                f"SELECT {_LEASE_COLUMNS} FROM runtime_cell_leases WHERE lease_id=?",
                (values["lease_id"],),
            ).fetchone()
        )
    assert claimed is not None
    return claimed


def token_from_lease(lease: RuntimeCellLease) -> RuntimeCellLeaseToken:
    return RuntimeCellLeaseToken(
        lease.lease_id,
        lease.cell_id,
        lease.generation,
        lease.runtime_id,
        lease.application_id,
        lease.actor_id,
        lease.attempt_id,
        lease.hostname,
        lease.lease_epoch,
    )


def _token_where(token: RuntimeCellLeaseToken) -> tuple[object, ...]:
    return (
        token.lease_id,
        token.cell_id,
        token.generation,
        token.runtime_id,
        token.application_id,
        token.actor_id,
        token.attempt_id,
        token.hostname,
        token.lease_epoch,
    )


def heartbeat_lease(
    connection: sqlite3.Connection,
    token: RuntimeCellLeaseToken,
    *,
    ttl_seconds: int = 300,
    now: datetime | None = None,
) -> RuntimeCellLease:
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be positive")
    ensure_schema(connection)
    instant = now or datetime.now(UTC)
    with _write(connection, "runtime_cell_heartbeat"):
        _mark_expired_suspect(connection, now_text=_iso(instant))
        cursor = connection.execute(
            "UPDATE runtime_cell_leases SET heartbeat_at=?,expires_at=? "
            "WHERE lease_id=? AND cell_id=? AND generation=? AND runtime_id=? "
            "AND application_id=? AND actor_id=? AND attempt_id=? AND hostname=? "
            "AND lease_epoch=? AND status='open'",
            (_iso(instant), _iso(instant + timedelta(seconds=ttl_seconds)), *_token_where(token)),
        )
        if cursor.rowcount != 1:
            raise StaleRuntimeCellTokenError("runtime cell heartbeat token is stale")
        lease = _lease(
            connection.execute(
                f"SELECT {_LEASE_COLUMNS} FROM runtime_cell_leases WHERE lease_id=?",
                (token.lease_id,),
            ).fetchone()
        )
    assert lease is not None
    return lease


def begin_drain(
    connection: sqlite3.Connection,
    token: RuntimeCellLeaseToken,
    *,
    reason: str,
    now: datetime | None = None,
) -> RuntimeCellLease:
    reason = _required(reason, "reason")
    ensure_schema(connection)
    now_text = _iso(now or datetime.now(UTC))
    with _write(connection, "runtime_cell_drain"):
        cursor = connection.execute(
            "UPDATE runtime_cell_leases SET status='draining',terminal_reason=?,heartbeat_at=? "
            "WHERE lease_id=? AND cell_id=? AND generation=? AND runtime_id=? "
            "AND application_id=? AND actor_id=? AND attempt_id=? AND hostname=? "
            "AND lease_epoch=? AND status IN ('open','suspect')",
            (reason, now_text, *_token_where(token)),
        )
        if cursor.rowcount != 1:
            raise StaleRuntimeCellTokenError("runtime cell drain token is stale")
        connection.execute(
            "UPDATE runtime_cell_generations SET status='draining',updated_at=? "
            "WHERE cell_id=? AND generation=? AND runtime_id=? "
            "AND status IN ('active','suspect')",
            (now_text, token.cell_id, token.generation, token.runtime_id),
        )
        lease = _lease(
            connection.execute(
                f"SELECT {_LEASE_COLUMNS} FROM runtime_cell_leases WHERE lease_id=?",
                (token.lease_id,),
            ).fetchone()
        )
    assert lease is not None
    return lease


def release_after_cleanup(
    connection: sqlite3.Connection,
    token: RuntimeCellLeaseToken,
    *,
    agent_stopped: bool,
    context_cleanup_verified: bool,
    residual_resources: int | None,
    now: datetime | None = None,
) -> RuntimeCellLease:
    """Release only after Agent stop and readable zero-residual verification."""

    if not agent_stopped or not context_cleanup_verified or residual_resources != 0:
        quarantine_after_cleanup_failure(
            connection,
            token,
            reason="cleanup_unverified_or_residual",
            now=now,
        )
        raise RuntimeCellQuarantinedError("runtime cell cleanup was not proven zero-residual")
    ensure_schema(connection)
    now_text = _iso(now or datetime.now(UTC))
    with _write(connection, "runtime_cell_release"):
        cursor = connection.execute(
            "UPDATE runtime_cell_leases SET status='released',released_at=?,"
            "terminal_reason='clean_release' WHERE lease_id=? AND cell_id=? AND generation=? "
            "AND runtime_id=? AND application_id=? AND actor_id=? AND attempt_id=? "
            "AND hostname=? AND lease_epoch=? AND status IN ('open','draining')",
            (now_text, *_token_where(token)),
        )
        if cursor.rowcount != 1:
            raise StaleRuntimeCellTokenError("runtime cell release token is stale")
        connection.execute(
            "UPDATE runtime_cell_generations SET status='active',updated_at=? "
            "WHERE cell_id=? AND generation=? AND runtime_id=? AND status='draining'",
            (now_text, token.cell_id, token.generation, token.runtime_id),
        )
        lease = _lease(
            connection.execute(
                f"SELECT {_LEASE_COLUMNS} FROM runtime_cell_leases WHERE lease_id=?",
                (token.lease_id,),
            ).fetchone()
        )
    assert lease is not None
    return lease


def quarantine_after_cleanup_failure(
    connection: sqlite3.Connection,
    token: RuntimeCellLeaseToken,
    *,
    reason: str,
    now: datetime | None = None,
) -> RuntimeCellLease:
    """Quarantine only if the complete token still owns the current live lease."""

    reason = _required(reason, "reason")
    ensure_schema(connection)
    now_text = _iso(now or datetime.now(UTC))
    with _write(connection, "runtime_cell_cleanup_quarantine"):
        current = _lease(
            connection.execute(
                f"SELECT {_LEASE_COLUMNS} FROM runtime_cell_leases WHERE lease_id=? "
                "AND cell_id=? AND generation=? AND runtime_id=? AND application_id=? "
                "AND actor_id=? AND attempt_id=? AND hostname=? AND lease_epoch=? "
                "AND status IN ('open','suspect','draining')",
                _token_where(token),
            ).fetchone()
        )
        if current is None:
            raise StaleRuntimeCellTokenError("cleanup failure token does not own the current live lease")
        cursor = connection.execute(
            "UPDATE runtime_cell_generations SET status='quarantined',updated_at=?,"
            "quarantine_reason=? WHERE cell_id=? AND generation=? AND runtime_id=? "
            "AND status IN ('active','suspect','draining')",
            (now_text, reason, token.cell_id, token.generation, token.runtime_id),
        )
        if cursor.rowcount != 1:
            raise StaleRuntimeCellTokenError("cleanup failure token does not own the current generation")
        cursor = connection.execute(
            "UPDATE runtime_cell_leases SET status='quarantined',released_at=?,"
            "terminal_reason=? WHERE lease_id=? AND cell_id=? AND generation=? "
            "AND runtime_id=? AND application_id=? AND actor_id=? AND attempt_id=? "
            "AND hostname=? AND lease_epoch=? AND status IN ('open','suspect','draining')",
            (now_text, reason, *_token_where(token)),
        )
        if cursor.rowcount != 1:
            raise StaleRuntimeCellTokenError("cleanup quarantine lease CAS failed")
        quarantined = _lease(
            connection.execute(
                f"SELECT {_LEASE_COLUMNS} FROM runtime_cell_leases WHERE lease_id=?",
                (token.lease_id,),
            ).fetchone()
        )
    assert quarantined is not None
    return quarantined


__all__ = [
    "RUNTIME_CELL_SCHEMA_VERSION",
    "RuntimeCellConflictError",
    "RuntimeCellGeneration",
    "RuntimeCellLease",
    "RuntimeCellLeaseToken",
    "RuntimeCellQuarantinedError",
    "StaleRuntimeCellTokenError",
    "begin_drain",
    "claim_lease",
    "ensure_schema",
    "get_generation",
    "heartbeat_lease",
    "mark_expired_suspect",
    "normalize_hostname",
    "quarantine_after_cleanup_failure",
    "register_generation",
    "release_after_cleanup",
    "token_from_lease",
]
