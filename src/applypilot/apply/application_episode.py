"""Durable application episodes built on the existing control event log.

The episode table is a replayable aggregate, not a second authority. Typed
command and result records are appended to ``agent_events``; existing recovery,
operator, browser, SubmissionGate, ledger, and receipt owners retain their
current authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from applypilot.apply.application_facts import (
    ApplicationFact,
    current_profile_facts,
    resolve_application_fact,
)
from applypilot.apply.authorization import compute_job_fingerprint
from applypilot.apply.browser_broker import BrowserLeaseBundle
from applypilot.apply.contracts import ApplicationEvent, application_actor_id
from applypilot.apply.control_descriptors import (
    ControlDescriptor,
    ControlInspectionDenied,
    FormInspection,
    provider_for_url,
)
from applypilot.apply.provider_registry import provider_supports
from applypilot.storage import agent_control

EpisodeState = Literal[
    "active",
    "checkpoint",
    "recovering",
    "parked",
    "human_required",
    "receipt_only",
    "complete",
]
CommandKind = Literal["checkpoint", "recovery", "browser_control", "park", "human_request"]
CommandEffect = Literal["none", "recovery", "browser"]
CommandStatus = Literal[
    "verified",
    "failed_no_effect",
    "effect_unknown",
    "parked",
    "human_required",
]
EvidenceStatus = Literal["ready", "unavailable", "conflicted"]

SCHEMA_VERSION = "1"
MAX_REPLANS = 1
MAX_FORM_DIFF = 24
_REF = re.compile(r"[^\s\x00-\x1f]{1,300}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MATERIAL_FACT_MARKERS = (
    "identity",
    "legal",
    "work_authorization",
    "work authorization",
    "salary",
    "compensation",
    "date",
    "availability",
    "sponsorship",
    "visa",
)


class EpisodeConflict(RuntimeError):
    """The persisted episode or command identity changed."""


class EpisodeParked(RuntimeError):
    """A bounded replan cannot safely continue automatically."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _ref(value: object, name: str) -> str:
    text = _required(value, name)
    if not _REF.fullmatch(text):
        raise ValueError(f"{name} must be a bounded opaque reference")
    return text


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FactEvidence:
    fact_ref: str
    key: str
    source: str
    scope: str
    sensitivity: str
    confirmed_at: str
    expires_at: str | None

    def __post_init__(self) -> None:
        _ref(self.fact_ref, "fact_ref")
        for name in ("key", "source", "scope", "sensitivity", "confirmed_at"):
            _required(getattr(self, name), name)

    def as_dict(self) -> dict[str, object]:
        return {
            "fact_ref": self.fact_ref,
            "key": self.key,
            "source": self.source,
            "scope": self.scope,
            "sensitivity": self.sensitivity,
            "confirmed_at": self.confirmed_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class JobEvidenceBundle:
    """Source references and hashes only; raw applicant answers never persist here."""

    bundle_id: str
    attempt_id: str
    actor_id: str
    job_fingerprint: str
    provider: str
    source_refs: tuple[str, ...]
    fact_evidence: tuple[FactEvidence, ...]
    material_refs: tuple[str, ...]
    prompt_provenance_ref: str | None
    answer_provenance_ref: str | None
    provider_evidence_refs: tuple[str, ...]
    unavailable: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    status: EvidenceStatus = "ready"
    schema_version: str = SCHEMA_VERSION
    created_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))

    def __post_init__(self) -> None:
        _ref(self.bundle_id, "bundle_id")
        _ref(self.attempt_id, "attempt_id")
        if self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("evidence actor/attempt identity is not canonical")
        if not _SHA256.fullmatch(self.job_fingerprint):
            raise ValueError("job_fingerprint must be a lowercase SHA-256 digest")
        _required(self.provider, "provider")
        if self.status not in {"ready", "unavailable", "conflicted"}:
            raise ValueError("unsupported evidence status")
        if self.conflicts and self.status != "conflicted":
            raise ValueError("conflicting evidence must remain conflicted")
        if self.unavailable and self.status == "ready":
            raise ValueError("unavailable evidence cannot be ready")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported JobEvidenceBundle schema_version")
        _aware(self.created_at, "created_at")

    @property
    def evidence_digest(self) -> str:
        payload = self.as_dict(include_identity=False)
        payload.pop("created_at")
        return _digest(payload)

    def as_dict(self, *, include_identity: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "actor_id": self.actor_id,
            "job_fingerprint": self.job_fingerprint,
            "provider": self.provider,
            "source_refs": list(self.source_refs),
            "fact_evidence": [item.as_dict() for item in self.fact_evidence],
            "material_refs": list(self.material_refs),
            "prompt_provenance_ref": self.prompt_provenance_ref,
            "answer_provenance_ref": self.answer_provenance_ref,
            "provider_evidence_refs": list(self.provider_evidence_refs),
            "unavailable": list(self.unavailable),
            "conflicts": list(self.conflicts),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }
        if include_identity:
            value["bundle_id"] = self.bundle_id
            value["evidence_digest"] = self.evidence_digest
        return value


