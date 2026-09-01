"""Provider-neutral resolution of lossy ATS answer choices.

The resolver is deliberately side-effect free.  It does not know about ATS
vendors, pages, or jobs; it compares an observed field with a confirmed fact
and returns an auditable recommendation for an orchestrator to execute.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from applypilot.apply.answer_policy import (
    FieldRisk,
    SafeDefaultRule,
    field_requires_expiry,
    field_risk,
    sensitivity_satisfies_risk,
)
from applypilot.apply.application_facts import FactResolution

Relation = Literal[
    "exact",
    "alias",
    "broader",
    "containing_bucket",
    "truthful_negative",
    "preference",
    "closest_non_equivalent",
    "contradiction",
]
Action = Literal[
    "select",
    "select_and_record",
    "answer_negative_and_continue",
    "enter_value",
    "research_then_select",
    "continue_unanswered",
    "review",
]
FailureCode = Literal["answer_policy_unresolved", "unsupported_legal_declaration"]

_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_CJK_LABEL_SEPARATOR = r"(?:\s|[，,。.;；:：/／|｜\-–—()（）\[\]【】])"
_NUMBER_RE = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)")
_RANGE_RE = re.compile(
    r"\$?\s*(\d+(?:\.\d+)?)\s*(?:-|to|–|—)\s*\$?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_NEGATIVE = frozenset({"no", "none", "not applicable", "n/a", "0", "zero"})
_OTHER = frozenset({"other", "others", "other (please specify)", "not listed"})
_SENSITIVE_SEMANTIC_MARKERS = (
    "password",
    "passcode",
    "otp",
    "one time code",
    "one time password",
    "verification code",
    "security code",
    "identity number",
    "identification number",
    "national id",
    "passport number",
    "social security",
    "ssn",
    "nric",
    "fin number",
)


class SensitiveAnswerError(ValueError):
    """Raised before a sensitive field or value can enter resolution/audit."""


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    """Inputs available at answer-decision time.

    ``direct_impact`` means an incorrect answer can materially change
    eligibility or the truth of the application.  ``declaration`` is stronger:
    the control is part of an attestation or legal declaration.
    """

    field_semantic: str
    options: tuple[str, ...] = ()
    confirmed_fact: object | None = None
    fact_resolution: FactResolution | None = None
    aliases: tuple[str, ...] = ()
    required: bool = False
    direct_impact: bool = False
    declaration: bool = False
    can_explain: bool = False
    preference: bool = False
    context: Mapping[str, object] = field(default_factory=dict)
    adapter: str = "generic"
    adapter_version: str = "unknown"
    adapter_risk: FieldRisk | None = None
    safe_default_rule: SafeDefaultRule | None = None


@dataclass(frozen=True, slots=True)
class AnswerResolution:
    relation: Relation
    action: Action
    selected_option: str | None
    value: object | None
    confidence: float
    audit: Mapping[str, object]


def _text(value: object) -> str:
    return _SPACE_RE.sub(" ", str("" if value is None else value)).strip()


def _token(value: object) -> str:
    return " ".join(_TOKEN_RE.sub(" ", _text(value).casefold()).split())


def _fact_value(fact: object | None) -> object | None:
    if isinstance(fact, Mapping):
        return fact.get("value")
    return fact


def _audit(request: AnswerRequest, reason: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "field_semantic": request.field_semantic,
        "available_options": list(request.options),
        "required": request.required,
        "direct_impact": request.direct_impact,
        "declaration": request.declaration,
        "can_explain": request.can_explain,
        "reason": reason,
    }
    if request.fact_resolution and request.fact_resolution.fact_ref:
        payload["fact_ref"] = request.fact_resolution.fact_ref
    payload.update(extra)
    return payload


def _result(
    request: AnswerRequest,
    relation: Relation,
    action: Action,
    *,
    option: str | None = None,
    value: object | None = None,
    confidence: float,
    reason: str,
    fact_ref: str | None = None,
    safe_default_rule_id: str | None = None,
) -> AnswerResolution:
    provenance = fact_ref or (request.fact_resolution.fact_ref if request.fact_resolution else None)
    if provenance is None and request.confirmed_fact is not None:
        provenance = "legacy:confirmed_fact"
    if option is not None and provenance is None and safe_default_rule_id is None:
        raise ValueError("automatic selection requires fact or safe-default provenance")
    failure_code = _failure_code(request, action)
    return AnswerResolution(
        relation=relation,
        action=action,
        selected_option=option,
        value=value,
        confidence=confidence,
        audit=_audit(
            request,
            reason,
            selected_option=option,
            relation=relation,
            action=action,
            confidence=confidence,
            **({"failure_code": failure_code} if failure_code else {}),
            **({"fact_ref": provenance} if provenance else {}),
            **({"safe_default_rule_id": safe_default_rule_id} if safe_default_rule_id else {}),
        ),
    )


def _failure_code(request: AnswerRequest, action: Action) -> FailureCode | None:
    """Return a bounded failure code for an actually unresolved required answer."""

    automatic_actions = {
        "select",
        "select_and_record",
        "answer_negative_and_continue",
        "enter_value",
    }
    if action in automatic_actions or not request.required:
        return None
    risk = field_risk(
        request.field_semantic,
        adapter_risk=request.adapter_risk,
        direct_impact=request.direct_impact,
        declaration=request.declaration,
    )
    if action == "review" and (risk == "high" or request.declaration):
        return "unsupported_legal_declaration"
    return "answer_policy_unresolved"


def _degree_level(value: object) -> str | None:
    token = _token(value)
    levels = (
        ("doctorate", ("doctorate", "doctoral", "phd", "doctor of philosophy")),
        ("master", ("master", "msc", "ma", "mba", "mcomp", "meng")),
        ("bachelor", ("bachelor", "bsc", "ba", "beng")),
        ("diploma", ("diploma", "associate")),
    )
    for level, markers in levels:
        if any(marker in token.split() or marker in token for marker in markers):
            return level
    return None


def _is_degree_semantic(semantic: str) -> bool:
    token = _token(semantic)
    return any(marker in token for marker in ("degree", "education", "qualification"))


def is_sensitive_answer_semantic(semantic: object) -> bool:
    """Return whether a field is outside the answer-resolution boundary."""

    token = _token(semantic)
    return any(marker in token for marker in _SENSITIVE_SEMANTIC_MARKERS)


def _degree_family(value: object) -> str | None:
    token = _token(value)
    families = (
        ("science", ("science", "computing", "computer", "technology", "engineering", "data", "ai")),
        ("business", ("business", "management", "finance", "mba")),
        ("arts", ("arts", "humanities", "social science", "ma")),
    )
    for family, markers in families:
        if any(marker in token.split() or marker in token for marker in markers):
            return family
    return None


def _month_interval(value: object) -> tuple[date, date] | None:
    text = _text(value).casefold()
    found = re.findall(
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)?"
        r"\s*(20\d{2})",
        text,
    )
    if not found:
        return None
    points = [date(int(year), _MONTHS.get(month, 1) if month else 1, 1) for month, year in found]
    if len(points) == 1:
        point = points[0]
        return point, date(point.year, 12 if not found[0][0] else point.month, 28)
    return min(points), max(points)


def _fact_interval(fact: object | None) -> tuple[date, date] | None:
    if isinstance(fact, Mapping):
        start = fact.get("start")
        end = fact.get("end")
        if start and end:
            try:
                return date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
            except ValueError:
                return None
    return _month_interval(_fact_value(fact))


def _availability_truth(request: AnswerRequest, fact: object | None) -> bool | None:
    if not any(marker in _token(request.field_semantic) for marker in ("availability", "available")):
        return None
    asked = _month_interval(request.field_semantic)
    actual = _fact_interval(fact)
    if not asked or not actual:
        return None
    return max(asked[0], actual[0]) <= min(asked[1], actual[1])


def _numeric_value(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = _NUMBER_RE.search(_text(value).replace(",", ""))
    return float(match.group(1)) if match else None


def _containing_option(options: Sequence[str], value: object) -> str | None:
    number = _numeric_value(value)
    if number is None:
        return None
    for option in sorted(options, key=lambda item: (_token(item), _text(item))):
        cleaned = _text(option).replace(",", "")
        match = _RANGE_RE.search(cleaned)
        if match and float(match.group(1)) <= number <= float(match.group(2)):
            return option
        lower = re.search(
            r"(?:at least|minimum(?: of)?)\s*\$?\s*(\d+(?:\.\d+)?)|"
            r"\$?\s*(\d+(?:\.\d+)?)\s*(?:years?\s*)?(?:\+|or more|and above|plus)(?:\s|$)",
            cleaned,
            re.IGNORECASE,
        )
        lower_bound = next((group for group in lower.groups() if group is not None), None) if lower else None
        if lower_bound is not None and number >= float(lower_bound):
            return option
        upper = re.search(
            r"(?:under|below|less than|up to)\s*\$?\s*(\d+(?:\.\d+)?)",
            cleaned,
            re.IGNORECASE,
        )
        if upper and number <= float(upper.group(1)):
            return option
    return None


def _first_option(options: Sequence[str], accepted: frozenset[str]) -> str | None:
    accepted_tokens = {_token(value) for value in accepted}
    matches = [option for option in options if _token(option) in accepted_tokens]
    return min(matches, key=lambda option: (_token(option), _text(option))) if matches else None


def _option_polarity(option: object) -> bool | None:
    visible_label = _text(option)
    if re.match(rf"^(?:否|不是|不适用|不同意)(?:$|{_CJK_LABEL_SEPARATOR})", visible_label):
        return False
    if re.match(rf"^(?:是|同意)(?:$|{_CJK_LABEL_SEPARATOR})", visible_label):
        return True
    token = _token(option)
    if re.match(r"^(?:yes|true)\b", token):
        return True
    if (
        re.match(r"^(?:no|false|none|zero|not applicable|n a)\b", token)
        or "none of the above" in token
        or "none of these" in token
    ):
        return False
    return None


def _safe_default(request: AnswerRequest, options: Sequence[str]) -> tuple[str, str] | None:
    rule = request.safe_default_rule
    if rule is None or not rule.matches(
        adapter=request.adapter,
        adapter_version=request.adapter_version,
        field_semantic=request.field_semantic,
        context=request.context,
    ):
        return None
    matches = [option for option in options if _token(option) == _token(rule.value)]
    if not matches:
        return None
    return min(matches, key=lambda option: (_token(option), _text(option))), rule.rule_id


def resolve_answer(request: AnswerRequest) -> AnswerResolution:
    """Resolve a field without fabricating a confirmed fact.

    The default is progress: low-impact unknowns remain resumable and lossy
    classifications may be selected with an audit record.  Review is reserved
    for material contradictions and unknown required high-impact answers.
    """

    if is_sensitive_answer_semantic(request.field_semantic):
        raise SensitiveAnswerError("sensitive credential or identity field cannot be resolved")

    risk = field_risk(
        request.field_semantic,
        adapter_risk=request.adapter_risk,
        direct_impact=request.direct_impact,
        declaration=request.declaration,
    )
    typed = request.fact_resolution
    if typed is not None:
        fact_source = (
            typed.value
            if typed.production_ready
            and sensitivity_satisfies_risk(sensitivity=typed.sensitivity, risk=risk)
            and not (
                risk in {"medium", "high"}
                and field_requires_expiry(request.field_semantic)
                and typed.expires_at is None
            )
            else None
        )
        fact = _fact_value(fact_source)
    else:
        fact_source = request.confirmed_fact
        fact = _fact_value(fact_source)
        if risk in {"medium", "high"}:
            fact_source = None
            fact = None
    options = tuple(option for option in request.options if _text(option))

    fact_known = fact is not None or (
        isinstance(fact_source, Mapping) and fact_source.get("start") is not None and fact_source.get("end") is not None
    )
    if not fact_known or (fact is not None and _text(fact) == ""):
        if request.required and (request.direct_impact or request.declaration):
            return _result(
                request,
                "contradiction",
                "review",
                confidence=1.0,
                reason="required_high_impact_fact_unknown",
            )
        fallback = _safe_default(request, options) if request.required and risk == "low" else None
        if fallback:
            return _result(
                request,
                "closest_non_equivalent",
                "select_and_record",
                option=fallback[0],
                confidence=0.3,
                reason="required_low_impact_uses_registered_safe_default",
                safe_default_rule_id=fallback[1],
            )
        action: Action = "research_then_select" if request.required else "continue_unanswered"
        return _result(
            request,
            "closest_non_equivalent",
            action,
            confidence=0.2,
            reason="low_impact_fact_not_yet_confirmed",
        )

    availability = _availability_truth(request, fact_source)
    if availability is not None:
        matches = [option for option in options if _option_polarity(option) is availability]
        selected = min(matches, key=lambda option: (_token(option), _text(option))) if matches else None
        if selected:
            relation: Relation = "exact" if availability else "truthful_negative"
            action = "select" if availability else "answer_negative_and_continue"
            return _result(
                request,
                relation,
                action,
                option=selected,
                confidence=0.98,
                reason="availability_window_overlap" if availability else "availability_window_does_not_overlap",
            )

    if isinstance(fact, bool):
        matches = [option for option in options if _option_polarity(option) is fact]
        selected = min(matches, key=lambda option: (_token(option), _text(option))) if matches else None
        if selected:
            relation = "exact" if fact else "truthful_negative"
            action = "select" if fact else "answer_negative_and_continue"
            return _result(
                request,
                relation,
                action,
                option=selected,
                confidence=1.0,
                reason="boolean_fact_matches_option",
            )
        other = _first_option(options, _OTHER) if not fact else None
        if other:
            return _result(
                request,
                "truthful_negative",
                "select_and_record",
                option=other,
                value="No / none of the listed choices",
                confidence=0.95,
                reason="truthful_negative_recorded_as_other",
            )
        return _result(
            request,
            "contradiction",
            "review",
            confidence=1.0,
            reason="available_options_would_reverse_boolean_fact",
        )

    fact_token = _token(fact)
    exact_matches = [option for option in options if _text(option).casefold() == _text(fact).casefold()]
    exact = min(exact_matches, key=lambda option: (_token(option), _text(option))) if exact_matches else None
    if exact:
        return _result(request, "exact", "select", option=exact, confidence=1.0, reason="literal_option_match")

    alias_tokens = {_token(alias) for alias in request.aliases}
    if isinstance(fact_source, Mapping):
        alias_tokens.update(_token(alias) for alias in fact_source.get("aliases", ()))
    alias_matches = [option for option in options if _token(option) == fact_token or _token(option) in alias_tokens]
    alias = min(alias_matches, key=lambda option: (_token(option), _text(option))) if alias_matches else None
    if alias:
        return _result(request, "alias", "select", option=alias, confidence=0.98, reason="normalized_or_known_alias")

    bucket = _containing_option(options, fact)
    if bucket:
        relation = "preference" if request.preference else "containing_bucket"
        return _result(
            request, relation, "select", option=bucket, confidence=0.95, reason="value_is_inside_option_bucket"
        )

    if _is_degree_semantic(request.field_semantic):
        level = _degree_level(fact)
        same_level = [option for option in options if _degree_level(option) == level]
        if same_level:
            generic_tokens = {level, f"{level} degree", f"{level}s degree", f"{level} s degree"}
            generic_matches = [option for option in same_level if _token(option) in generic_tokens]
            generic = (
                min(generic_matches, key=lambda option: (_token(option), _text(option))) if generic_matches else None
            )
            if generic:
                return _result(
                    request,
                    "broader",
                    "select_and_record",
                    option=generic,
                    value=fact,
                    confidence=0.94,
                    reason="same_level_generic_degree_category",
                )
            family = _degree_family(fact)
            family_matches = [candidate for candidate in same_level if _degree_family(candidate) == family]
            candidates = family_matches or same_level
            option = min(candidates, key=lambda candidate: (_token(candidate), _text(candidate)))
            if request.declaration:
                return _result(
                    request,
                    "contradiction",
                    "review",
                    option=option,
                    value=fact,
                    confidence=0.9,
                    reason="declared_named_degree_category_is_not_the_confirmed_award",
                )
            return _result(
                request,
                "closest_non_equivalent",
                "select_and_record",
                option=option,
                value=fact,
                confidence=0.76,
                reason="same_level_degree_taxonomy_with_exact_award_preserved",
            )

    other = _first_option(options, _OTHER)
    if other and not request.declaration:
        return _result(
            request,
            "broader",
            "select_and_record",
            option=other,
            value=fact,
            confidence=0.9,
            reason="unlisted_confirmed_fact_preserved_in_explanation",
        )

    negative_matches = [option for option in options if _option_polarity(option) is False]
    negative = min(negative_matches, key=lambda option: (_token(option), _text(option))) if negative_matches else None
    semantic = _token(request.field_semantic)
    fact_is_negative = (isinstance(fact, (int, float)) and not isinstance(fact, bool) and float(fact) <= 0) or _token(
        fact
    ) in {_token(value) for value in _NEGATIVE}
    categorical_none = negative is not None and any(
        phrase in _token(negative) for phrase in ("none of the above", "none of these")
    )
    if (
        negative
        and any(marker in semantic for marker in ("skill", "experience", "proficiency", "certification"))
        and (fact_is_negative or categorical_none)
    ):
        return _result(
            request,
            "truthful_negative",
            "answer_negative_and_continue",
            option=negative,
            confidence=0.95,
            reason="unmatched_capability_can_be_answered_negatively",
        )

    if request.preference:
        if not options:
            return _result(
                request,
                "preference",
                "enter_value",
                value=fact,
                confidence=0.95,
                reason="confirmed_preference_is_not_a_factual_attestation",
            )
        if request.declaration:
            return _result(
                request,
                "contradiction",
                "review",
                value=fact,
                confidence=0.9,
                reason="declared_preference_cannot_be_represented_truthfully",
            )
        return _result(
            request,
            "contradiction",
            "review" if request.required else "continue_unanswered",
            value=fact,
            confidence=0.9,
            reason="preference_has_no_registered_truthful_mapping",
        )

    if not options:
        return _result(request, "exact", "enter_value", value=fact, confidence=1.0, reason="free_text_confirmed_fact")

    if not request.required and not request.direct_impact and not request.declaration:
        return _result(
            request,
            "closest_non_equivalent",
            "continue_unanswered",
            value=fact,
            confidence=0.8,
            reason="optional_low_impact_fact_has_no_truthful_option",
        )

    if request.required and not request.direct_impact and not request.declaration:
        fallback = _safe_default(request, options) if risk == "low" else None
        if fallback:
            return _result(
                request,
                "closest_non_equivalent",
                "select_and_record",
                option=fallback[0],
                value=fact,
                confidence=0.3,
                reason="required_low_impact_uses_registered_safe_default",
                safe_default_rule_id=fallback[1],
            )

    return _result(
        request,
        "contradiction",
        "review",
        confidence=0.9,
        reason="no_available_option_can_represent_confirmed_fact",
    )
