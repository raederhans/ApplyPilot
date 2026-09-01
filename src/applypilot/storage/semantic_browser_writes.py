"""Durable, metadata-only journal for bounded semantic browser writes.

The journal never stores local paths, raw field values, DOM content, or write
authority.  It records only immutable operation claims and a small CAS state
machine so crash recovery can observe before deciding whether a write may be
replayed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

OperationState = Literal[
    "started",
    "effect_observed",
    "verified",
    "failed_no_effect",
    "parked_side_effect_unknown",
    "parked_stale_after_effect",
]

SCHEMA_VERSION = 1
OPERATION_KIND = "upload_bound_artifact"
_PROVIDERS = {"workday", "smartrecruiters"}
_STATES = {
    "started",
    "effect_observed",
    "verified",
    "failed_no_effect",
    "parked_side_effect_unknown",
    "parked_stale_after_effect",
}
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_REASON_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,119}")


class SemanticWriteJournalError(RuntimeError):
    """Base class for journal contract violations."""


class SemanticWriteCollision(SemanticWriteJournalError):
    """An operation identity was reused with different immutable claims."""


class SemanticWriteTransitionError(SemanticWriteJournalError):
    """A state transition lost its exact expected-state CAS."""


@dataclass(frozen=True, slots=True)
class SemanticWriteClaims:
    operation_id: str
    operation_digest: str
    actor_id: str
    attempt_id: str
    provider: str
    operation_kind: str
    adapter_version: str
    application_binding_hash: str
    page_id: str
    page_lease_id: str
    page_lease_epoch: int
    expected_page_epoch: int
    artifact_sha256: str
    artifact_size: int
    material_binding_hash: str
    policy_contract_version: str
    policy_digest: str
    expected_postcondition_digest: str

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "actor_id",
            "attempt_id",
            "page_id",
            "page_lease_id",
            "adapter_version",
            "policy_contract_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        for name in (
            "operation_digest",
            "artifact_sha256",
            "application_binding_hash",
            "material_binding_hash",
            "policy_digest",
            "expected_postcondition_digest",
        ):
            if not _DIGEST_RE.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.provider not in _PROVIDERS:
            raise ValueError(f"unsupported semantic provider: {self.provider}")
        if self.operation_kind != OPERATION_KIND:
            raise ValueError(f"unsupported semantic operation: {self.operation_kind}")
        if isinstance(self.page_lease_epoch, bool) or self.page_lease_epoch < 1:
            raise ValueError("page_lease_epoch must be a positive integer")
        if isinstance(self.expected_page_epoch, bool) or self.expected_page_epoch < 0:
            raise ValueError("expected_page_epoch must be a non-negative integer")
        if isinstance(self.artifact_size, bool) or self.artifact_size < 0:
            raise ValueError("artifact_size must be a non-negative integer")

    @property
    def claims_digest(self) -> str:
        encoded = json.dumps(
            asdict(self),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticWriteRecord:
    operation_id: str
    operation_digest: str
    claims_digest: str
    actor_id: str
    attempt_id: str
    provider: str
    operation_kind: str
    adapter_version: str
    application_binding_hash: str
    page_id: str
    page_lease_id: str
    page_lease_epoch: int
    expected_page_epoch: int
    artifact_sha256: str
    artifact_size: int
    material_binding_hash: str
    policy_contract_version: str
    policy_digest: str
    expected_postcondition_digest: str
    state: str
    dispatch_count: int
    effect_observed: bool
    resulting_page_epoch: int | None
    reason_code: str | None
    created_at: str
    updated_at: str


_COLUMNS = (
    "operation_id,operation_digest,claims_digest,actor_id,attempt_id,provider,"
    "operation_kind,adapter_version,application_binding_hash,page_id,page_lease_id,"
    "page_lease_epoch,expected_page_epoch,"
    "artifact_sha256,artifact_size,material_binding_hash,policy_contract_version,"
    "policy_digest,expected_postcondition_digest,state,dispatch_count,"
    "effect_observed,resulting_page_epoch,"
    "reason_code,created_at,updated_at"
)


def _record(row: sqlite3.Row | tuple[object, ...] | None) -> SemanticWriteRecord | None:
    if row is None:
        return None
    values = list(row)
    values[21] = bool(values[21])
    return SemanticWriteRecord(*values)


def _iso(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("semantic write timestamps must be timezone-aware")
    return current.astimezone(UTC).isoformat()


def _reason(value: str) -> str:
    normalized = value.strip().casefold()
    if not _REASON_RE.fullmatch(normalized):
        raise ValueError("reason_code must be a bounded machine-readable code")
    return normalized


@contextmanager
def _migration(connection: sqlite3.Connection) -> Iterator[None]:
    nested = connection.in_transaction
    if nested:
        connection.execute("SAVEPOINT semantic_write_migration")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            connection.execute("RELEASE SAVEPOINT semantic_write_migration")
        else:
            connection.commit()
    except Exception:
        if nested:
            connection.execute("ROLLBACK TO SAVEPOINT semantic_write_migration")
            connection.execute("RELEASE SAVEPOINT semantic_write_migration")
        elif connection.in_transaction:
            connection.rollback()
        raise


@contextmanager
def _write(connection: sqlite3.Connection) -> Iterator[None]:
    nested = connection.in_transaction
    if nested:
        connection.execute("SAVEPOINT semantic_write_cas")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            connection.execute("RELEASE SAVEPOINT semantic_write_cas")
        else:
            connection.commit()
    except Exception:
        if nested:
            connection.execute("ROLLBACK TO SAVEPOINT semantic_write_cas")
            connection.execute("RELEASE SAVEPOINT semantic_write_cas")
        elif connection.in_transaction:
            connection.rollback()
        raise


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Install the additive version-1 journal without owning caller work."""

    with _migration(connection):
        connection.execute(
            """CREATE TABLE IF NOT EXISTS semantic_browser_write_schema (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL CHECK(version > 0)
            )"""
        )
        row = connection.execute(
            "SELECT version FROM semantic_browser_write_schema WHERE component=?",
            ("semantic_browser_writes",),
        ).fetchone()
        if row is not None and int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported semantic browser write schema version: {row[0]}"
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS semantic_browser_writes (
                operation_id TEXT PRIMARY KEY,
                operation_digest TEXT NOT NULL UNIQUE,
                claims_digest TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                provider TEXT NOT NULL CHECK(provider IN ('workday','smartrecruiters')),
                operation_kind TEXT NOT NULL
                    CHECK(operation_kind='upload_bound_artifact'),
                adapter_version TEXT NOT NULL,
                application_binding_hash TEXT NOT NULL,
                page_id TEXT NOT NULL,
                page_lease_id TEXT NOT NULL,
                page_lease_epoch INTEGER NOT NULL CHECK(page_lease_epoch > 0),
                expected_page_epoch INTEGER NOT NULL CHECK(expected_page_epoch >= 0),
                artifact_sha256 TEXT NOT NULL,
                artifact_size INTEGER NOT NULL CHECK(artifact_size >= 0),
                material_binding_hash TEXT NOT NULL,
                policy_contract_version TEXT NOT NULL,
                policy_digest TEXT NOT NULL,
                expected_postcondition_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'started','effect_observed','verified','failed_no_effect',
                    'parked_side_effect_unknown','parked_stale_after_effect'
                )),
                dispatch_count INTEGER NOT NULL DEFAULT 0
                    CHECK(dispatch_count BETWEEN 0 AND 2),
                effect_observed INTEGER NOT NULL DEFAULT 0
                    CHECK(effect_observed IN (0,1)),
                resulting_page_epoch INTEGER,
                reason_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(resulting_page_epoch IS NULL OR resulting_page_epoch >= 0)
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_semantic_browser_writes_attempt
            ON semantic_browser_writes(attempt_id, created_at)"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO semantic_browser_write_schema(component,version)
            VALUES('semantic_browser_writes', ?)""",
            (SCHEMA_VERSION,),
        )


def get_operation(
    connection: sqlite3.Connection,
    operation_id: str,
) -> SemanticWriteRecord | None:
    ensure_schema(connection)
    return _record(
        connection.execute(
            f"SELECT {_COLUMNS} FROM semantic_browser_writes WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    )


def get_operation_by_digest(
    connection: sqlite3.Connection,
    operation_digest: str,
) -> SemanticWriteRecord | None:
    ensure_schema(connection)
    return _record(
        connection.execute(
            f"SELECT {_COLUMNS} FROM semantic_browser_writes WHERE operation_digest=?",
            (operation_digest,),
        ).fetchone()
    )


def list_attempt_operations(
    connection: sqlite3.Connection,
    attempt_id: str,
) -> list[SemanticWriteRecord]:
    ensure_schema(connection)
    return [
        value
        for row in connection.execute(
            f"SELECT {_COLUMNS} FROM semantic_browser_writes "
            "WHERE attempt_id=? ORDER BY created_at,operation_id",
            (attempt_id,),
        ).fetchall()
        if (value := _record(row)) is not None
    ]


def begin_operation(
    connection: sqlite3.Connection,
    claims: SemanticWriteClaims,
    *,
    now: datetime | None = None,
) -> SemanticWriteRecord:
    """Insert one immutable operation or return its exact durable replay."""

    ensure_schema(connection)
    timestamp = _iso(now)
    with _write(connection):
        try:
            connection.execute(
                "INSERT INTO semantic_browser_writes("
                f"{_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,NULL,NULL,?,?)",
                (
                    claims.operation_id,
                    claims.operation_digest,
                    claims.claims_digest,
                    claims.actor_id,
                    claims.attempt_id,
                    claims.provider,
                    claims.operation_kind,
                    claims.adapter_version,
                    claims.application_binding_hash,
                    claims.page_id,
                    claims.page_lease_id,
                    claims.page_lease_epoch,
                    claims.expected_page_epoch,
                    claims.artifact_sha256,
                    claims.artifact_size,
                    claims.material_binding_hash,
                    claims.policy_contract_version,
                    claims.policy_digest,
                    claims.expected_postcondition_digest,
                    "started",
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            existing = _record(
                connection.execute(
                    f"SELECT {_COLUMNS} FROM semantic_browser_writes "
                    "WHERE operation_id=? OR operation_digest=?",
                    (claims.operation_id, claims.operation_digest),
                ).fetchone()
            )
            if (
                existing is None
                or existing.operation_id != claims.operation_id
                or existing.operation_digest != claims.operation_digest
                or existing.claims_digest != claims.claims_digest
            ):
                raise SemanticWriteCollision(
                    "semantic operation identity was reused with different claims"
                ) from None
            return existing
        created = _record(
            connection.execute(
                f"SELECT {_COLUMNS} FROM semantic_browser_writes WHERE operation_id=?",
                (claims.operation_id,),
            ).fetchone()
        )
    assert created is not None
    return created


def claim_dispatch(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    expected_dispatch_count: int,
    allow_replay: bool = False,
    now: datetime | None = None,
) -> SemanticWriteRecord | None:
    """Claim the initial dispatch or one explicitly observed no-effect replay."""

    ensure_schema(connection)
    if expected_dispatch_count not in (0, 1):
        raise ValueError("expected_dispatch_count must be 0 or 1")
    if expected_dispatch_count == 0 and allow_replay:
        raise ValueError("initial dispatch cannot be marked as a replay")
    if expected_dispatch_count == 1 and not allow_replay:
        raise ValueError("second dispatch requires explicit bounded replay")
    allowed_states = (
        ("started",)
        if expected_dispatch_count == 0
        else ("failed_no_effect",)
    )
    placeholders = ",".join("?" for _ in allowed_states)
    with _write(connection):
        cursor = connection.execute(
            "UPDATE semantic_browser_writes SET state='started',"
            "dispatch_count=dispatch_count+1,reason_code=NULL,updated_at=? "
            f"WHERE operation_id=? AND dispatch_count=? AND state IN ({placeholders})",
            (_iso(now), operation_id, expected_dispatch_count, *allowed_states),
        )
        if cursor.rowcount != 1:
            return None
        claimed = _record(
            connection.execute(
                f"SELECT {_COLUMNS} FROM semantic_browser_writes WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        )
    return claimed


def _transition(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    expected_state: str,
    target_state: str,
    effect_observed: bool,
    resulting_page_epoch: int | None,
    reason_code: str | None,
    now: datetime | None,
) -> SemanticWriteRecord:
    if expected_state not in _STATES or target_state not in _STATES:
        raise ValueError("unsupported semantic write state")
    ensure_schema(connection)
    normalized_reason = None if reason_code is None else _reason(reason_code)
    with _write(connection):
        cursor = connection.execute(
            "UPDATE semantic_browser_writes SET state=?,effect_observed=?,"
            "resulting_page_epoch=?,reason_code=?,updated_at=? "
            "WHERE operation_id=? AND state=?",
            (
                target_state,
                int(effect_observed),
                resulting_page_epoch,
                normalized_reason,
                _iso(now),
                operation_id,
                expected_state,
            ),
        )
        if cursor.rowcount != 1:
            current = get_operation(connection, operation_id)
            if (
                current is not None
                and current.state == target_state
                and current.effect_observed is effect_observed
                and current.resulting_page_epoch == resulting_page_epoch
                and current.reason_code == normalized_reason
            ):
                return current
            raise SemanticWriteTransitionError(
                f"semantic write transition lost CAS: {expected_state}->{target_state}"
            )
        transitioned = _record(
            connection.execute(
                f"SELECT {_COLUMNS} FROM semantic_browser_writes WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        )
    assert transitioned is not None
    return transitioned


def mark_effect_observed(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    now: datetime | None = None,
) -> SemanticWriteRecord:
    return _transition(
        connection,
        operation_id,
        expected_state="started",
        target_state="effect_observed",
        effect_observed=True,
        resulting_page_epoch=None,
        reason_code=None,
        now=now,
    )


def mark_verified(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    resulting_page_epoch: int,
    now: datetime | None = None,
) -> SemanticWriteRecord:
    current = get_operation(connection, operation_id)
    if current is None:
        raise KeyError(f"semantic operation does not exist: {operation_id}")
    if resulting_page_epoch != current.expected_page_epoch + 1:
        raise ValueError("verified result epoch must equal expected page epoch plus one")
    return _transition(
        connection,
        operation_id,
        expected_state="effect_observed",
        target_state="verified",
        effect_observed=True,
        resulting_page_epoch=resulting_page_epoch,
        reason_code=None,
        now=now,
    )


def mark_failed_no_effect(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    reason_code: str,
    now: datetime | None = None,
) -> SemanticWriteRecord:
    return _transition(
        connection,
        operation_id,
        expected_state="started",
        target_state="failed_no_effect",
        effect_observed=False,
        resulting_page_epoch=None,
        reason_code=reason_code,
        now=now,
    )


def park_side_effect_unknown(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    reason_code: str,
    now: datetime | None = None,
) -> SemanticWriteRecord:
    return _transition(
        connection,
        operation_id,
        expected_state="started",
        target_state="parked_side_effect_unknown",
        effect_observed=False,
        resulting_page_epoch=None,
        reason_code=reason_code,
        now=now,
    )


def park_stale_after_effect(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    reason_code: str,
    now: datetime | None = None,
) -> SemanticWriteRecord:
    return _transition(
        connection,
        operation_id,
        expected_state="effect_observed",
        target_state="parked_stale_after_effect",
        effect_observed=True,
        resulting_page_epoch=None,
        reason_code=reason_code,
        now=now,
    )