@dataclass(frozen=True, slots=True)
class ApplicationEpisode:
    episode_id: str
    actor_id: str
    attempt_id: str
    application_session_id: str
    run_id: str
    turn_id: str
    provider: str
    page_id: str
    page_lease_id: str
    page_lease_epoch: int
    page_epoch: int
    state: EpisodeState
    checkpoint_id: str | None
    command_sequence: int
    result_sequence: int
    replan_count: int
    evidence_digest: str
    terminal_reason: str | None = None
    human_required_reason: str | None = None
    revision: int = 0
    schema_version: str = SCHEMA_VERSION
    updated_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))

    def __post_init__(self) -> None:
        for name in (
            "episode_id",
            "attempt_id",
            "application_session_id",
            "run_id",
            "turn_id",
            "provider",
            "page_id",
            "page_lease_id",
        ):
            _ref(getattr(self, name), name)
        if self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("episode actor/attempt identity is not canonical")
        if self.turn_id != self.run_id:
            raise ValueError("episode run_id must remain the turn_id compatibility alias")
        if self.state not in {
            "active", "checkpoint", "recovering", "parked", "human_required", "receipt_only", "complete"
        }:
            raise ValueError("unsupported episode state")
        for name in (
            "page_lease_epoch",
            "page_epoch",
            "command_sequence",
            "result_sequence",
            "replan_count",
            "revision",
        ):
            value = getattr(self, name)
            minimum = 1 if name == "page_lease_epoch" else 0
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} is invalid")
        if self.result_sequence > self.command_sequence:
            raise ValueError("episode result sequence cannot lead command sequence")
        if self.replan_count > MAX_REPLANS:
            raise ValueError("episode replan count exceeds the bounded policy")
        if not _SHA256.fullmatch(self.evidence_digest):
            raise ValueError("episode evidence_digest must be a lowercase SHA-256 digest")
        if self.state == "human_required" and not self.human_required_reason:
            raise ValueError("human_required episode needs an exact reason")
        if self.state in {"parked", "receipt_only", "complete"} and not self.terminal_reason:
            raise ValueError("terminal episode state needs an exact reason")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported ApplicationEpisode schema_version")
        _aware(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class ApplicationCommand:
    command_id: str
    idempotency_key: str
    episode_id: str
    episode_revision: int
    sequence: int
    actor_id: str
    attempt_id: str
    run_id: str
    turn_id: str
    kind: CommandKind
    action: str
    effect: CommandEffect
    evidence_digest: str
    expected_page_epoch: int | None = None
    provider: str | None = None
    descriptor_id: str | None = None
    descriptor_digest: str | None = None
    value_ref: str | None = None
    recovery_ref: str | None = None
    submit_authority: bool = False
    ledger_write_authority: bool = False
    page_write_authority: bool = False
    schema_version: str = SCHEMA_VERSION
    created_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))

    def __post_init__(self) -> None:
        for name in ("command_id", "idempotency_key", "episode_id", "attempt_id", "run_id", "turn_id", "action"):
            _ref(getattr(self, name), name)
        if self.actor_id != application_actor_id(self.attempt_id) or self.turn_id != self.run_id:
            raise ValueError("application command identity is not canonical")
        if self.kind not in {"checkpoint", "recovery", "browser_control", "park", "human_request"}:
            raise ValueError("unsupported application command kind")
        expected_effect = {"browser_control": "browser", "recovery": "recovery"}.get(self.kind, "none")
        if self.effect != expected_effect:
            raise ValueError("application command effect does not match kind")
        if any((self.submit_authority, self.ledger_write_authority, self.page_write_authority)):
            raise ValueError("application commands cannot claim page, Submit, or ledger authority")
        if self.episode_revision < 0 or self.sequence < 1:
            raise ValueError("application command sequence or revision is invalid")
        if not _SHA256.fullmatch(self.evidence_digest):
            raise ValueError("application command evidence digest is invalid")
        if self.kind == "browser_control":
            if not provider_supports(self.provider, "application_episode"):
                raise ValueError("browser command provider is unsupported")
            if self.expected_page_epoch is None or self.expected_page_epoch < 0:
                raise ValueError("browser command requires an expected page epoch")
            _ref(self.descriptor_id, "descriptor_id")
            if self.descriptor_digest is None or not _SHA256.fullmatch(self.descriptor_digest):
                raise ValueError("browser command requires a descriptor digest")
            _ref(self.value_ref, "value_ref")
        if self.kind == "recovery":
            _ref(self.recovery_ref, "recovery_ref")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported ApplicationCommand schema_version")
        _aware(self.created_at, "created_at")

    def event_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "episode_revision": self.episode_revision,
            "sequence": self.sequence,
            "kind": self.kind,
            "action": self.action,
            "effect": self.effect,
            "evidence_digest": self.evidence_digest,
            "expected_page_epoch": self.expected_page_epoch,
            "provider": self.provider,
            "descriptor_id": self.descriptor_id,
            "descriptor_digest": self.descriptor_digest,
            "value_ref": self.value_ref,
            "recovery_ref": self.recovery_ref,
        }


@dataclass(frozen=True, slots=True)
class ApplicationCommandResult:
    result_id: str
    command_id: str
    episode_id: str
    sequence: int
    actor_id: str
    attempt_id: str
    run_id: str
    turn_id: str
    status: CommandStatus
    outcome: str
    effect_applied: bool | None
    result_ref: str | None = None
    resulting_page_epoch: int | None = None
    replayed: bool = False
    schema_version: str = SCHEMA_VERSION
    occurred_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))

    def __post_init__(self) -> None:
        for name in ("result_id", "command_id", "episode_id", "attempt_id", "run_id", "turn_id", "outcome"):
            _ref(getattr(self, name), name)
        if self.actor_id != application_actor_id(self.attempt_id) or self.turn_id != self.run_id:
            raise ValueError("application command result identity is not canonical")
        if self.status not in {"verified", "failed_no_effect", "effect_unknown", "parked", "human_required"}:
            raise ValueError("unsupported command result status")
        if self.sequence < 1:
            raise ValueError("result sequence is invalid")
        if self.status == "verified" and self.effect_applied is None:
            raise ValueError("verified result must state whether an effect occurred")
        if self.status == "failed_no_effect" and self.effect_applied is not False:
            raise ValueError("failed_no_effect must prove no effect")
        if self.status == "effect_unknown" and self.effect_applied is not None:
            raise ValueError("effect_unknown cannot claim an effect outcome")
        if self.result_ref is not None:
            _ref(self.result_ref, "result_ref")
        if self.resulting_page_epoch is not None and self.resulting_page_epoch < 0:
            raise ValueError("resulting_page_epoch is invalid")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported ApplicationCommandResult schema_version")
        _aware(self.occurred_at, "occurred_at")

    def event_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "sequence": self.sequence,
            "command_id": self.command_id,
            "status": self.status,
            "outcome": self.outcome,
            "effect_applied": self.effect_applied,
            "result_ref": self.result_ref,
            "resulting_page_epoch": self.resulting_page_epoch,
        }


