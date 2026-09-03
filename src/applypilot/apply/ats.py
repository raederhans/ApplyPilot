"""Provider-neutral ATS form inspection and proposal helpers.

This module deliberately stops before browser or persistence side effects.  It
turns already-observed field metadata into a value-free intermediate form and
proposes semantic actions that a policy-aware orchestrator may later review.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from applypilot.apply.answer_policy import FieldRisk, field_risk

ATS_SCHEMA_VERSION = "1"
MAX_FORM_FIELDS = 200
MAX_OPTIONS_PER_FIELD = 100
MAX_PROMPT_FIELDS = 80
MAX_PROMPT_OPTIONS_PER_FIELD = 20
MAX_PROMPT_OPTION_LENGTH = 80
MAX_TEXT_LENGTH = 240

_ROUTINE_SEMANTIC_CONTROL_KINDS = frozenset(
    {
        "text",
        "textarea",
        "native_select",
        "custom_combobox",
        "radio",
        "checkbox",
        "switch",
        "date",
        "resume_file",
        "navigation",
    }
)

_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_VALUE_KEYS = frozenset(
    {
        "value",
        "current_value",
        "default_value",
        "text_content",
        "inner_text",
        "files",
        "selected_value",
    }
)


def _text(value: object, *, limit: int = MAX_TEXT_LENGTH) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()[:limit]


def _token_text(*values: object) -> str:
    return " ".join(_TOKEN_RE.sub(" ", _text(value).casefold()) for value in values).strip()


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").rstrip(".").casefold()
    except ValueError:
        return ""


def _field_key(raw: Mapping[str, object], index: int) -> str:
    for name in ("field_key", "id", "name", "selector"):
        candidate = _text(raw.get(name), limit=160)
        if candidate:
            return candidate
    return f"field-{index + 1}"


def _semantic(raw: Mapping[str, object]) -> str:
    text = _token_text(
        raw.get("label"),
        raw.get("name"),
        raw.get("id"),
        raw.get("autocomplete"),
        raw.get("placeholder"),
        raw.get("aria_label"),
        raw.get("type"),
    )
    patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("email", ("email", "e mail")),
        ("first_name", ("first name", "given name", "firstname")),
        ("last_name", ("last name", "family name", "surname", "lastname")),
        ("full_name", ("full name", "your name", "candidate name")),
        ("phone", ("phone", "mobile", "telephone", "tel")),
        ("resume", ("resume", "résumé", "cv", "curriculum vitae")),
        ("cover_letter", ("cover letter", "motivation letter")),
        ("linkedin", ("linkedin",)),
        ("website", ("portfolio", "website", "personal site", "github")),
        ("location", ("location", "city", "address")),
        ("work_authorization", ("authorized to work", "work authorization", "right to work")),
        ("sponsorship", ("sponsorship", "sponsor", "visa")),
        ("gender", ("gender", "sex")),
        ("race_ethnicity", ("race", "ethnicity", "ethnic")),
        ("veteran_status", ("veteran", "military status")),
        ("disability_status", ("disability", "disabled")),
        ("consent", ("consent", "privacy policy", "terms and conditions")),
        ("password", ("password", "passcode")),
        (
            "verification_code",
            ("one time password", "one time code", "verification code", "security code", "otp"),
        ),
        (
            "identity_number",
            (
                "identity number",
                "identification number",
                "unique identification",
                "passport number",
                "national id",
                "social security",
                "ssn",
                "nric",
                "fin number",
            ),
        ),
    )
    for semantic, needles in patterns:
        if any(needle in text for needle in needles):
            return semantic
    return "unknown"


def _control(raw: Mapping[str, object]) -> str:
    candidate = _text(raw.get("control") or raw.get("type") or raw.get("tag") or "text").casefold()
    aliases = {
        "input": "text",
        "file": "file",
        "select-one": "select",
        "combobox": "select",
        "textarea": "textarea",
        "checkbox": "checkbox",
        "radio": "radio",
    }
    return aliases.get(candidate, candidate or "text")


@dataclass(frozen=True, slots=True)
class FormFieldIR:
    """A value-free description of one observed form control."""

    field_key: str
    semantic: str
    control: str
    label: str = ""
    required: bool = False
    disabled: bool = False
    readonly: bool = False
    options: tuple[str, ...] = ()
    constraints: Mapping[str, object] = field(default_factory=dict)
    risk: FieldRisk = "low"

    def __post_init__(self) -> None:
        if not self.field_key.strip():
            raise ValueError("field_key is required")


@dataclass(frozen=True, slots=True)
class FormIR:
    """Provider-neutral, non-PII form structure."""

    site: str
    adapter: str
    fields: tuple[FormFieldIR, ...]
    schema_version: str = ATS_SCHEMA_VERSION
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class FillAction:
    """One proposal only; it does not contain a field value or execute a write."""

    field_key: str
    semantic: str
    action: str
    source_key: str | None = None
    reason: str = ""
    requires_review: bool = False


@dataclass(frozen=True, slots=True)
class FillPlan:
    adapter: str
    actions: tuple[FillAction, ...]
    schema_version: str = ATS_SCHEMA_VERSION


@runtime_checkable
class AtsAdapter(Protocol):
    name: str

    def matches(self, *, hostname: str, path: str) -> bool: ...

    def guidance(self) -> tuple[str, ...]: ...

    def normalize_semantic(self, semantic: str, raw: Mapping[str, object]) -> str: ...

    def risk_for(self, semantic: str, raw: Mapping[str, object]) -> FieldRisk | None: ...

    def semantic_control_kinds(self) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class GenericAtsAdapter:
    name: str = "generic"

    def matches(self, *, hostname: str, path: str) -> bool:
        del hostname, path
        return True

    def guidance(self) -> tuple[str, ...]:
        return (
            "Use field semantics and accessible labels; do not infer answers from ATS branding.",
            "Treat every action as a proposal until the application policy layer approves it.",
        )

    def normalize_semantic(self, semantic: str, raw: Mapping[str, object]) -> str:
        del raw
        return semantic

    def risk_for(self, semantic: str, raw: Mapping[str, object]) -> FieldRisk | None:
        del semantic, raw
        return None

    def semantic_control_kinds(self) -> frozenset[str]:
        """Generic pages have no production semantic-write admission."""

        return frozenset()


@dataclass(frozen=True, slots=True)
class GreenhouseAtsAdapter(GenericAtsAdapter):
    name: str = "greenhouse"

    def matches(self, *, hostname: str, path: str) -> bool:
        del path
        return hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"}

    def guidance(self) -> tuple[str, ...]:
        return (
            *GenericAtsAdapter.guidance(self),
            "Re-observe Greenhouse custom questions after resume parsing changes the form.",
        )


@dataclass(frozen=True, slots=True)
class LeverAtsAdapter(GenericAtsAdapter):
    name: str = "lever"

    def matches(self, *, hostname: str, path: str) -> bool:
        del path
        return hostname in {"jobs.lever.co", "jobs.eu.lever.co"}

    def guidance(self) -> tuple[str, ...]:
        return (
            *GenericAtsAdapter.guidance(self),
            "Treat Lever additional-information controls as ordinary custom questions.",
        )


@dataclass(frozen=True, slots=True)
class AshbyAtsAdapter(GenericAtsAdapter):
    name: str = "ashby"

    def matches(self, *, hostname: str, path: str) -> bool:
        del path
        return hostname == "jobs.ashbyhq.com"

    def guidance(self) -> tuple[str, ...]:
        return (
            *GenericAtsAdapter.guidance(self),
            "Re-observe Ashby conditional questions after each approved selection.",
        )


@dataclass(frozen=True, slots=True)
class SmartRecruitersAtsAdapter(GenericAtsAdapter):
    name: str = "smartrecruiters"

    def matches(self, *, hostname: str, path: str) -> bool:
        del path
        return hostname == "jobs.smartrecruiters.com"

    def guidance(self) -> tuple[str, ...]:
        return (
            *GenericAtsAdapter.guidance(self),
            "Distinguish the optional Easy Apply autocomplete upload from the required Resume upload.",
            "Upload the validated resume through the required Resume container and verify its file list before continuing.",
        )

    def semantic_control_kinds(self) -> frozenset[str]:
        return _ROUTINE_SEMANTIC_CONTROL_KINDS


@dataclass(frozen=True, slots=True)
class WorkdayAtsAdapter(GenericAtsAdapter):
    name: str = "workday"

    def matches(self, *, hostname: str, path: str) -> bool:
        del path
        return any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in ("myworkdayjobs.com", "myworkdaysite.com")
        )

    def guidance(self) -> tuple[str, ...]:
        return (
            *GenericAtsAdapter.guidance(self),
            "Treat each Workday page as an explicit state and verify structural progress after Next.",
            "After final Submit, do not switch runtimes and require visible receipt evidence.",
        )

    def semantic_control_kinds(self) -> frozenset[str]:
        return _ROUTINE_SEMANTIC_CONTROL_KINDS


class AtsAdapterRegistry:
    """Ordered dynamic registry with a provider-neutral fallback."""

    def __init__(self, adapters: Iterable[AtsAdapter] = (), *, fallback: AtsAdapter | None = None) -> None:
        self._items: dict[str, AtsAdapter] = {}
        self.fallback = fallback or GenericAtsAdapter()
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: AtsAdapter, *, replace: bool = False) -> None:
        name = _text(getattr(adapter, "name", ""), limit=80)
        if not name:
            raise ValueError("adapter name is required")
        if name == self.fallback.name:
            raise ValueError("register the fallback through the registry constructor")
        if name in self._items and not replace:
            raise ValueError(f"ATS adapter already registered: {name}")
        self._items[name] = adapter

    def get(self, name: str) -> AtsAdapter | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return [*self._items, self.fallback.name]

    def detect(self, url: str) -> AtsAdapter:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").rstrip(".").casefold()
        path = parsed.path or "/"
        for adapter in self._items.values():
            if adapter.matches(hostname=hostname, path=path):
                return adapter
        return self.fallback


def default_ats_registry() -> AtsAdapterRegistry:
    return AtsAdapterRegistry(
        (
            GreenhouseAtsAdapter(),
            LeverAtsAdapter(),
            AshbyAtsAdapter(),
            SmartRecruitersAtsAdapter(),
            WorkdayAtsAdapter(),
        )
    )


def detect_ats_site(url: str, *, registry: AtsAdapterRegistry | None = None) -> str:
    """Return an adapter name using parsed hostnames, never substring matching."""
    return (registry or default_ats_registry()).detect(url).name


def build_form_ir(
    url: str,
    fields: Iterable[Mapping[str, object]],
    *,
    registry: AtsAdapterRegistry | None = None,
) -> FormIR:
    """Build a bounded IR while intentionally discarding all observed values."""
    resolved = registry or default_ats_registry()
    adapter = resolved.detect(url)
    built: list[FormFieldIR] = []
    truncated = False
    for index, raw in enumerate(fields):
        if index >= MAX_FORM_FIELDS:
            truncated = True
            break
        # Rejecting is safer than silently retaining future value-shaped data.
        unexpected = _VALUE_KEYS.intersection(key.casefold() for key in raw)
        if unexpected:
            raw = {key: value for key, value in raw.items() if key.casefold() not in _VALUE_KEYS}
        semantic = adapter.normalize_semantic(_semantic(raw), raw)
        risk_hook = getattr(adapter, "risk_for", None)
        adapter_risk = risk_hook(semantic, raw) if callable(risk_hook) else None
        raw_options = raw.get("options")
        options = (
            tuple(_text(option, limit=120) for option in raw_options[:MAX_OPTIONS_PER_FIELD])
            if isinstance(raw_options, (list, tuple))
            else ()
        )
        constraints: dict[str, object] = {}
        for key in ("minlength", "maxlength", "min", "max", "pattern", "multiple"):
            value = raw.get(key)
            if isinstance(value, (str, int, float, bool)):
                constraints[key] = _text(value) if isinstance(value, str) else value
        built.append(
            FormFieldIR(
                field_key=_field_key(raw, index),
                semantic=semantic,
                control=_control(raw),
                label=_text(raw.get("label") or raw.get("aria_label")),
                required=bool(raw.get("required", False)),
                disabled=bool(raw.get("disabled", False)),
                readonly=bool(raw.get("readonly", False)),
                options=options,
                constraints=constraints,
                risk=field_risk(semantic, adapter_risk=adapter_risk),
            )
        )
    return FormIR(site=_hostname(url), adapter=adapter.name, fields=tuple(built), truncated=truncated)


_SENSITIVE_SEMANTICS = frozenset(
    {
        "gender",
        "race_ethnicity",
        "veteran_status",
        "disability_status",
        "consent",
        "password",
        "verification_code",
        "identity_number",
    }
)


def propose_fill_plan(form: FormIR, available_facts: Iterable[str]) -> FillPlan:
    """Propose value-free semantic actions from confirmed fact *names*."""
    facts = {_text(name, limit=120) for name in available_facts if _text(name, limit=120)}
    actions: list[FillAction] = []
    for field_ir in form.fields:
        if field_ir.disabled or field_ir.readonly:
            actions.append(
                FillAction(field_ir.field_key, field_ir.semantic, "skip", reason="field is not writable")
            )
            continue
        if field_ir.semantic in _SENSITIVE_SEMANTICS:
            actions.append(
                FillAction(
                    field_ir.field_key,
                    field_ir.semantic,
                    "review",
                    reason="sensitive or consent answer requires explicit policy review",
                    requires_review=True,
                )
            )
            continue
        source_key = field_ir.semantic if field_ir.semantic in facts else None
        if source_key is None:
            actions.append(
                FillAction(
                    field_ir.field_key,
                    field_ir.semantic,
                    "request_fact" if field_ir.required else "skip",
                    reason="no confirmed semantic fact is available",
                    requires_review=field_ir.required,
                )
            )
            continue
        action = "upload" if field_ir.control == "file" or field_ir.semantic in {"resume", "cover_letter"} else "fill"
        if field_ir.control in {"select", "radio", "checkbox"}:
            action = "select"
        actions.append(FillAction(field_ir.field_key, field_ir.semantic, action, source_key=source_key))
    return FillPlan(adapter=form.adapter, actions=tuple(actions))


def adapter_prompt_guidance(url: str, *, registry: AtsAdapterRegistry | None = None) -> tuple[str, ...]:
    return (registry or default_ats_registry()).detect(url).guidance()


def adapter_prompt_context(form: FormIR, plan: FillPlan | None = None) -> dict[str, Any]:
    """Return a bounded JSON-safe context without field values or PII answers."""
    visible_fields = form.fields[:MAX_PROMPT_FIELDS]
    context: dict[str, Any] = {
        "schema_version": form.schema_version,
        "adapter": form.adapter,
        "site": form.site,
        "field_count": len(form.fields),
        "truncated": form.truncated or len(form.fields) > MAX_PROMPT_FIELDS,
        "fields": [
            {
                "field_key": item.field_key,
                "semantic": item.semantic,
                "control": item.control,
                "required": item.required,
                "writable": not (item.disabled or item.readonly),
                "option_count": len(item.options),
                "options": [
                    _text(option, limit=MAX_PROMPT_OPTION_LENGTH)
                    for option in item.options[:MAX_PROMPT_OPTIONS_PER_FIELD]
                ],
                "options_truncated": len(item.options) > MAX_PROMPT_OPTIONS_PER_FIELD,
            }
            for item in visible_fields
        ],
    }
    if plan is not None:
        allowed = {item.field_key for item in visible_fields}
        context["actions"] = [
            {
                "field_key": item.field_key,
                "semantic": item.semantic,
                "action": item.action,
                "source_key": item.source_key,
                "requires_review": item.requires_review,
            }
            for item in plan.actions
            if item.field_key in allowed
        ]
    return context
