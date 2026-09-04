"""Durable metadata-only journal for production semantic patch batches.

The journal intentionally stores no applicant values, DOM text, URLs, local
paths, or live browser authority.  It records the exact attempt/page lease and
the set of semantic field names so an interrupted dispatch can never be
replayed as an ordinary per-field fallback.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

SCHEMA_VERSION = 1
_STATES = {
    "shadow",
    "started",
    "verified",
    "failed_no_effect",
    "parked_side_effect_unknown",
    "parked_stale_after_effect",
}
_TERMINAL_STATES = _STATES - {"started"}
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_REASON_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,119}")


class SemanticPatchBatchJournalError(RuntimeError):
    """A durable semantic-batch claim or transition was invalid."""


class SemanticPatchBatchCollision(SemanticPatchBatchJournalError):
    """A batch identity was reused with different immutable claims."""


class SemanticPatchBatchTransitionError(SemanticPatchBatchJournalError):
    """A semantic-batch state transition lost its expected-state CAS."""


@dataclass(frozen=True, slots=True)
class SemanticPatchBatchClaims:
    batch_id: str
    attempt_id: str
    actor_id: str
    provider: str
    adapter_version: str
    page_id: str
    page_lease_id: str
    page_lease_epoch: int
    expected_page_epoch: int
    page_signature: str
    semantics: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "batch_id",
            "attempt_id",
            "actor_id",
            "provider",
            "adapter_version",
            "page_id",
            "page_lease_id",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")
        if self.provider not in {"workday", "smartrecruiters"}:
            raise ValueError("semantic patch batch provider is unsupported")
        if isinstance(self.page_lease_epoch, bool) or self.page_lease_epoch < 1:
            raise ValueError("page_lease_epoch must be positive")
        if isinstance(self.expected_page_epoch, bool) or self.expected_page_epoch < 0:
            raise ValueError("expected_page_epoch must be non-negative")
        if not _DIGEST_RE.fullmatch(self.page_signature):
            raise ValueError("page_signature must be a SHA-256 digest")
        if not self.semantics or tuple(sorted(set(self.semantics))) != self.semantics:
            raise ValueError("semantics must be a non-empty sorted unique tuple")

    @property
    def semantics_digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.semantics)).hexdigest()

    @property
    def claims_digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(
                {
                    "batch_id": self.batch_id,
                    "attempt_id": self.attempt_id,
                    "actor_id": self.actor_id,
                    "provider": self.provider,
                    "adapter_version": self.adapter_version,
                    "page_id": self.page_id,
                    "page_lease_id": self.page_lease_id,
                    "page_lease_epoch": self.page_lease_epoch,
                    "expected_page_epoch": self.expected_page_epoch,
                    "page_signature": self.page_signature,
                    "semantics_digest": self.semantics_digest,
                    "semantic_count": len(self.semantics),
                    "submit_authority": False,
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticPatchBatchRecord:
    batch_id: str
    claims_digest: str
    attempt_id: str
    actor_id: str
    provider: str
    adapter_version: str
    page_id: str
    page_lease_id: str
    page_lease_epoch: int
    expected_page_epoch: int
    page_signature: str
    semantics_digest: str
    semantic_count: int
    state: str
    dispatch_count: int
    effect_count: int
    resulting_page_epoch: int | None
    reason_code: str | None
    created_at: str
    updated_at: str


_COLUMNS = (
    "batch_id,claims_digest,attempt_id,actor_id,provider,adapter_version,"
    "page_id,page_lease_id,page_lease_epoch,expected_page_epoch,page_signature,"
    "semantics_digest,semantic_count,state,dispatch_count,effect_count,"
    "resulting_page_epoch,reason_code,created_at,updated_at"
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record(row: sqlite3.Row | tuple[object, ...] | None) -> SemanticPatchBatchRecord | None:
    return None if row is None else SemanticPatchBatchRecord(*row)


def _iso(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("semantic patch batch timestamps must be timezone-aware")
    return current.astimezone(UTC).isoformat()


def _reason(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _REASON_RE.fullmatch(normalized):
        raise ValueError("reason_code must be bounded machine-readable text")
    return normalized


@contextmanager
def _write(connection: sqlite3.Connection, name: str) -> Iterator[None]:
    nested = connection.in_transaction
    if nested:
        connection.execute(f"SAVEPOINT {name}")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            connection.execute(f"RELEASE SAVEPOINT {name}")
        else:
            connection.commit()
    except Exception:
        if nested:
            connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            connection.execute(f"RELEASE SAVEPOINT {name}")
        elif connection.in_transaction:
            connection.rollback()
        raise


def ensure_schema(connection: sqlite3.Connection) -> None:
    with _write(connection, "semantic_patch_batch_schema"):
        connection.execute(
            """CREATE TABLE IF NOT EXISTS semantic_patch_batch_schema (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL CHECK(version > 0)
            )"""
        )
        row = connection.execute(
            "SELECT version FROM semantic_patch_batch_schema WHERE component=?",
            ("semantic_patch_batches",),
        ).fetchone()
        if row is not None and int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported semantic patch batch schema version: {row[0]}")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS semantic_patch_batches (
                batch_id TEXT PRIMARY KEY,
                claims_digest TEXT NOT NULL UNIQUE,
                attempt_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                provider TEXT NOT NULL CHECK(provider IN ('workday','smartrecruiters')),
                adapter_version TEXT NOT NULL,
                page_id TEXT NOT NULL,
                page_lease_id TEXT NOT NULL,
                page_lease_epoch INTEGER NOT NULL CHECK(page_lease_epoch > 0),
                expected_page_epoch INTEGER NOT NULL CHECK(expected_page_epoch >= 0),
                page_signature TEXT NOT NULL,
                semantics_digest TEXT NOT NULL,
                semantic_count INTEGER NOT NULL CHECK(semantic_count > 0),
                state TEXT NOT NULL CHECK(state IN (
                    'shadow','started','verified','failed_no_effect',
                    'parked_side_effect_unknown','parked_stale_after_effect'
                )),
                dispatch_count INTEGER NOT NULL DEFAULT 0
                    CHECK(dispatch_count BETWEEN 0 AND 1),
                effect_count INTEGER NOT NULL DEFAULT 0 CHECK(effect_count >= 0),
                resulting_page_epoch INTEGER,
                reason_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(effect_count <= semantic_count),
                CHECK(resulting_page_epoch IS NULL OR resulting_page_epoch >= 0)
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_semantic_patch_batches_attempt
            ON semantic_patch_batches(attempt_id,semantics_digest,created_at)"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO semantic_patch_batch_schema(component,version)
            VALUES('semantic_patch_batches', ?)""",
            (SCHEMA_VERSION,),
        )


