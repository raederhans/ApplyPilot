"""Classify identity-related application requirements without a blanket stop.

The classifier is provider-neutral and side-effect free. It distinguishes
ordinary confirmed facts from protected identifiers, document artifacts,
biometric/media requests, and financial identity data so callers can expose
only the capability required by the actual field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

IdentityRequirementKind = Literal[
    "not_identity",
    "ordinary_fact",
    "protected_identifier",
    "document_artifact",
    "biometric_or_media",
    "financial_identity",
]


@dataclass(frozen=True, slots=True)
class IdentityRequirement:
    kind: IdentityRequirementKind
    handling: str
    allows_approximation: bool = False
    requires_secure_source: bool = False
    requires_verified_artifact: bool = False
    requires_human: bool = False


_SPACE_RE = re.compile(r"\s+")
_BIOMETRIC = re.compile(
    r"\b(?:biometric|selfie|face scan|facial recognition|fingerprint|"
    r"video(?: identity)? verification|audio(?: identity)? verification|"
    r"(?:record|upload)(?: a)? (?:video|audio)|voice recording|camera|microphone)\b",
    re.IGNORECASE,
)
_FINANCIAL = re.compile(
    r"\b(?:bank account|credit card|debit card|payment account|routing number|"
    r"tax identification|tax id)\b",
    re.IGNORECASE,
)
_PROTECTED_IDENTIFIER = re.compile(
    r"\b(?:passport (?:no\.?|number)|national id(?:entification)? (?:no\.?|number)|"
    r"identity (?:no\.?|number)|identification (?:no\.?|number)|nric|fin number|"
    r"social security|ssn|sin)\b",
    re.IGNORECASE,
)
_IDENTITY_DOCUMENT = re.compile(
    r"\b(?:passport|national id|identity document|identification document|"
    r"government[- ]issued id|work permit|employment pass|residence permit|"
    r"visa document|proof of (?:identity|citizenship|work authori[sz]ation|eligibility))\b",
    re.IGNORECASE,
)
_ORDINARY_FACT = re.compile(
    r"\b(?:legal name|full name|given name|first name|family name|last name|surname|"
    r"nationality|citizenship|country of citizenship|work authori[sz]ation|"
    r"right to work|visa sponsorship|sponsorship required|work permit status|"
    r"residency status|date of birth)\b",
    re.IGNORECASE,
)


def classify_identity_requirement(
    label: object,
    *,
    field_type: str = "text",
) -> IdentityRequirement:
    """Return the narrow handling contract for one observed field."""
    text = _SPACE_RE.sub(" ", str(label or "")).strip()
    normalized_type = str(field_type or "text").strip().casefold()
    if _BIOMETRIC.search(text):
        return IdentityRequirement(
            "biometric_or_media",
            "human_gate",
            requires_human=True,
        )
    if _FINANCIAL.search(text):
        return IdentityRequirement(
            "financial_identity",
            "human_gate",
            requires_human=True,
        )
    if normalized_type in {"file", "file_upload"} and _IDENTITY_DOCUMENT.search(text):
        return IdentityRequirement(
            "document_artifact",
            "verified_explicitly_authorized_artifact",
            requires_verified_artifact=True,
        )
    if _PROTECTED_IDENTIFIER.search(text):
        return IdentityRequirement(
            "protected_identifier",
            "secure_exact_source",
            requires_secure_source=True,
        )
    if _ORDINARY_FACT.search(text):
        return IdentityRequirement(
            "ordinary_fact",
            "confirmed_fact",
        )
    return IdentityRequirement("not_identity", "ordinary_field")


def identity_requirement_is_satisfied(
    requirement: IdentityRequirement,
    *,
    value_present: bool = False,
    confirmed_source: bool = False,
    artifact_verified: bool = False,
    explicitly_authorized: bool = False,
) -> bool:
    """Check whether the exact requirement can proceed without approximation."""
    if requirement.kind == "not_identity":
        return value_present
    if requirement.kind == "ordinary_fact":
        return value_present and confirmed_source
    if requirement.kind == "protected_identifier":
        return value_present and confirmed_source
    if requirement.kind == "document_artifact":
        return value_present and artifact_verified and explicitly_authorized
    return False
