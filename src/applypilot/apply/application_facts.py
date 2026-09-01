"""Typed, current-authority application facts and deterministic resolution."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

FactSensitivity = Literal["low", "medium", "high"]
FactStatus = Literal["resolved", "missing", "out_of_scope", "expired", "conflict"]
_FACT_ISSUER = secrets.token_hex(16)
_FACT_SEAL_KEY = secrets.token_bytes(32)
_TIME_SENSITIVE_MARKERS = (
    "availability",
    "available",
    "salary",
    "compensation",
    "work authorization",
    "work_authorization",
    "sponsorship",
    "visa",
    "current employment",
)


def _instant(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _required_instant(raw: Mapping[str, object], name: str) -> datetime | None:
    if name not in raw or raw.get(name) in (None, ""):
        return None
    parsed = _instant(raw.get(name))
    if parsed is None:
        raise ValueError(f"application fact {name} must be an ISO-8601 timestamp")
    return parsed


@dataclass(frozen=True, slots=True)
class ApplicationFact:
    """One fact from the current profile authority.

    Legacy entries can still be represented, but ``production_ready`` is false
    until source, scope, and freshness are explicit.
    """

    fact_ref: str
    key: str
    value: object
    source: str | None = None
    scope: str | None = None
    confirmed_at: datetime | None = None
    expires_at: datetime | None = None
    sensitivity: FactSensitivity = "medium"
    supersedes: tuple[str, ...] = ()

    @property
    def production_ready(self) -> bool:
        return bool(self.source and self.scope and self.confirmed_at)

    @property
    def time_sensitive(self) -> bool:
        token = f"{self.key} {self.scope or ''}".casefold()
        return any(marker in token for marker in _TIME_SENSITIVE_MARKERS)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object], *, index: int = 0) -> ApplicationFact:
        key = str(raw.get("key") or "").strip()
        fact_ref = str(raw.get("fact_ref") or raw.get("id") or f"profile:{key}:{index}").strip()
        if not key or not fact_ref or "value" not in raw:
            raise ValueError("application fact requires key, fact_ref, and value")
        sensitivity = str(raw.get("sensitivity") or "medium").casefold()
        if sensitivity not in {"low", "medium", "high"}:
            raise ValueError("invalid fact sensitivity")
        supersedes_raw = raw.get("supersedes") or ()
        supersedes = (
            tuple(str(item).strip() for item in supersedes_raw if str(item).strip())
            if isinstance(supersedes_raw, (list, tuple))
            else ()
        )
        return cls(
            fact_ref=fact_ref,
            key=key,
            value=raw["value"],
            source=str(raw.get("source") or "").strip() or None,
            scope=str(raw.get("scope") or raw.get("context") or "").strip() or None,
            confirmed_at=_required_instant(raw, "confirmed_at"),
            expires_at=_required_instant(raw, "expires_at"),
            sensitivity=sensitivity,  # type: ignore[arg-type]
            supersedes=supersedes,
        )


@dataclass(frozen=True, slots=True)
class FactResolution:
    status: FactStatus
    key: str
    value: object | None = None
    fact_ref: str | None = None
    sensitivity: FactSensitivity = "medium"
    reason: str = ""
    expires_at: datetime | None = None
    _issuer: str = field(default="", repr=False, compare=False)
    _seal: str = field(default="", repr=False, compare=False)

    @property
    def production_ready(self) -> bool:
        if self.status != "resolved" or self.fact_ref is None or self._issuer != _FACT_ISSUER:
            return False
        expected = _resolution_seal(
            status=self.status,
            key=self.key,
            value=self.value,
            fact_ref=self.fact_ref,
            sensitivity=self.sensitivity,
            reason=self.reason,
            expires_at=self.expires_at,
            issuer=self._issuer,
        )
        return hmac.compare_digest(self._seal, expected)


def _resolution_seal(**claims: object) -> str:
    encoded = json.dumps(
        claims,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hmac.new(_FACT_SEAL_KEY, encoded, hashlib.sha256).hexdigest()


def current_profile_facts(profile: Mapping[str, object]) -> tuple[ApplicationFact, ...]:
    """Read only current ``application_facts``; revision history is not authority."""

    raw_facts = profile.get("application_facts")
    if not isinstance(raw_facts, list):
        return ()
    facts: list[ApplicationFact] = []
    for index, raw in enumerate(raw_facts):
        if isinstance(raw, Mapping):
            facts.append(ApplicationFact.from_mapping(raw, index=index))
    return tuple(facts)


def _supersession_has_cycle(facts: Iterable[ApplicationFact]) -> bool:
    adjacency = {fact.fact_ref: fact.supersedes for fact in facts}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(fact_ref: str) -> bool:
        if fact_ref in visiting:
            return True
        if fact_ref in visited:
            return False
        visiting.add(fact_ref)
        if any(visit(parent_ref) for parent_ref in adjacency[fact_ref]):
            return True
        visiting.remove(fact_ref)
        visited.add(fact_ref)
        return False

    return any(visit(fact_ref) for fact_ref in adjacency)


def resolve_application_fact(
    facts: Iterable[ApplicationFact],
    *,
    key: str,
    scope: str,
    at: datetime | None = None,
    minimum_sensitivity: FactSensitivity = "low",
) -> FactResolution:
    """Resolve an exact-scope fact with deterministic expiry and supersession."""

    now = (at or datetime.now(UTC)).astimezone(UTC)
    keyed = [fact for fact in facts if fact.key == key]
    if not keyed:
        return FactResolution("missing", key, reason="fact_key_missing")
    scoped = [fact for fact in keyed if fact.scope == scope]
    if not scoped:
        return FactResolution("out_of_scope", key, reason="exact_scope_missing")
    refs = [fact.fact_ref for fact in scoped]
    if len(set(refs)) != len(refs):
        return FactResolution("conflict", key, reason="duplicate_fact_ref")
    superseded = {ref for fact in scoped for ref in fact.supersedes}
    if superseded - set(refs):
        return FactResolution("conflict", key, reason="dangling_supersession_reference")
    if _supersession_has_cycle(scoped):
        return FactResolution("conflict", key, reason="supersession_cycle")
    heads = [fact for fact in scoped if fact.fact_ref not in superseded]
    if not heads:
        return FactResolution("conflict", key, reason="supersession_cycle_or_empty_head")
    if len(heads) != 1:
        return FactResolution("conflict", key, reason="multiple_active_lineage_heads")
    chosen = heads[0]
    if chosen.expires_at is not None and chosen.expires_at <= now:
        return FactResolution("expired", key, reason="lineage_head_expired")
    rank = {"low": 0, "medium": 1, "high": 2}
    required_rank = rank[minimum_sensitivity]
    if (
        not chosen.production_ready
        or chosen.confirmed_at is None
        or chosen.confirmed_at > now
        or rank[chosen.sensitivity] < required_rank
        or (chosen.time_sensitive and chosen.sensitivity in {"medium", "high"} and chosen.expires_at is None)
    ):
        return FactResolution("missing", key, reason="fact_lacks_scope_source_or_freshness")
    resolution_claims = {
        "status": "resolved",
        "key": key,
        "value": chosen.value,
        "fact_ref": chosen.fact_ref,
        "sensitivity": chosen.sensitivity,
        "reason": "current_profile_fact_resolved",
        "expires_at": chosen.expires_at,
    }
    seal_claims = {**resolution_claims, "issuer": _FACT_ISSUER}
    return FactResolution(
        **resolution_claims,
        _issuer=_FACT_ISSUER,
        _seal=_resolution_seal(**seal_claims),
    )


def resolve_application_fact_ref(
    facts: Iterable[ApplicationFact],
    *,
    fact_ref: str,
    scope: str,
    at: datetime | None = None,
    minimum_sensitivity: FactSensitivity = "low",
) -> FactResolution:
    facts_tuple = tuple(facts)
    referenced = [fact for fact in facts_tuple if fact.fact_ref == fact_ref]
    if len(referenced) != 1:
        return FactResolution("missing", "", reason="fact_ref_missing_or_ambiguous")
    resolution = resolve_application_fact(
        facts_tuple,
        key=referenced[0].key,
        scope=scope,
        at=at,
        minimum_sensitivity=minimum_sensitivity,
    )
    if resolution.status == "resolved" and resolution.fact_ref != fact_ref:
        return FactResolution("conflict", referenced[0].key, reason="fact_ref_is_superseded")
    return resolution