def get_batch(connection: sqlite3.Connection, batch_id: str) -> SemanticPatchBatchRecord | None:
    ensure_schema(connection)
    return _record(
        connection.execute(
            f"SELECT {_COLUMNS} FROM semantic_patch_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
    )


def latest_attempt_semantics(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    semantics_digest: str,
) -> SemanticPatchBatchRecord | None:
    ensure_schema(connection)
    return _record(
        connection.execute(
            f"SELECT {_COLUMNS} FROM semantic_patch_batches "
            "WHERE attempt_id=? AND semantics_digest=? AND state<>'shadow' "
            "ORDER BY created_at DESC,batch_id DESC LIMIT 1",
            (attempt_id, semantics_digest),
        ).fetchone()
    )


def begin_batch(
    connection: sqlite3.Connection,
    claims: SemanticPatchBatchClaims,
    *,
    shadow: bool = False,
    now: datetime | None = None,
) -> SemanticPatchBatchRecord:
    ensure_schema(connection)
    timestamp = _iso(now)
    state = "shadow" if shadow else "started"
    with _write(connection, "semantic_patch_batch_begin"):
        try:
            connection.execute(
                f"INSERT INTO semantic_patch_batches({_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,NULL,NULL,?,?)",
                (
                    claims.batch_id,
                    claims.claims_digest,
                    claims.attempt_id,
                    claims.actor_id,
                    claims.provider,
                    claims.adapter_version,
                    claims.page_id,
                    claims.page_lease_id,
                    claims.page_lease_epoch,
                    claims.expected_page_epoch,
                    claims.page_signature,
                    claims.semantics_digest,
                    len(claims.semantics),
                    state,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            existing = _record(
                connection.execute(
                    f"SELECT {_COLUMNS} FROM semantic_patch_batches WHERE batch_id=? OR claims_digest=?",
                    (claims.batch_id, claims.claims_digest),
                ).fetchone()
            )
            if (
                existing is None
                or existing.batch_id != claims.batch_id
                or existing.claims_digest != claims.claims_digest
            ):
                raise SemanticPatchBatchCollision(
                    "semantic patch batch identity collided with different claims"
                ) from None
            return existing
        created = _record(
            connection.execute(
                f"SELECT {_COLUMNS} FROM semantic_patch_batches WHERE batch_id=?",
                (claims.batch_id,),
            ).fetchone()
        )
    assert created is not None
    return created


def claim_dispatch(
    connection: sqlite3.Connection,
    batch_id: str,
    *,
    now: datetime | None = None,
) -> SemanticPatchBatchRecord:
    ensure_schema(connection)
    with _write(connection, "semantic_patch_batch_dispatch"):
        cursor = connection.execute(
            "UPDATE semantic_patch_batches SET dispatch_count=1,updated_at=? "
            "WHERE batch_id=? AND state='started' AND dispatch_count=0 AND effect_count=0",
            (_iso(now), batch_id),
        )
        if cursor.rowcount != 1:
            raise SemanticPatchBatchTransitionError("semantic patch batch dispatch was already claimed")
    record = get_batch(connection, batch_id)
    assert record is not None
    return record


def note_effect(
    connection: sqlite3.Connection,
    batch_id: str,
    *,
    expected_effect_count: int,
    now: datetime | None = None,
) -> SemanticPatchBatchRecord:
    ensure_schema(connection)
    with _write(connection, "semantic_patch_batch_effect"):
        cursor = connection.execute(
            "UPDATE semantic_patch_batches SET effect_count=effect_count+1,updated_at=? "
            "WHERE batch_id=? AND state='started' AND dispatch_count=1 "
            "AND effect_count=? AND effect_count<semantic_count",
            (_iso(now), batch_id, expected_effect_count),
        )
        if cursor.rowcount != 1:
            raise SemanticPatchBatchTransitionError("semantic patch batch effect count lost its CAS")
    record = get_batch(connection, batch_id)
    assert record is not None
    return record


def finish_batch(
    connection: sqlite3.Connection,
    batch_id: str,
    *,
    state: str,
    expected_effect_count: int,
    reason_code: str,
    resulting_page_epoch: int | None = None,
    now: datetime | None = None,
) -> SemanticPatchBatchRecord:
    if state not in _TERMINAL_STATES or state == "shadow":
        raise ValueError("unsupported semantic patch batch terminal state")
    if state == "failed_no_effect" and expected_effect_count != 0:
        raise ValueError("failed_no_effect requires zero effects")
    if state == "verified" and resulting_page_epoch is None:
        raise ValueError("verified batches require a resulting page epoch")
    ensure_schema(connection)
    with _write(connection, "semantic_patch_batch_finish"):
        cursor = connection.execute(
            "UPDATE semantic_patch_batches SET state=?,resulting_page_epoch=?,"
            "reason_code=?,updated_at=? WHERE batch_id=? AND state='started' "
            "AND dispatch_count=1 AND effect_count=?",
            (
                state,
                resulting_page_epoch,
                _reason(reason_code),
                _iso(now),
                batch_id,
                expected_effect_count,
            ),
        )
        if cursor.rowcount != 1:
            raise SemanticPatchBatchTransitionError("semantic patch batch terminal transition lost its CAS")
    record = get_batch(connection, batch_id)
    assert record is not None
    return record


__all__ = [
    "SemanticPatchBatchClaims",
    "SemanticPatchBatchCollision",
    "SemanticPatchBatchJournalError",
    "SemanticPatchBatchRecord",
    "SemanticPatchBatchTransitionError",
    "begin_batch",
    "claim_dispatch",
    "ensure_schema",
    "finish_batch",
    "get_batch",
    "latest_attempt_semantics",
    "note_effect",
]
