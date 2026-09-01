"""Field-risk and explicitly registered safe-default policy."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

FieldRisk = Literal["low", "medium", "high"]
_RANK: dict[FieldRisk, int] = {"low": 0, "medium": 1, "high": 2}
_RULE_SEAL_KEY = secrets.token_bytes(32)
_ACTIVE_ISSUERS: set[str] = set()
_HIGH_MARKERS = (
    "declaration",
    "attestation",
    "citizenship",
    "nationality",
    "country of birth",
    "date of birth",
    "gender",
    "work authorization",
    "sponsorship",
    "criminal",
    "identity",
    "passport",
    "security clearance",
)
_MEDIUM_MARKERS = (
    "degree",
    "education",
    "qualification",
    "certification",
    "license",
    "experience",
    "availability",
    "salary",
)
_EXPIRY_REQUIRED_MARKERS = (
    "availability",
    "available",
    "salary",
    "compensation",
    "work authorization",
    "sponsorship",
    "visa",
    "current employment",
)


def _token(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split())


def context_binding(context: Mapping[str, object]) -> str:
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def field_risk(
    semantic: str,
    *,
    adapter_risk: FieldRisk | None = None,
    direct_impact: bool = False,
    declaration: bool = False,
) -> FieldRisk:
    token = _token(semantic)
    core: FieldRisk = "low"
    if declaration or any(marker in token for marker in _HIGH_MARKERS):
        core = "high"
    elif direct_impact or any(marker in token for marker in _MEDIUM_MARKERS):
        core = "medium"
    if adapter_risk is None:
        return core
    return adapter_risk if _RANK[adapter_risk] > _RANK[core] else core


def sensitivity_satisfies_risk(*, sensitivity: FieldRisk, risk: FieldRisk) -> bool:
    return _RANK[sensitivity] >= _RANK[risk]


def field_requires_expiry(semantic: str) -> bool:
    token = _token(semantic)
    return any(marker in token for marker in _EXPIRY_REQUIRED_MARKERS)


@dataclass(frozen=True, slots=True)
class SafeDefaultRule:
    rule_id: str
    adapter: str
    adapter_version: str
    field_semantic: str
    context_digest: str
    value: object
    risk: FieldRisk = "low"
    _issuer: str = field(default="", repr=False, compare=False)
    _seal: str = field(default="", repr=False, compare=False)

    def matches(
        self,
        *,
        adapter: str,
        adapter_version: str,
        field_semantic: str,
        context: Mapping[str, object],
    ) -> bool:
        claims = _rule_claims(self, issuer=self._issuer)
        valid_seal = self._issuer in _ACTIVE_ISSUERS and hmac.compare_digest(
            self._seal, _rule_seal(claims)
        )
        return (
            valid_seal
            and self.adapter != "generic"
            and self.adapter == adapter
            and self.adapter_version == adapter_version
            and _token(self.field_semantic) == _token(field_semantic)
            and self.context_digest == context_binding(context)
            and self.risk == "low"
        )


class SafeDefaultRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, SafeDefaultRule] = {}
        self._issuer = secrets.token_hex(16)
        _ACTIVE_ISSUERS.add(self._issuer)

    def register(self, rule: SafeDefaultRule) -> None:
        if not rule.rule_id.strip() or rule.adapter == "generic":
            raise ValueError("safe defaults require a named non-generic adapter")
        if rule.rule_id in self._rules:
            raise ValueError(f"safe default already registered: {rule.rule_id}")
        claims = _rule_claims(rule, issuer=self._issuer)
        self._rules[rule.rule_id] = replace(
            rule,
            _issuer=self._issuer,
            _seal=_rule_seal(claims),
        )

    def get(self, rule_id: str) -> SafeDefaultRule | None:
        return self._rules.get(rule_id)


def _rule_claims(rule: SafeDefaultRule, *, issuer: str) -> dict[str, object]:
    return {
        "rule_id": rule.rule_id,
        "adapter": rule.adapter,
        "adapter_version": rule.adapter_version,
        "field_semantic": rule.field_semantic,
        "context_digest": rule.context_digest,
        "value": rule.value,
        "risk": rule.risk,
        "issuer": issuer,
    }


def _rule_seal(claims: Mapping[str, object]) -> str:
    encoded = json.dumps(
        claims,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hmac.new(_RULE_SEAL_KEY, encoded, hashlib.sha256).hexdigest()