@dataclass(frozen=True, slots=True)
class FormDiff:
    provider: str
    before_digest: str
    after_digest: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    before_page_epoch: int
    after_page_epoch: int

    @property
    def size(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)


@dataclass(frozen=True, slots=True)
class ReplanDecision:
    authorized: bool
    reason: str
    diff: FormDiff
    replacement: ApplicationCommand | None = None


CommandExecutor = Callable[[ApplicationCommand], ApplicationCommandResult]


def ensure_schema(connection: sqlite3.Connection) -> None:
    agent_control.ensure_schema(connection)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS application_evidence_bundles (
            bundle_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            evidence_digest TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            bundle_json TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_application_evidence_attempt
        ON application_evidence_bundles(attempt_id, created_at, bundle_id)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS application_episodes (
            episode_id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL UNIQUE,
            application_session_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            page_id TEXT NOT NULL,
            page_lease_id TEXT NOT NULL,
            page_lease_epoch INTEGER NOT NULL CHECK(page_lease_epoch > 0),
            page_epoch INTEGER NOT NULL CHECK(page_epoch >= 0),
            state TEXT NOT NULL,
            checkpoint_id TEXT,
            command_sequence INTEGER NOT NULL CHECK(command_sequence >= 0),
            result_sequence INTEGER NOT NULL CHECK(result_sequence >= 0),
            replan_count INTEGER NOT NULL CHECK(replan_count >= 0),
            evidence_digest TEXT NOT NULL,
            terminal_reason TEXT,
            human_required_reason TEXT,
            revision INTEGER NOT NULL CHECK(revision >= 0),
            schema_version TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )


def persist_job_evidence_bundle(connection: sqlite3.Connection, bundle: JobEvidenceBundle) -> bool:
    ensure_schema(connection)
    payload = json.dumps(bundle.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    values = (
        bundle.bundle_id,
        bundle.attempt_id,
        bundle.actor_id,
        bundle.evidence_digest,
        bundle.status,
        payload,
        bundle.schema_version,
        bundle.created_at.isoformat(),
    )
    try:
        connection.execute(
            "INSERT INTO application_evidence_bundles VALUES(?,?,?,?,?,?,?,?)",
            values,
        )
        return True
    except sqlite3.IntegrityError:
        row = connection.execute(
            "SELECT bundle_id,attempt_id,actor_id,evidence_digest,status,bundle_json,"
            "schema_version,created_at FROM application_evidence_bundles "
            "WHERE bundle_id=? OR evidence_digest=?",
            (bundle.bundle_id, bundle.evidence_digest),
        ).fetchone()
        if row is not None and tuple(row[:5]) == values[:5] and row[6] == values[6]:
            return False
        raise EpisodeConflict("evidence bundle identity collision") from None


_EPISODE_COLUMNS = (
    "episode_id,actor_id,attempt_id,application_session_id,run_id,turn_id,provider,"
    "page_id,page_lease_id,page_lease_epoch,page_epoch,state,checkpoint_id,"
    "command_sequence,result_sequence,replan_count,evidence_digest,terminal_reason,"
    "human_required_reason,revision,schema_version,updated_at"
)


def _episode(row: sqlite3.Row | tuple[object, ...] | None) -> ApplicationEpisode | None:
    if row is None:
        return None
    values = tuple(row)
    return ApplicationEpisode(
        episode_id=str(values[0]),
        actor_id=str(values[1]),
        attempt_id=str(values[2]),
        application_session_id=str(values[3]),
        run_id=str(values[4]),
        turn_id=str(values[5]),
        provider=str(values[6]),
        page_id=str(values[7]),
        page_lease_id=str(values[8]),
        page_lease_epoch=int(values[9]),
        page_epoch=int(values[10]),
        state=str(values[11]),  # type: ignore[arg-type]
        checkpoint_id=None if values[12] is None else str(values[12]),
        command_sequence=int(values[13]),
        result_sequence=int(values[14]),
        replan_count=int(values[15]),
        evidence_digest=str(values[16]),
        terminal_reason=None if values[17] is None else str(values[17]),
        human_required_reason=None if values[18] is None else str(values[18]),
        revision=int(values[19]),
        schema_version=str(values[20]),
        updated_at=datetime.fromisoformat(str(values[21])),
    )


def get_episode(connection: sqlite3.Connection, episode_id: str) -> ApplicationEpisode | None:
    ensure_schema(connection)
    return _episode(
        connection.execute(
            f"SELECT {_EPISODE_COLUMNS} FROM application_episodes WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
    )


def latest_episode_for_attempt(connection: sqlite3.Connection, attempt_id: str) -> ApplicationEpisode | None:
    ensure_schema(connection)
    return _episode(
        connection.execute(
            f"SELECT {_EPISODE_COLUMNS} FROM application_episodes WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
    )


def create_episode(connection: sqlite3.Connection, episode: ApplicationEpisode) -> bool:
    if episode.revision != 0:
        raise ValueError("new episode revision must be zero")
    ensure_schema(connection)
    values = (
        episode.episode_id,
        episode.actor_id,
        episode.attempt_id,
        episode.application_session_id,
        episode.run_id,
        episode.turn_id,
        episode.provider,
        episode.page_id,
        episode.page_lease_id,
        episode.page_lease_epoch,
        episode.page_epoch,
        episode.state,
        episode.checkpoint_id,
        episode.command_sequence,
        episode.result_sequence,
        episode.replan_count,
        episode.evidence_digest,
        episode.terminal_reason,
        episode.human_required_reason,
        episode.revision,
        episode.schema_version,
        episode.updated_at.isoformat(),
    )
    try:
        connection.execute(
            f"INSERT INTO application_episodes({_EPISODE_COLUMNS}) VALUES({','.join('?' for _ in values)})",
            values,
        )
        return True
    except sqlite3.IntegrityError:
        existing = get_episode(connection, episode.episode_id)
        if existing == episode:
            return False
        raise EpisodeConflict("application episode identity collision") from None


def _update_episode(
    connection: sqlite3.Connection,
    current: ApplicationEpisode,
    updated: ApplicationEpisode,
) -> None:
    if updated.episode_id != current.episode_id or updated.revision != current.revision + 1:
        raise EpisodeConflict("episode update revision is invalid")
    cursor = connection.execute(
        """UPDATE application_episodes SET
        run_id=?,turn_id=?,provider=?,page_id=?,page_lease_id=?,page_lease_epoch=?,
        page_epoch=?,state=?,checkpoint_id=?,command_sequence=?,result_sequence=?,
        replan_count=?,evidence_digest=?,terminal_reason=?,human_required_reason=?,
        revision=?,schema_version=?,updated_at=? WHERE episode_id=? AND revision=?""",
        (
            updated.run_id,
            updated.turn_id,
            updated.provider,
            updated.page_id,
            updated.page_lease_id,
            updated.page_lease_epoch,
            updated.page_epoch,
            updated.state,
            updated.checkpoint_id,
            updated.command_sequence,
            updated.result_sequence,
            updated.replan_count,
            updated.evidence_digest,
            updated.terminal_reason,
            updated.human_required_reason,
            updated.revision,
            updated.schema_version,
            updated.updated_at.isoformat(),
            updated.episode_id,
            current.revision,
        ),
    )
    if cursor.rowcount != 1:
        raise EpisodeConflict("application episode CAS failed")


def _fact_evidence(fact: ApplicationFact) -> FactEvidence:
    assert fact.source is not None and fact.scope is not None and fact.confirmed_at is not None
    return FactEvidence(
        fact_ref=fact.fact_ref,
        key=fact.key,
        source=fact.source,
        scope=fact.scope,
        sensitivity=fact.sensitivity,
        confirmed_at=fact.confirmed_at.isoformat(),
        expires_at=None if fact.expires_at is None else fact.expires_at.isoformat(),
    )


def _material_refs(job: Mapping[str, object]) -> tuple[str, ...]:
    refs: list[str] = []
    for key in ("_bound_submission_materials", "_material_snapshot"):
        raw = job.get(key)
        if not isinstance(raw, Mapping):
            continue
        materials = raw.get("materials")
        if not isinstance(materials, (list, tuple)):
            continue
        for item in materials:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind") or "material").strip().casefold()
            digest = str(item.get("sha256") or "").strip()
            state = str(item.get("state") or "").strip().casefold()
            if _SHA256.fullmatch(digest):
                refs.append(f"material:{kind}:{digest}")
            elif state == "not_required":
                refs.append(f"material:{kind}:not_required")
    resume_digest = str(job.get("tailored_resume_sha256") or "").strip()
    if _SHA256.fullmatch(resume_digest):
        refs.append(f"material:resume:{resume_digest}")
    return tuple(sorted(set(refs)))


def build_job_evidence_bundle(
    job: Mapping[str, object],
    profile: Mapping[str, object],
    *,
    attempt_id: str,
    inspection: FormInspection | None = None,
    required_fact_keys: Iterable[str] = (),
    required_material_kinds: Iterable[str] = (),
    now: datetime | None = None,
) -> JobEvidenceBundle:
    """Build one value-redacted bundle from current host-owned sources."""
    attempt_id = _ref(attempt_id, "attempt_id")
    actor_id = application_actor_id(attempt_id)
    current = now or datetime.now(UTC)
    _aware(current, "now")
    fingerprint = compute_job_fingerprint(dict(job))
    source_refs = tuple(
        dict.fromkeys(
            str(job.get(key) or "").strip()
            for key in ("source_url", "url", "application_url")
            if str(job.get(key) or "").strip()
        )
    )
    application_url = job.get("application_url") or job.get("url")
    provider = inspection.provider if inspection is not None else provider_for_url(application_url)
    if provider is None:
        provider = str(job.get("site") or job.get("source_site") or "unknown").strip().casefold()
    unavailable: list[str] = []
    conflicts: list[str] = []
    if not source_refs:
        unavailable.append("job_source_missing")
    if not provider_supports(provider, "application_episode"):
        unavailable.append("provider_evidence_unavailable")

    raw_binding = job.get("_answer_provenance_binding")
    binding = dict(raw_binding) if isinstance(raw_binding, Mapping) else {}
    scopes = tuple(str(item) for item in binding.get("fact_scopes", ()) if str(item).strip())
    facts = current_profile_facts(profile)
    fact_evidence: list[FactEvidence] = []
    for key in sorted({_required(item, "required_fact_key") for item in required_fact_keys}):
        resolutions: list[ApplicationFact] = []
        conflict = False
        for scope in scopes:
            resolution = resolve_application_fact(facts, key=key, scope=scope, at=current)
            if resolution.status == "conflict":
                conflict = True
            if resolution.production_ready and resolution.fact_ref is not None:
                matched = [item for item in facts if item.fact_ref == resolution.fact_ref]
                if len(matched) == 1:
                    resolutions.append(matched[0])
        unique = {item.fact_ref: item for item in resolutions}
        if conflict or len(unique) > 1:
            conflicts.append(f"fact_conflict:{key}")
        elif not unique:
            marker = "material_fact_unavailable" if any(token in key.casefold() for token in _MATERIAL_FACT_MARKERS) else "fact_unavailable"
            unavailable.append(f"{marker}:{key}")
        else:
            fact_evidence.append(_fact_evidence(next(iter(unique.values()))))

    material_refs = _material_refs(job)
    required_materials = {str(item).strip().casefold() for item in required_material_kinds if str(item).strip()}
    present_materials = {item.split(":", 2)[1] for item in material_refs}
    for kind in sorted(required_materials - present_materials):
        unavailable.append(f"material_unavailable:{kind}")

    prompt_contract = job.get("_control_contract")
    prompt_ref = f"prompt:{_digest(prompt_contract)}" if isinstance(prompt_contract, Mapping) else None
    answer_seed = str(binding.get("opaque_binding_seed") or "").strip()
    answer_ref = f"answer-provenance:{answer_seed}" if _SHA256.fullmatch(answer_seed) else None
    if prompt_ref is None:
        unavailable.append("prompt_provenance_unavailable")
    if answer_ref is None:
        unavailable.append("answer_provenance_unavailable")

    provider_refs: list[str] = []
    raw_lease = job.get("_browser_lease_binding")
    if isinstance(raw_lease, Mapping):
        try:
            bundle = BrowserLeaseBundle.from_mapping(raw_lease)
        except (TypeError, ValueError):
            unavailable.append("page_binding_invalid")
        else:
            page = bundle.page_binding
            if page.attempt_id != attempt_id or page.owner_id != actor_id:
                conflicts.append("page_binding_identity_conflict")
            provider_refs.extend(
                (
                    f"page:{page.page_id}",
                    f"lease:{page.page_lease_id}:{page.page_lease_epoch}",
                    f"page-epoch:{page.page_epoch}",
                )
            )
    else:
        unavailable.append("page_binding_missing")
    if inspection is not None:
        if inspection.context.actor_id != actor_id or inspection.context.attempt_id != attempt_id:
            conflicts.append("inspection_identity_conflict")
        descriptor_claims = [
            {
                "descriptor_id": item.descriptor_id,
                "kind": item.kind,
                "semantic": item.semantic,
                "required": item.required,
                "writable": item.writable,
                "stateful": item.stateful,
                "options": item.options,
                "options_truncated": item.options_truncated,
                "locator_digest": item.locator_digest,
            }
            for item in inspection.controls
        ]
        provider_refs.append(f"form-inspection:{_digest(descriptor_claims)}")

    observations = job.get("_agent_observations")
    if isinstance(observations, Mapping):
        refs = observations.get("evidence_refs")
        if isinstance(refs, (list, tuple)):
            provider_refs.extend(f"agent-evidence:{str(item)[:120]}" for item in refs if str(item).strip())

    unavailable_tuple = tuple(sorted(set(unavailable)))
    conflicts_tuple = tuple(sorted(set(conflicts)))
    status: EvidenceStatus = "conflicted" if conflicts_tuple else "unavailable" if unavailable_tuple else "ready"
    claims = {
        "attempt_id": attempt_id,
        "actor_id": actor_id,
        "job_fingerprint": fingerprint,
        "provider": provider or "unknown",
        "source_refs": source_refs,
        "fact_evidence": [item.as_dict() for item in fact_evidence],
        "material_refs": material_refs,
        "prompt_provenance_ref": prompt_ref,
        "answer_provenance_ref": answer_ref,
        "provider_evidence_refs": tuple(sorted(set(provider_refs))),
        "unavailable": unavailable_tuple,
        "conflicts": conflicts_tuple,
        "status": status,
        "schema_version": SCHEMA_VERSION,
    }
    claims_digest = _digest(claims)
    return JobEvidenceBundle(
        bundle_id=f"evidence:{claims_digest}",
        attempt_id=attempt_id,
        actor_id=actor_id,
        job_fingerprint=fingerprint,
        provider=provider or "unknown",
        source_refs=source_refs,
        fact_evidence=tuple(fact_evidence),
        material_refs=material_refs,
        prompt_provenance_ref=prompt_ref,
        answer_provenance_ref=answer_ref,
        provider_evidence_refs=tuple(sorted(set(provider_refs))),
        unavailable=unavailable_tuple,
        conflicts=conflicts_tuple,
        status=status,
        created_at=current,
    )


def episode_from_job(
    job: Mapping[str, object],
    *,
    run_id: str,
    evidence: JobEvidenceBundle,
    state: EpisodeState = "active",
    checkpoint_id: str | None = None,
    now: datetime | None = None,
) -> ApplicationEpisode:
    raw_lease = job.get("_browser_lease_binding")
    if not isinstance(raw_lease, Mapping):
        raise TypeError("application episode requires the current browser lease")
    bundle = BrowserLeaseBundle.from_mapping(raw_lease)
    page = bundle.page_binding
    attempt_id = evidence.attempt_id
    actor_id = evidence.actor_id
    if page.attempt_id != attempt_id or page.owner_id != actor_id:
        raise ValueError("application episode page identity is stale")
    run_id = _ref(run_id, "run_id")
    terminal_reason = None
    human_reason = None
    if evidence.status != "ready":
        reason = evidence.conflicts[0] if evidence.conflicts else evidence.unavailable[0]
        needs_human = bool(evidence.conflicts) or reason.startswith(
            ("material_fact_unavailable:", "material_unavailable:")
        )
        if needs_human:
            state = "human_required"
            human_reason = reason
        else:
            state = "parked"
            terminal_reason = reason
    return ApplicationEpisode(
        episode_id=f"episode:{attempt_id}",
        actor_id=actor_id,
        attempt_id=attempt_id,
        application_session_id=str(job.get("_application_session_id") or f"session:{attempt_id}"),
        run_id=run_id,
        turn_id=run_id,
        provider=evidence.provider,
        page_id=page.page_id,
        page_lease_id=page.page_lease_id,
        page_lease_epoch=page.page_lease_epoch,
        page_epoch=page.page_epoch,
        state=state,
        checkpoint_id=checkpoint_id,
        command_sequence=0,
        result_sequence=0,
        replan_count=0,
        evidence_digest=evidence.evidence_digest,
        terminal_reason=terminal_reason,
        human_required_reason=human_reason,
        updated_at=now or datetime.now(UTC),
    )


def application_command(
    episode: ApplicationEpisode,
    *,
    kind: CommandKind,
    action: str,
    recovery_ref: str | None = None,
    descriptor: ControlDescriptor | None = None,
    value_ref: str | None = None,
    now: datetime | None = None,
) -> ApplicationCommand:
    sequence = episode.command_sequence + 1
    effect: CommandEffect = "browser" if kind == "browser_control" else "recovery" if kind == "recovery" else "none"
    identity = {
        "episode_id": episode.episode_id,
        "revision": episode.revision,
        "sequence": sequence,
        "kind": kind,
        "action": action,
        "recovery_ref": recovery_ref,
        "descriptor_id": None if descriptor is None else descriptor.descriptor_id,
        "descriptor_digest": None if descriptor is None else descriptor.locator_digest,
        "value_ref": value_ref,
        "evidence_digest": episode.evidence_digest,
    }
    key = _digest(identity)
    return ApplicationCommand(
        command_id=f"episode-command:{key}",
        idempotency_key=f"episode-command:{key}",
        episode_id=episode.episode_id,
        episode_revision=episode.revision,
        sequence=sequence,
        actor_id=episode.actor_id,
        attempt_id=episode.attempt_id,
        run_id=episode.run_id,
        turn_id=episode.turn_id,
        kind=kind,
        action=action,
        effect=effect,
        evidence_digest=episode.evidence_digest,
        expected_page_epoch=None if descriptor is None else descriptor.page_binding.page_epoch,
        provider=None if descriptor is None else descriptor.provider,
        descriptor_id=None if descriptor is None else descriptor.descriptor_id,
        descriptor_digest=None if descriptor is None else descriptor.locator_digest,
        value_ref=value_ref,
        recovery_ref=recovery_ref,
        created_at=now or datetime.now(UTC),
    )


def _result_from_event(event: ApplicationEvent, *, replayed: bool) -> ApplicationCommandResult:
    payload = event.payload
    return ApplicationCommandResult(
        result_id=event.event_id,
        command_id=str(payload["command_id"]),
        episode_id=str(payload["episode_id"]),
        sequence=int(payload["sequence"]),
        actor_id=str(event.actor_id),
        attempt_id=event.attempt_id,
        run_id=event.run_id,
        turn_id=str(event.turn_id),
        status=str(payload["status"]),  # type: ignore[arg-type]
        outcome=str(payload["outcome"]),
        effect_applied=payload.get("effect_applied"),  # type: ignore[arg-type]
        result_ref=None if payload.get("result_ref") is None else str(payload["result_ref"]),
        resulting_page_epoch=(
            None if payload.get("resulting_page_epoch") is None else int(payload["resulting_page_epoch"])
        ),
        replayed=replayed,
        occurred_at=event.occurred_at,
    )


def _existing_events(
    connection: sqlite3.Connection,
    command: ApplicationCommand,
) -> tuple[ApplicationEvent | None, ApplicationEvent | None]:
    events = agent_control.list_events(connection, attempt_id=command.attempt_id)
    admitted = next(
        (
            item
            for item in events
            if item.event_type == "application.command.admitted"
            and item.payload.get("command_id") == command.command_id
        ),
        None,
    )
    result = next(
        (
            item
            for item in events
            if item.event_type == "application.command.result"
            and item.payload.get("command_id") == command.command_id
        ),
        None,
    )
    return admitted, result


def _result_state(result: ApplicationCommandResult, command: ApplicationCommand) -> tuple[EpisodeState, str | None, str | None]:
    if result.status == "human_required":
        return "human_required", None, result.outcome
    if command.action == "enqueue_human_handoff":
        return "human_required", None, result.outcome
    if command.action == "park_exception":
        return "parked", result.outcome, None
    if result.status in {"effect_unknown", "parked"}:
        return "parked", result.outcome, None
    if command.action == "enqueue_receipt_reconciliation":
        return "receipt_only", result.outcome, None
    if result.status == "verified":
        return ("recovering" if command.kind == "recovery" else "checkpoint"), None, None
    return "checkpoint", None, None


def _record_result(
    connection: sqlite3.Connection,
    current: ApplicationEpisode,
    command: ApplicationCommand,
    result: ApplicationCommandResult,
) -> ApplicationCommandResult:
    state, terminal_reason, human_reason = _result_state(result, command)
    event = ApplicationEvent(
        event_id=result.result_id,
        attempt_id=command.attempt_id,
        run_id=command.run_id,
        phase="recover" if command.kind == "recovery" else "act",
        actor="application-episode",
        event_type="application.command.result",
        payload=result.event_payload(),
        evidence_refs=(f"evidence:{command.evidence_digest}",),
        idempotency_key=result.result_id,
        actor_id=command.actor_id,
        turn_id=command.turn_id,
        schema_version="2",
        occurred_at=result.occurred_at,
    )
    agent_control.append_event(connection, event)
    updated = replace(
        current,
        state=state,
        result_sequence=result.sequence,
        page_epoch=(
            current.page_epoch if result.resulting_page_epoch is None else result.resulting_page_epoch
        ),
        terminal_reason=terminal_reason,
        human_required_reason=human_reason,
        revision=current.revision + 1,
        updated_at=result.occurred_at,
    )
    _update_episode(connection, current, updated)
    connection.commit()
    return result


def execute_application_command(
    connection: sqlite3.Connection,
    command: ApplicationCommand,
    executor: CommandExecutor,
) -> ApplicationCommandResult:
    """Admit once, delegate once, and replay only the durable typed result."""
    if connection.in_transaction:
        raise ValueError("application command execution requires an independent durable transaction")
    ensure_schema(connection)
    admitted, existing_result = _existing_events(connection, command)
    if existing_result is not None:
        return _result_from_event(existing_result, replayed=True)
    current = get_episode(connection, command.episode_id)
    if current is None:
        raise EpisodeConflict("application episode is missing")
    if admitted is not None:
        # The previous owner may have crossed the side-effect boundary. Never
        # call the executor again without a typed verified result.
        replay = ApplicationCommandResult(
            result_id=f"{command.command_id}:result",
            command_id=command.command_id,
            episode_id=command.episode_id,
            sequence=command.sequence,
            actor_id=command.actor_id,
            attempt_id=command.attempt_id,
            run_id=command.run_id,
            turn_id=command.turn_id,
            status="effect_unknown" if command.effect != "none" else "verified",
            outcome="admitted_effect_outcome_unknown" if command.effect != "none" else "replayed_no_effect",
            effect_applied=None if command.effect != "none" else False,
            occurred_at=datetime.now(UTC),
        )
        return _record_result(connection, current, command, replay)
    if (
        command.episode_revision != current.revision
        or command.sequence != current.command_sequence + 1
        or command.evidence_digest != current.evidence_digest
        or command.actor_id != current.actor_id
        or command.attempt_id != current.attempt_id
    ):
        raise EpisodeConflict("application command episode binding is stale")
    admitted_event = ApplicationEvent(
        event_id=f"{command.command_id}:admitted",
        attempt_id=command.attempt_id,
        run_id=command.run_id,
        phase="recover" if command.kind == "recovery" else "act",
        actor="application-episode",
        event_type="application.command.admitted",
        payload={"command_id": command.command_id, **command.event_payload()},
        evidence_refs=(f"evidence:{command.evidence_digest}",),
        idempotency_key=command.idempotency_key,
        actor_id=command.actor_id,
        turn_id=command.turn_id,
        schema_version="2",
        occurred_at=command.created_at,
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent_control.append_event(connection, admitted_event)
        admitted_episode = replace(
            current,
            state="recovering" if command.kind == "recovery" else current.state,
            command_sequence=command.sequence,
            revision=current.revision + 1,
            updated_at=command.created_at,
        )
        _update_episode(connection, current, admitted_episode)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    try:
        result = executor(command)
        if not isinstance(result, ApplicationCommandResult):
            raise TypeError("application command executor returned an invalid result")
        if (
            result.command_id != command.command_id
            or result.episode_id != command.episode_id
            or result.sequence != command.sequence
            or result.actor_id != command.actor_id
            or result.attempt_id != command.attempt_id
        ):
            raise EpisodeConflict("application command result identity drifted")
    except Exception as exc:  # noqa: BLE001 - injected executor is an effect boundary
        result = ApplicationCommandResult(
            result_id=f"{command.command_id}:result",
            command_id=command.command_id,
            episode_id=command.episode_id,
            sequence=command.sequence,
            actor_id=command.actor_id,
            attempt_id=command.attempt_id,
            run_id=command.run_id,
            turn_id=command.turn_id,
            status="effect_unknown" if command.effect != "none" else "failed_no_effect",
            outcome=f"executor_{type(exc).__name__.casefold()}",
            effect_applied=None if command.effect != "none" else False,
            occurred_at=datetime.now(UTC),
        )
    refreshed = get_episode(connection, command.episode_id)
    if refreshed is None:
        raise EpisodeConflict("application episode disappeared after command admission")
    return _record_result(connection, refreshed, command, result)


def _descriptor_claim(descriptor: ControlDescriptor) -> dict[str, object]:
    return {
        "descriptor_id": descriptor.descriptor_id,
        "provider": descriptor.provider,
        "kind": descriptor.kind,
        "semantic": descriptor.semantic,
        "required": descriptor.required,
        "writable": descriptor.writable,
        "stateful": descriptor.stateful,
        "options": descriptor.options,
        "options_truncated": descriptor.options_truncated,
        "locator_digest": descriptor.locator_digest,
    }


def diff_form_inspections(before: FormInspection, after: FormInspection) -> FormDiff:
    if (
        before.provider != after.provider
        or before.context.actor_id != after.context.actor_id
        or before.context.attempt_id != after.context.attempt_id
        or before.context.application_session_id != after.context.application_session_id
        or before.page_binding.page_id != after.page_binding.page_id
        or before.page_binding.page_lease_id != after.page_binding.page_lease_id
    ):
        raise EpisodeParked("form diff application or page identity changed")
    if after.page_binding.page_epoch < before.page_binding.page_epoch:
        raise EpisodeParked("form diff page epoch moved backwards")
    left = {item.descriptor_id: _descriptor_claim(item) for item in before.controls}
    right = {item.descriptor_id: _descriptor_claim(item) for item in after.controls}
    added = tuple(sorted(set(right) - set(left)))
    removed = tuple(sorted(set(left) - set(right)))
    changed = tuple(sorted(key for key in set(left) & set(right) if left[key] != right[key]))
    return FormDiff(
        provider=before.provider,
        before_digest=_digest(left),
        after_digest=_digest(right),
        added=added,
        removed=removed,
        changed=changed,
        before_page_epoch=before.page_binding.page_epoch,
        after_page_epoch=after.page_binding.page_epoch,
    )


def bounded_form_replan(
    episode: ApplicationEpisode,
    command: ApplicationCommand,
    *,
    before: FormInspection,
    after: FormInspection,
    now: datetime | None = None,
) -> ReplanDecision:
    """Authorize at most one same-application replacement from a fresh P2 census."""
    diff = diff_form_inspections(before, after)
    if episode.replan_count >= MAX_REPLANS:
        return ReplanDecision(False, "replan_budget_exhausted", diff)
    if command.kind != "browser_control" or command.effect != "browser":
        return ReplanDecision(False, "replan_requires_browser_command", diff)
    if (
        not provider_supports(episode.provider, "application_episode")
        or after.provider != episode.provider
    ):
        return ReplanDecision(False, "replan_provider_unsupported_or_changed", diff)
    if (
        after.context.actor_id != episode.actor_id
        or after.context.attempt_id != episode.attempt_id
        or after.context.application_session_id != episode.application_session_id
        or after.page_binding.page_lease_id != episode.page_lease_id
    ):
        return ReplanDecision(False, "replan_episode_binding_changed", diff)
    if diff.size > MAX_FORM_DIFF:
        return ReplanDecision(False, "replan_diff_too_large", diff)
    if command.descriptor_id is None:
        return ReplanDecision(False, "replan_descriptor_missing", diff)
    try:
        descriptor = after.require(command.descriptor_id)
    except ControlInspectionDenied:
        return ReplanDecision(False, "replan_descriptor_absent_or_ambiguous", diff)
    if descriptor.kind == "final_submit" or not descriptor.writable or descriptor.options_truncated:
        return ReplanDecision(False, "replan_descriptor_not_safely_writable", diff)
    advanced = replace(
        episode,
        page_epoch=after.page_binding.page_epoch,
        replan_count=episode.replan_count + 1,
        revision=episode.revision + 1,
        updated_at=now or datetime.now(UTC),
    )
    replacement = application_command(
        advanced,
        kind="browser_control",
        action=command.action,
        descriptor=descriptor,
        value_ref=command.value_ref,
        now=now,
    )
    return ReplanDecision(True, "same_application_descriptor_reobserved", diff, replacement)


def persist_bounded_form_replan(
    connection: sqlite3.Connection,
    episode: ApplicationEpisode,
    decision: ReplanDecision,
) -> tuple[ApplicationEpisode, ApplicationCommand]:
    """Durably consume the one-shot replan budget before replacement execution."""
    replacement = decision.replacement
    if not decision.authorized or replacement is None:
        raise EpisodeParked(decision.reason)
    current = get_episode(connection, episode.episode_id)
    if current is None or current != episode:
        raise EpisodeConflict("application episode changed before replan admission")
    if current.replan_count >= MAX_REPLANS:
        raise EpisodeParked("replan_budget_exhausted")
    updated = replace(
        current,
        page_epoch=decision.diff.after_page_epoch,
        replan_count=current.replan_count + 1,
        revision=current.revision + 1,
        updated_at=replacement.created_at,
    )
    if (
        replacement.episode_revision != updated.revision
        or replacement.sequence != updated.command_sequence + 1
        or replacement.expected_page_epoch != updated.page_epoch
    ):
        raise EpisodeConflict("replacement command does not match the admitted replan")
    try:
        _update_episode(connection, current, updated)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return updated, replacement


def command_result(
    command: ApplicationCommand,
    *,
    status: CommandStatus,
    outcome: str,
    effect_applied: bool | None,
    result_ref: str | None = None,
    resulting_page_epoch: int | None = None,
    occurred_at: datetime | None = None,
) -> ApplicationCommandResult:
    return ApplicationCommandResult(
        result_id=f"{command.command_id}:result",
        command_id=command.command_id,
        episode_id=command.episode_id,
        sequence=command.sequence,
        actor_id=command.actor_id,
        attempt_id=command.attempt_id,
        run_id=command.run_id,
        turn_id=command.turn_id,
        status=status,
        outcome=outcome,
        effect_applied=effect_applied,
        result_ref=result_ref,
        resulting_page_epoch=resulting_page_epoch,
        occurred_at=occurred_at or datetime.now(UTC),
    )


__all__ = [
    "ApplicationCommand",
    "ApplicationCommandResult",
    "ApplicationEpisode",
    "EpisodeConflict",
    "EpisodeParked",
    "FactEvidence",
    "FormDiff",
    "JobEvidenceBundle",
    "ReplanDecision",
    "application_command",
    "bounded_form_replan",
    "build_job_evidence_bundle",
    "command_result",
    "create_episode",
    "diff_form_inspections",
    "ensure_schema",
    "episode_from_job",
    "execute_application_command",
    "get_episode",
    "latest_episode_for_attempt",
    "persist_bounded_form_replan",
    "persist_job_evidence_bundle",
]
