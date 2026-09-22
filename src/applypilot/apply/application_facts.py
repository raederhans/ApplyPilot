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


def _normalized_policy_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _profile_fact_policy(profile: Mapping[str, object]) -> tuple[list[dict], set[str], set[str]]:
    """Validate profile-owned tombstones and current lineage, without exposing history."""
    raw_facts = profile.get("application_facts", [])
    if not isinstance(raw_facts, list):
        raw_facts = []  # Preserve the legacy empty-facts behavior.
    facts = []
    for index, raw in enumerate(raw_facts):
        if isinstance(raw, Mapping):
            fact = dict(raw)
            if "application_fact_retirements" in profile:
                # Materialize the parser's exact ref before filtering entries,
                # so non-mapping entries cannot shift implicit fact identities.
                fact["fact_ref"] = ApplicationFact.from_mapping(raw, index=index).fact_ref
            facts.append(fact)
    refs: dict[str, dict] = {}
    for raw in facts:
        ref = str(raw.get("fact_ref") or raw.get("id") or "").strip()
        if ref:
            if ref in refs:
                raise ValueError("duplicate current application fact_ref")
            ApplicationFact.from_mapping(raw)
            refs[ref] = raw

    retirements = profile.get("application_fact_retirements", [])
    if not isinstance(retirements, list):
        raise ValueError("application_fact_retirements must be a list")  # noqa: TRY004 - profile validation contract
    retired_refs: set[str] = set()
    retired_keys: set[str] = set()
    retired_values: set[str] = set()
    retired_fragments: set[str] = set()
    targets: dict[str, str] = {}
    for retirement in retirements:
        if not isinstance(retirement, Mapping):
            raise ValueError("application fact retirement must be an object")  # noqa: TRY004 - profile validation contract
        ref = retirement.get("fact_ref")
        target = retirement.get("superseded_by")
        if not isinstance(ref, str) or not ref.strip() or not isinstance(target, str) or not target.strip():
            raise ValueError("application fact retirement requires fact_ref and superseded_by")
        ref, target = ref.strip(), target.strip()
        if ref in retired_refs or ref in refs:
            raise ValueError("duplicate or revived retired application fact_ref")
        if target not in refs:
            raise ValueError("retirement superseded_by must reference a current application fact")
        retired_refs.add(ref)
        targets[ref] = target
        if _required_instant(retirement, "retired_at") is None:
            raise ValueError("application fact retirement requires retired_at")
        for name, destination in (
            ("keys", retired_keys),
            ("normalized_values", retired_values),
            ("normalized_fragments", retired_fragments),
        ):
            values = retirement.get(name, [])
            if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"retirement {name} must be a list of nonempty strings")
            destination.update(_normalized_policy_text(value) for value in values)

    adjacency: dict[str, tuple[str, ...]] = {}
    superseded: set[str] = set()
    for raw in facts:
        ref = str(raw.get("fact_ref") or raw.get("id") or "").strip()
        parents = raw.get("supersedes", [])
        if not isinstance(parents, (list, tuple)) or any(not isinstance(parent, str) or not parent.strip() for parent in parents):
            raise ValueError("application fact supersedes must contain nonempty fact refs")
        if parents and not ref:
            raise ValueError("superseding application facts require an explicit fact_ref")
        current_parents = []
        for parent in parents:
            parent = parent.strip()
            if parent in retired_refs:
                if targets[parent] != ref:
                    raise ValueError("retirement superseded_by conflicts with current lineage")
                continue
            if parent not in refs:
                raise ValueError("dangling application fact supersession reference")
            previous = refs[parent]
            if (raw.get("key"), raw.get("scope") or raw.get("context")) != (previous.get("key"), previous.get("scope") or previous.get("context")):
                raise ValueError("application fact supersession must preserve key and scope")
            current_parents.append(parent)
            superseded.add(parent)
        if ref:
            adjacency[ref] = tuple(current_parents)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit_ref(ref: str) -> None:
        if ref in visiting:
            raise ValueError("application fact supersession cycle")
        if ref in visited:
            return
        visiting.add(ref)
        for parent in adjacency[ref]:
            visit_ref(parent)
        visiting.remove(ref)
        visited.add(ref)

    for ref in adjacency:
        visit_ref(ref)
    if any(target in superseded for target in targets.values()):
        raise ValueError("retirement superseded_by must reference an active current fact")
    active_groups: set[tuple[str, str]] = set()
    for ref, raw in refs.items():
        if ref in superseded:
            continue
        group = (str(raw.get("key") or ""), str(raw.get("scope") or raw.get("context") or ""))
        if group in active_groups:
            raise ValueError("multiple active application fact lineage heads")
        active_groups.add(group)

    def scan(value: object, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if _normalized_policy_text(str(key)) in retired_keys:
                    raise ValueError(f"Profile contains retired application fact key at {path}.{key}")
                if key == "key" and isinstance(child, str) and _normalized_policy_text(child) in retired_keys:
                    raise ValueError(f"Profile contains retired application fact key at {path}.key")
                if key in {"fact_ref", "id"} and isinstance(child, str) and child.strip() in retired_refs:
                    raise ValueError(f"Profile contains retired application fact_ref at {path}.{key}")
                scan(child, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                scan(child, f"{path}[{index}]")
        elif isinstance(value, str):
            normalized = _normalized_policy_text(value)
            if normalized in retired_values or any(fragment in normalized for fragment in retired_fragments):
                raise ValueError(f"Profile contains retired application fact value at {path}")

    # Tombstones are metadata; they are deliberately excluded from answer data.
    for key, value in profile.items():
        if key != "application_fact_retirements":
            if _normalized_policy_text(str(key)) in retired_keys:
                raise ValueError(f"Profile contains retired application fact key at {key}")
            scan(value, str(key))
    return facts, superseded, retired_refs


def validate_profile_fact_policy(profile: Mapping[str, object]) -> Mapping[str, object]:
    """Enforce only the retirement policy supplied by this profile's owner."""
    _profile_fact_policy(profile)
    return profile


def current_visible_fact_mappings(profile: Mapping[str, object]) -> list[dict]:
    """Project current lineage heads for legacy prompt and evidence consumers."""
    facts, superseded, _ = _profile_fact_policy(profile)
    visible = []
    for fact in facts:
        ref = str(fact.get("fact_ref") or fact.get("id") or "").strip()
        if ref not in superseded:
            # Retired references are lineage metadata, never prompt evidence.
            fact.pop("supersedes", None)
            visible.append(fact)
    return visible


def current_profile_facts(profile: Mapping[str, object]) -> tuple[ApplicationFact, ...]:
    """Read only current ``application_facts``; revision history is not authority."""

    # Keep the typed parser's established conflict-reporting contract: callers
    # resolve ambiguous legacy lineages through FactResolution, not exceptions.
    # Profiles opting into tombstones must validate their policy before use.
    retired_refs: set[str] = set()
    if "application_fact_retirements" in profile:
        raw_facts, _, retired_refs = _profile_fact_policy(profile)
    else:
        raw_facts = profile.get("application_facts")
        if not isinstance(raw_facts, list):
            return ()
    facts: list[ApplicationFact] = []
    for index, raw in enumerate(raw_facts):
        if isinstance(raw, Mapping):
            if retired_refs:
                raw = {**raw, "supersedes": [ref for ref in raw.get("supersedes", []) if ref not in retired_refs]}
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
