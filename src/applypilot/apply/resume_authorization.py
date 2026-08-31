"""Attempt- and page-bound authorization for resuming after a human-only wait.

This contract never grants page-write or submission authority.  It only proves
that a fresh Agent turn may re-observe the exact application page after the
configured trigger was observed by the launcher.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from applypilot.apply.page_binding import PageBinding

_TRANSITIONS = frozenset(
    {
        "awaiting_captcha_clearance -> prepare",
        "awaiting_login_completion -> prepare",
        "awaiting_captcha_clearance -> verify",
    }
)
_TRIGGERS = frozenset({"captcha_cleared", "login_completed"})


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


@dataclass(frozen=True, slots=True)
class ResumeAuthorization:
    """One expiring, observation-only resume token for an exact page epoch."""

    authorization_id: str
    attempt_id: str
    application_id: str
    page_binding: PageBinding
    allowed_transition: str
    trigger: str
    expires_at: datetime
    submit_started: bool = False
    submit_authority: bool = False
    page_write_authority: bool = False
    schema_version: str = "3"

    def __post_init__(self) -> None:
        for name in (
            "authorization_id",
            "attempt_id",
            "application_id",
            "allowed_transition",
            "trigger",
            "schema_version",
        ):
            _required(getattr(self, name), name)
        if self.schema_version != "3":
            raise ValueError("unsupported ResumeAuthorization schema_version")
        if self.allowed_transition not in _TRANSITIONS:
            raise ValueError("resume transition is not allowlisted")
        if self.trigger not in _TRIGGERS:
            raise ValueError("resume trigger is not allowlisted")
        if self.page_binding.attempt_id != self.attempt_id:
            raise ValueError("page binding attempt does not match resume authorization")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if self.submit_authority is not False or self.page_write_authority is not False:
            raise ValueError("resume authorization cannot grant write or submit authority")
        if self.submit_started and not self.allowed_transition.endswith(" -> verify"):
            raise ValueError("post-submit resume may only re-enter verification")

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["expires_at"] = self.expires_at.isoformat()
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ResumeAuthorization:
        raw_binding = value.get("page_binding")
        if not isinstance(raw_binding, Mapping):
            raise TypeError("ResumeAuthorization page_binding is required")
        return cls(
            authorization_id=str(value.get("authorization_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            application_id=str(value.get("application_id") or ""),
            page_binding=PageBinding.from_mapping(raw_binding),
            allowed_transition=str(value.get("allowed_transition") or ""),
            trigger=str(value.get("trigger") or ""),
            expires_at=datetime.fromisoformat(str(value.get("expires_at") or "")),
            submit_started=value.get("submit_started") is True,
            submit_authority=value.get("submit_authority") is True,
            page_write_authority=value.get("page_write_authority") is True,
            schema_version=str(value.get("schema_version") or ""),
        )


def issue_resume_authorization(
    *,
    attempt_id: str,
    application_id: str,
    page_binding: PageBinding,
    trigger: str,
    submit_started: bool,
    ttl_seconds: float,
    now: datetime | None = None,
) -> ResumeAuthorization:
    """Issue a deterministic-scope token without adding application authority."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    issued_at = now or datetime.now(UTC)
    transition = (
        "awaiting_captcha_clearance -> verify"
        if submit_started
        else (
            "awaiting_login_completion -> prepare"
            if trigger == "login_completed"
            else "awaiting_captcha_clearance -> prepare"
        )
    )
    return ResumeAuthorization(
        authorization_id=str(uuid.uuid4()),
        attempt_id=attempt_id,
        application_id=application_id,
        page_binding=page_binding,
        allowed_transition=transition,
        trigger=trigger,
        expires_at=issued_at + timedelta(seconds=float(ttl_seconds)),
        submit_started=submit_started,
    )


def validate_resume_authorization(
    authorization: ResumeAuthorization,
    *,
    attempt_id: str,
    application_id: str,
    page_binding: PageBinding,
    trigger: str,
    submit_started: bool,
    now: datetime | None = None,
) -> None:
    """Reject any expired, cross-job, cross-page, or post-submit escalation."""
    observed_at = now or datetime.now(UTC)
    if observed_at >= authorization.expires_at:
        raise ValueError("resume authorization expired")
    if authorization.attempt_id != attempt_id:
        raise ValueError("resume attempt mismatch")
    if authorization.application_id != application_id:
        raise ValueError("resume application mismatch")
    if authorization.page_binding != page_binding:
        raise ValueError("resume page binding mismatch")
    if authorization.trigger != trigger:
        raise ValueError("resume trigger mismatch")
    if authorization.submit_started != submit_started:
        raise ValueError("resume submit state mismatch")


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS agent_resume_authorizations ("
        "authorization_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, "
        "application_id TEXT NOT NULL, authorization_json TEXT NOT NULL, "
        "status TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_resume_authorizations_attempt_status "
        "ON agent_resume_authorizations(attempt_id, status, expires_at)"
    )


def store_resume_authorization(
    connection: sqlite3.Connection,
    authorization: ResumeAuthorization,
) -> bool:
    """Persist an issued token idempotently."""
    ensure_schema(connection)
    encoded = json.dumps(
        authorization.as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        connection.execute(
            "INSERT INTO agent_resume_authorizations "
            "(authorization_id, attempt_id, application_id, authorization_json, "
            "status, expires_at, consumed_at) VALUES (?, ?, ?, ?, 'open', ?, NULL)",
            (
                authorization.authorization_id,
                authorization.attempt_id,
                authorization.application_id,
                encoded,
                authorization.expires_at.isoformat(),
            ),
        )
        connection.commit()
        return True
    except sqlite3.IntegrityError:
        row = connection.execute(
            "SELECT authorization_json FROM agent_resume_authorizations "
            "WHERE authorization_id=?",
            (authorization.authorization_id,),
        ).fetchone()
        if row is not None and str(row[0]) == encoded:
            return False
        raise ValueError("resume authorization id collision") from None


def consume_resume_authorization(
    connection: sqlite3.Connection,
    authorization: ResumeAuthorization,
    *,
    consumed_at: datetime | None = None,
) -> bool:
    """Atomically consume the exact open token once."""
    ensure_schema(connection)
    when = consumed_at or datetime.now(UTC)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("consumed_at must be timezone-aware")
    cursor = connection.execute(
        "UPDATE agent_resume_authorizations SET status='consumed', consumed_at=? "
        "WHERE authorization_id=? AND attempt_id=? AND application_id=? "
        "AND status='open' AND expires_at>?",
        (
            when.isoformat(),
            authorization.authorization_id,
            authorization.attempt_id,
            authorization.application_id,
            when.isoformat(),
        ),
    )
    connection.commit()
    return cursor.rowcount == 1


def latest_open_resume_authorization(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    now: datetime | None = None,
) -> ResumeAuthorization | None:
    """Load the newest unexpired token for a restart/rebind decision."""
    ensure_schema(connection)
    when = now or datetime.now(UTC)
    row = connection.execute(
        "SELECT authorization_json FROM agent_resume_authorizations "
        "WHERE attempt_id=? AND status='open' AND expires_at>? "
        "ORDER BY expires_at DESC, authorization_id DESC LIMIT 1",
        (attempt_id, when.isoformat()),
    ).fetchone()
    if row is None:
        return None
    value = json.loads(str(row[0]))
    if not isinstance(value, Mapping):
        raise TypeError("stored resume authorization must be an object")
    return ResumeAuthorization.from_mapping(value)
