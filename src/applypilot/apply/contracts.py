"""Provider-neutral contracts for agent-assisted application work.

These contracts describe proposals and observations, not the authoritative job
state.  Phases, side effects, and concurrency modes are deliberately strings so
new runtimes and workflows can extend them without changing the core package.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, cast, runtime_checkable

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

MAX_AGENT_REPORT_BYTES = 131_072
MAX_AGENT_PROPOSALS = 50
MAX_PROPOSAL_DEPENDENCIES = 50
MAX_TASK_DEPENDENCIES = 100

FAILURE_OBSERVATION_CODES = frozenset(
    {
        "agent_runtime_timeout",
        "already_applied",
        "answer_policy_unresolved",
        "assessment_required",
        "browser_interaction_unavailable",
        "browser_mcp_unavailable",
        "browser_runtime_blocked",
        "captcha_required",
        "cover_letter_missing",
        "credential_relay_missing",
        "duplicate",
        "expired",
        "explicit_do_not_apply",
        "form_resolution_incomplete",
        "mailbox_route_unavailable",
        "not_a_job_application",
        "page_or_progress_failure",
        "post_submit_observer_unavailable",
        "provider_submission_error",
        "required_document_missing",
        "resume_upload_failed",
        "security_challenge_required",
        "sensitive_identity_material_required",
        "submission_uncertain",
        "truth_or_security_boundary",
        "unknown",
        "unsupported_legal_declaration",
        "visual_control_unavailable",
    }
)
FAILURE_OBSERVATION_SOURCES = frozenset(
    {
        "agent",
        "browser_observer",
        "policy",
        "provider_adapter",
        "receipt_observer",
        "runtime",
        "submission_observer",
    }
)
FAILURE_OBSERVATION_PROVIDERS = frozenset(
    {
        "direct_email",
        "generic",
        "greenhouse",
        "lever",
        "linkedin",
        "smartrecruiters",
        "unknown",
        "workday",
    }
)
FAILURE_OBSERVATION_PHASES = frozenset(
    {"observe", "prepare", "receipt", "submit", "verify"}
)
FAILURE_MISSING_CAPABILITIES = frozenset(
    {
        "agent_runtime_budget",
        "answer_resolution",
        "assessment_owner",
        "authorized_human_security_handoff",
        "browser_file_upload_or_site_adapter",
        "compatible_browser_runtime",
        "credential_relay",
        "human_verification_relay",
        "mailbox_route",
        "playwright_mcp",
        "post_submit_observer_or_receipt_reconciliation",
        "provider_submission_diagnostics_or_adapter",
        "site_specific_browser_interaction_or_app_handoff",
        "site_state_or_adapter_progress",
        "visual_control",
    }
)
FAILURE_MISSING_MATERIALS = frozenset(
    {"required_application_document", "validated_cover_letter"}
)

_FAILURE_SEMANTIC = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_FAILURE_REFERENCE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9._/-]{1,180}$")

_FORBIDDEN_PERSISTED_KEYS = {
    "api_key",
    "authorization",
    "browser_handle",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "fin",
    "fin_number",
    "otp",
    "body",
    "email_body",
    "mailbox_content",
    "message_body",
    "page_handle",
    "password",
    "passport_number",
    "national_id_number",
    "nric",
    "nric_number",
    "identity_number",
    "secret",
    "session_cookie",
    "security_code",
    "token",
    "verification_code",
}

_PROTECTED_IDENTIFIER_VALUE = re.compile(
    r"(?<![A-Z0-9])[A-Z][0-9]{7}[A-Z](?![A-Z0-9])",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def application_actor_id(attempt_id: str) -> str:
    """Return the stable durable actor identity for one application attempt."""
    _required(attempt_id, "attempt_id")
    return f"application:{attempt_id}"


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _strings(values: tuple[str, ...], name: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{name} must contain non-empty strings")


def ensure_json_safe(value: object, *, path: str = "$") -> JsonValue:
    """Validate and copy a value into the strict JSON value domain."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string mapping key")
            copied[key] = ensure_json_safe(item, path=f"{path}.{key}")
        return copied
    if isinstance(value, (list, tuple)):
        return [ensure_json_safe(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} contains a non-JSON value: {type(value).__name__}")


def ensure_persistable(value: object, *, path: str = "$") -> JsonValue:
    """Validate durable control data, rejecting raw secrets and live handles.

    References such as ``evidence_ref`` or ``secret_ref`` are allowed.  The
    referenced material remains in its owning secure store.
    """
    copied = ensure_json_safe(value, path=path)

    def visit(item: JsonValue, current: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key.casefold() in _FORBIDDEN_PERSISTED_KEYS:
                    raise ValueError(f"{current}.{key} may not be persisted; store a reference instead")
                visit(child, f"{current}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{current}[{index}]")
        elif isinstance(item, str) and _PROTECTED_IDENTIFIER_VALUE.search(item):
            raise ValueError(
                f"{current} contains a protected identity number; store a reference instead"
            )

    visit(copied, path)
    return copied


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Runtime-independent tool declaration with extensible policy metadata."""

    name: str
    description: str
    input_schema: Mapping[str, object] = field(default_factory=dict)
    output_schema: Mapping[str, object] = field(default_factory=dict)
    phases: tuple[str, ...] = ()
    side_effect: str = "read"
    effect_class: str | None = None
    idempotency: str = "unknown"
    authority: str = "unknown"
    providers: tuple[str, ...] = ()
    sensitivity: str = "unknown"
    timeout_seconds: float | None = None
    retry_policy: Mapping[str, object] = field(default_factory=dict)
    rate_limit: Mapping[str, object] = field(default_factory=dict)
    postcondition: Mapping[str, object] = field(default_factory=dict)
    namespace: str = "core"
    defer_loading: bool = False
    concurrency_mode: str = "adaptive"
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required(self.name, "name")
        _required(self.description, "description")
        _required(self.side_effect, "side_effect")
        normalized_effect = self.effect_class or self.side_effect
        _required(normalized_effect, "effect_class")
        if self.effect_class is not None and self.side_effect not in {"read", normalized_effect}:
            raise ValueError("side_effect and effect_class must not conflict")
        object.__setattr__(self, "effect_class", normalized_effect)
        if self.effect_class != self.side_effect and self.side_effect == "read":
            # New declarations use effect_class while legacy callers continue
            # to observe the same classification through side_effect.
            object.__setattr__(self, "side_effect", normalized_effect)
        _required(self.idempotency, "idempotency")
        _required(self.authority, "authority")
        _required(self.sensitivity, "sensitivity")
        _required(self.namespace, "namespace")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when provided")
        _required(self.concurrency_mode, "concurrency_mode")
        ensure_json_safe(self.input_schema, path="$.input_schema")
        ensure_json_safe(self.output_schema, path="$.output_schema")
        ensure_json_safe(self.retry_policy, path="$.retry_policy")
        ensure_json_safe(self.rate_limit, path="$.rate_limit")
        ensure_json_safe(self.postcondition, path="$.postcondition")
        ensure_json_safe(self.metadata, path="$.metadata")
        _strings(self.phases, "phases")
        _strings(self.providers, "providers")
        _strings(self.tags, "tags")


@dataclass(frozen=True, slots=True)
class AgentProposal:
    """One proposal that an orchestrator may schedule serially or in parallel."""

    kind: str
    summary: str
    payload: Mapping[str, object] = field(default_factory=dict)
    proposal_id: str = field(default_factory=lambda: f"proposal-{uuid.uuid4()}")
    depends_on: tuple[str, ...] = ()
    concurrency_mode: str = "adaptive"
    concurrency_key: str | None = None
    priority: int = 0

    def __post_init__(self) -> None:
        _required(self.proposal_id, "proposal_id")
        _required(self.kind, "kind")
        _required(self.summary, "summary")
        _required(self.concurrency_mode, "concurrency_mode")
        ensure_persistable(self.payload, path="$.payload")
        _strings(self.depends_on, "depends_on")
        if len(self.depends_on) > MAX_PROPOSAL_DEPENDENCIES:
            raise ValueError(
                f"a proposal may have at most {MAX_PROPOSAL_DEPENDENCIES} dependencies"
            )
        if self.concurrency_key is not None:
            _required(self.concurrency_key, "concurrency_key")


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    """One capacity claim used by the local task coordinator.

    Keys are deliberately open strings (for example ``page:attempt-1`` or
    ``llm:provider/score``), so the contract is not tied to a browser, model,
    or orchestration framework.
    """

    key: str
    units: int = 1

    def __post_init__(self) -> None:
        _required(self.key, "key")
        if isinstance(self.units, bool) or not isinstance(self.units, int) or self.units < 1:
            raise ValueError("resource claim units must be a positive integer")


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Framework-neutral, durable description of one bounded workflow task."""

    task_id: str
    kind: str
    objective: str
    inputs: Mapping[str, object] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    required_results: tuple[str, ...] = ()
    effect_class: str = "read"
    authority_scope: tuple[str, ...] = ()
    resource_claims: tuple[ResourceClaim, ...] = ()
    retry_budget: int = 0
    retry_categories: tuple[str, ...] = ()
    deadline_at: datetime | None = None
    cancellation_mode: str = "cooperative"
    partial_allowed: bool = False
    conflict_keys: tuple[str, ...] = ()
    idempotency_key: str | None = None
    priority: int = 0
    counts_toward_target: bool = False
    resume_cursor: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("task_id", "kind", "objective", "effect_class"):
            _required(getattr(self, name), name)
        ensure_persistable(self.inputs, path="$.inputs")
        ensure_persistable(self.resume_cursor, path="$.resume_cursor")
        _strings(self.authority_scope, "authority_scope")
        _strings(self.depends_on, "depends_on")
        _strings(self.required_results, "required_results")
        _strings(self.retry_categories, "retry_categories")
        _strings(self.conflict_keys, "conflict_keys")
        _required(self.cancellation_mode, "cancellation_mode")
        if not isinstance(self.partial_allowed, bool):
            raise TypeError("partial_allowed must be a boolean")
        if len(self.depends_on) > MAX_TASK_DEPENDENCIES:
            raise ValueError(f"a task may have at most {MAX_TASK_DEPENDENCIES} dependencies")
        if not set(self.required_results) <= set(self.depends_on):
            raise ValueError("required_results must be a subset of depends_on")
        if any(not isinstance(claim, ResourceClaim) for claim in self.resource_claims):
            raise TypeError("resource_claims must contain ResourceClaim values")
        if isinstance(self.retry_budget, bool) or not isinstance(self.retry_budget, int):
            raise TypeError("retry_budget must be an integer")
        if self.retry_budget < 0:
            raise ValueError("retry_budget must be non-negative")
        if self.deadline_at is not None:
            _aware(self.deadline_at, "deadline_at")
        if self.idempotency_key is not None:
            _required(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Durable task output consumed by a coordinator reducer."""

    task_id: str
    status: str
    output: Mapping[str, object] = field(default_factory=dict)
    authority_scope: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    metrics: Mapping[str, object] = field(default_factory=dict)
    failure_category: str | None = None
    retryable: bool = False
    partial: bool = False
    conflict_keys: tuple[str, ...] = ()
    checkpoint: Mapping[str, object] = field(default_factory=dict)
    resume_cursor: Mapping[str, object] = field(default_factory=dict)
    completed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _required(self.task_id, "task_id")
        _required(self.status, "status")
        ensure_persistable(self.output, path="$.output")
        ensure_persistable(self.metrics, path="$.metrics")
        ensure_persistable(self.resume_cursor, path="$.resume_cursor")
        ensure_persistable(self.checkpoint, path="$.checkpoint")
        _strings(self.authority_scope, "authority_scope")
        _strings(self.evidence_refs, "evidence_refs")
        _strings(self.conflict_keys, "conflict_keys")
        if not isinstance(self.partial, bool):
            raise TypeError("partial must be a boolean")
        if self.failure_category is not None:
            _required(self.failure_category, "failure_category")
        _aware(self.completed_at, "completed_at")

    @property
    def succeeded(self) -> bool:
        return not self.partial and self.status.casefold() in {"completed", "succeeded", "ok"}


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    run_id: str
    attempt_id: str
    agent_role: str
    phase: str
    objective: str
    context: Mapping[str, object] = field(default_factory=dict)
    available_tools: tuple[str, ...] = ()
    actor_id: str = ""
    turn_id: str = ""
    parent_run_id: str | None = None
    proposal_group_id: str | None = None
    concurrency_mode: str = "adaptive"
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        actor_id = self.actor_id or application_actor_id(self.attempt_id)
        turn_id = self.turn_id or self.run_id
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "turn_id", turn_id)
        for name in ("run_id", "attempt_id", "agent_role", "phase", "objective", "concurrency_mode"):
            _required(getattr(self, name), name)
        _required(self.actor_id, "actor_id")
        _required(self.turn_id, "turn_id")
        if self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("actor_id must be the canonical identity for attempt_id")
        if self.turn_id != self.run_id:
            raise ValueError("run_id must remain the compatibility alias for turn_id")
        _aware(self.created_at, "created_at")
        ensure_persistable(self.context, path="$.context")
        _strings(self.available_tools, "available_tools")


@dataclass(frozen=True, slots=True)
class FailureObservation:
    """Bounded facts about one failed application turn, without recovery policy."""

    code: str
    source: str
    provider: str
    phase: str
    submit_started: bool
    field_semantic: str | None = None
    page_epoch: int | None = None
    evidence_refs: tuple[str, ...] = ()
    detail_ref: str | None = None
    missing_capability: str | None = None
    missing_material: str | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise ValueError("FailureObservation schema_version must be 1")
        for name, allowed in (
            ("code", FAILURE_OBSERVATION_CODES),
            ("source", FAILURE_OBSERVATION_SOURCES),
            ("provider", FAILURE_OBSERVATION_PROVIDERS),
            ("phase", FAILURE_OBSERVATION_PHASES),
        ):
            value = getattr(self, name)
            if value not in allowed:
                raise ValueError(f"unsupported FailureObservation {name}: {value}")
        if not isinstance(self.submit_started, bool):
            raise TypeError("FailureObservation submit_started must be a boolean")
        if self.code in {
            "post_submit_observer_unavailable",
            "provider_submission_error",
            "submission_uncertain",
        } and not self.submit_started:
            raise ValueError(
                f"FailureObservation {self.code} requires submit_started=true"
            )
        if self.field_semantic is not None and not _FAILURE_SEMANTIC.fullmatch(
            self.field_semantic
        ):
            raise ValueError("FailureObservation field_semantic is not a bounded semantic id")
        if self.page_epoch is not None and (
            isinstance(self.page_epoch, bool)
            or not isinstance(self.page_epoch, int)
            or self.page_epoch < 0
        ):
            raise ValueError("FailureObservation page_epoch must be a non-negative integer")
        if len(self.evidence_refs) > 8:
            raise ValueError("FailureObservation may contain at most 8 evidence refs")
        _strings(self.evidence_refs, "FailureObservation evidence_refs")
        for reference in (*self.evidence_refs, self.detail_ref):
            if reference is not None and not _FAILURE_REFERENCE.fullmatch(reference):
                raise ValueError("FailureObservation references must be bounded opaque refs")
        if (
            self.missing_capability is not None
            and self.missing_capability not in FAILURE_MISSING_CAPABILITIES
        ):
            raise ValueError("unsupported FailureObservation missing_capability")
        if (
            self.missing_material is not None
            and self.missing_material not in FAILURE_MISSING_MATERIALS
        ):
            raise ValueError("unsupported FailureObservation missing_material")


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    run_id: str
    status: str
    summary: str
    proposals: tuple[AgentProposal, ...] = ()
    observations: Mapping[str, object] = field(default_factory=dict)
    failure: FailureObservation | None = None
    requested_human_input: str | None = None
    completed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("run_id", "status", "summary"):
            _required(getattr(self, name), name)
        _aware(self.completed_at, "completed_at")
        ensure_persistable(self.observations, path="$.observations")
        if any(not isinstance(proposal, AgentProposal) for proposal in self.proposals):
            raise TypeError("proposals must contain AgentProposal values")
        if len(self.proposals) > MAX_AGENT_PROPOSALS:
            raise ValueError(f"a result may contain at most {MAX_AGENT_PROPOSALS} proposals")
        if self.failure is not None:
            if not isinstance(self.failure, FailureObservation):
                raise TypeError("failure must be a FailureObservation")
            normalized_status = self.status.strip().casefold()
            if self.failure.submit_started:
                if normalized_status != "submission_uncertain":
                    raise ValueError(
                        "a submit-started failure requires submission_uncertain status"
                    )
            elif normalized_status not in {
                "failed",
                f"failed:{self.failure.code}",
                "captcha" if self.failure.code == "captcha_required" else "failed",
                "expired" if self.failure.code == "expired" else "failed",
            }:
                raise ValueError("AgentTurnResult status conflicts with FailureObservation")
        if self.requested_human_input is not None:
            _required(self.requested_human_input, "requested_human_input")


RecoveryActionName = Literal[
    "retry_same_application",
    "retry_new_session",
    "requires_capability",
    "reconcile_receipt",
    "park",
    "human_only",
    "no_retry",
]
HumanInterruptionType = Literal[
    "captcha",
    "security_challenge",
    "assessment",
    "sensitive_identity_or_financial_material",
    "unsupported_legal_declaration",
    "human_boundary",
]
DecisionDisposition = Literal["advance", "recover", "checkpoint", "complete"]
RecoveryCommandName = Literal[
    "retry_same_application",
    "retry_new_session",
    "enqueue_receipt_reconciliation",
    "park_exception",
    "enqueue_human_handoff",
    "record_no_retry",
]
RecoveryEffectScope = Literal[
    "same_application",
    "new_session",
    "receipt_reconciliation",
    "exception_queue",
    "human_handoff",
    "none",
]
RecoveryExecutionStage = Literal["started", "executed", "verified", "failed"]
RecoveryTerminalStatus = Literal[
    "completed",
    "failed",
    "terminated",
    "timed_out",
    "canceled",
]
ExceptionQueueKind = Literal[
    "parked",
    "capability",
    "receipt_reconciliation",
    "human_only",
    "recovery_execution",
]

_RECOVERY_ACTION_NAMES = frozenset(
    {
        "retry_same_application",
        "retry_new_session",
        "requires_capability",
        "reconcile_receipt",
        "park",
        "human_only",
        "no_retry",
    }
)
_HUMAN_INTERRUPTION_TYPES = frozenset(
    {
        "captcha",
        "security_challenge",
        "assessment",
        "sensitive_identity_or_financial_material",
        "unsupported_legal_declaration",
        "human_boundary",
    }
)
_DECISION_DISPOSITIONS = frozenset({"advance", "recover", "checkpoint", "complete"})
_RECOVERY_COMMAND_EFFECTS: dict[str, str] = {
    "retry_same_application": "same_application",
    "retry_new_session": "new_session",
    "enqueue_receipt_reconciliation": "receipt_reconciliation",
    "park_exception": "exception_queue",
    "enqueue_human_handoff": "human_handoff",
    "record_no_retry": "none",
}
_RECOVERY_COMMAND_ACTIONS: dict[str, frozenset[str]] = {
    "retry_same_application": frozenset({"retry_same_application"}),
    "retry_new_session": frozenset({"retry_new_session"}),
    "enqueue_receipt_reconciliation": frozenset({"reconcile_receipt"}),
    "park_exception": frozenset({"requires_capability", "park"}),
    "enqueue_human_handoff": frozenset({"human_only"}),
    "record_no_retry": frozenset({"no_retry"}),
}
_RECOVERY_EXECUTION_STAGES = frozenset({"started", "executed", "verified", "failed"})
_RECOVERY_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "terminated", "timed_out", "canceled"}
)
_EXCEPTION_QUEUE_KINDS = frozenset(
    {
        "parked",
        "capability",
        "receipt_reconciliation",
        "human_only",
        "recovery_execution",
    }
)


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """Typed, non-authoritative recovery proposal for an application turn."""

    action: RecoveryActionName
    failure_category: str
    next_action: str
    retry_budget_remaining: int = 0
    missing_capability: str | None = None
    missing_material: str | None = None

    def __post_init__(self) -> None:
        if self.action not in _RECOVERY_ACTION_NAMES:
            raise ValueError(f"unsupported recovery action: {self.action}")
        _required(self.failure_category, "failure_category")
        _required(self.next_action, "next_action")
        if (
            isinstance(self.retry_budget_remaining, bool)
            or not isinstance(self.retry_budget_remaining, int)
            or self.retry_budget_remaining < 0
        ):
            raise ValueError("retry_budget_remaining must be a non-negative integer")
        if self.missing_capability is not None:
            _required(self.missing_capability, "missing_capability")
        if self.missing_material is not None:
            _required(self.missing_material, "missing_material")


@dataclass(frozen=True, slots=True)
class HumanInterruption:
    """A typed HUMAN_ONLY boundary; raw answers or secrets never belong here."""

    interruption_type: HumanInterruptionType
    reason: str
    next_action: str

    def __post_init__(self) -> None:
        if self.interruption_type not in _HUMAN_INTERRUPTION_TYPES:
            raise ValueError(f"unsupported human interruption: {self.interruption_type}")
        _required(self.reason, "reason")
        _required(self.next_action, "next_action")


@dataclass(frozen=True, slots=True)
class RecoveryCommand:
    """Policy-admitted recovery effect with no Submit or ledger authority."""

    command_id: str
    run_id: str
    attempt_id: str
    actor_id: str
    turn_id: str
    command: RecoveryCommandName
    effect_scope: RecoveryEffectScope
    recovery_action: RecoveryActionName
    failure_category: str
    next_action: str
    retry_budget_remaining: int = 0
    policy_reason: str = "recovery_policy_v1_admitted"
    payload: Mapping[str, object] = field(default_factory=dict)
    submit_authority: bool = False
    page_write_authority: bool = False
    ledger_write_authority: bool = False
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in (
            "command_id",
            "run_id",
            "attempt_id",
            "actor_id",
            "turn_id",
            "failure_category",
            "next_action",
            "policy_reason",
        ):
            _required(getattr(self, name), name)
        if self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("actor_id must be the canonical identity for attempt_id")
        if self.turn_id != self.run_id:
            raise ValueError("run_id must remain the compatibility alias for turn_id")
        expected_scope = _RECOVERY_COMMAND_EFFECTS.get(self.command)
        if expected_scope is None:
            raise ValueError(f"unsupported recovery command: {self.command}")
        if self.effect_scope != expected_scope:
            raise ValueError("recovery command effect_scope does not match command")
        if self.recovery_action not in _RECOVERY_ACTION_NAMES:
            raise ValueError(f"unsupported recovery action: {self.recovery_action}")
        if self.recovery_action not in _RECOVERY_COMMAND_ACTIONS[self.command]:
            raise ValueError("recovery command does not match the admitted recovery action")
        if (
            isinstance(self.retry_budget_remaining, bool)
            or not isinstance(self.retry_budget_remaining, int)
            or self.retry_budget_remaining < 0
        ):
            raise ValueError("retry_budget_remaining must be a non-negative integer")
        if self.submit_authority or self.page_write_authority or self.ledger_write_authority:
            raise ValueError("recovery commands cannot claim Submit, page-write, or ledger authority")
        if self.schema_version != "1":
            raise ValueError("unsupported RecoveryCommand schema_version")
        ensure_persistable(self.payload, path="$.payload")


@dataclass(frozen=True, slots=True)
class RecoveryExecutionResult:
    """One immutable lifecycle result for an allowlisted recovery command."""

    result_id: str
    command_id: str
    run_id: str
    attempt_id: str
    actor_id: str
    turn_id: str
    stage: RecoveryExecutionStage
    outcome: str
    terminal_status: RecoveryTerminalStatus | None = None
    details: Mapping[str, object] = field(default_factory=dict)
    schema_version: str = "1"
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in (
            "result_id",
            "command_id",
            "run_id",
            "attempt_id",
            "actor_id",
            "turn_id",
            "outcome",
        ):
            _required(getattr(self, name), name)
        if self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("actor_id must be the canonical identity for attempt_id")
        if self.turn_id != self.run_id:
            raise ValueError("run_id must remain the compatibility alias for turn_id")
        if self.stage not in _RECOVERY_EXECUTION_STAGES:
            raise ValueError(f"unsupported recovery execution stage: {self.stage}")
        if self.stage in {"started", "executed"} and self.terminal_status is not None:
            raise ValueError("non-terminal recovery stages cannot have terminal_status")
        if self.stage == "verified" and self.terminal_status != "completed":
            raise ValueError("verified recovery result must be completed")
        if self.stage == "failed" and self.terminal_status not in (
            _RECOVERY_TERMINAL_STATUSES - {"completed"}
        ):
            raise ValueError("failed recovery result requires a failure terminal_status")
        if self.schema_version != "1":
            raise ValueError("unsupported RecoveryExecutionResult schema_version")
        _aware(self.occurred_at, "occurred_at")
        ensure_persistable(self.details, path="$.details")


@dataclass(frozen=True, slots=True)
class ApplicationException:
    """One durable parked application item that never grants execution authority."""

    exception_id: str
    command_id: str
    run_id: str
    attempt_id: str
    actor_id: str
    turn_id: str
    queue_kind: ExceptionQueueKind
    failure_category: str
    next_action: str
    status: str = "open"
    context: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "exception_id",
            "command_id",
            "run_id",
            "attempt_id",
            "actor_id",
            "turn_id",
            "failure_category",
            "next_action",
            "status",
        ):
            _required(getattr(self, name), name)
        if self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("actor_id must be the canonical identity for attempt_id")
        if self.turn_id != self.run_id:
            raise ValueError("run_id must remain the compatibility alias for turn_id")
        if self.queue_kind not in _EXCEPTION_QUEUE_KINDS:
            raise ValueError(f"unsupported exception queue kind: {self.queue_kind}")
        if self.status not in {"open", "resolved", "expired"}:
            raise ValueError(f"unsupported exception status: {self.status}")
        _aware(self.created_at, "created_at")
        if self.resolved_at is not None:
            _aware(self.resolved_at, "resolved_at")
        ensure_persistable(self.context, path="$.context")


@dataclass(frozen=True, slots=True)
class DecisionEnvelope:
    """Structured shadow decision consumed by existing control-plane code."""

    run_id: str
    attempt_id: str
    phase: str
    disposition: DecisionDisposition
    next_phase: str
    recovery_action: RecoveryAction | None = None
    human_interruption: HumanInterruption | None = None
    actor_id: str = ""
    turn_id: str = ""
    upcast_from_schema_version: str | None = None
    fresh_turn_resume_authorized: bool = False
    shadow_only: bool = True
    schema_version: str = "2"

    def __post_init__(self) -> None:
        actor_id = self.actor_id or application_actor_id(self.attempt_id)
        turn_id = self.turn_id or self.run_id
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "turn_id", turn_id)
        for name in ("run_id", "attempt_id", "phase", "next_phase"):
            _required(getattr(self, name), name)
        _required(self.actor_id, "actor_id")
        _required(self.turn_id, "turn_id")
        if self.turn_id != self.run_id:
            raise ValueError("run_id must remain the compatibility alias for turn_id")
        if self.disposition not in _DECISION_DISPOSITIONS:
            raise ValueError(f"unsupported decision disposition: {self.disposition}")
        if self.shadow_only is not True:
            raise ValueError("DecisionEnvelope must remain shadow-only")
        if self.schema_version != "2":
            raise ValueError("unsupported DecisionEnvelope schema_version")
        if self.upcast_from_schema_version not in {None, "1"}:
            raise ValueError("unsupported DecisionEnvelope upcast source")
        if self.upcast_from_schema_version == "1":
            if not self.actor_id.startswith("legacy:"):
                raise ValueError("legacy DecisionEnvelope must use an isolated actor identity")
        elif self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("actor_id must be the canonical identity for attempt_id")
        if self.fresh_turn_resume_authorized is not False:
            raise ValueError("fresh-turn resume is not authorized by DecisionEnvelope v2")
        if self.disposition == "recover" and self.recovery_action is None:
            raise ValueError("recover disposition requires a RecoveryAction")
        if self.recovery_action is not None:
            expected_control = (
                ("recover", "recover")
                if self.recovery_action.action
                in {"retry_same_application", "retry_new_session"}
                else ("complete", "complete")
                if self.recovery_action.action == "no_retry"
                else ("checkpoint", "checkpoint")
            )
            if (self.disposition, self.next_phase) != expected_control:
                raise ValueError("DecisionEnvelope control flow does not match RecoveryAction")
        elif self.disposition in {"checkpoint", "complete"} and self.next_phase != self.disposition:
            raise ValueError("DecisionEnvelope next_phase does not match disposition")
        if self.human_interruption is not None and (
            self.recovery_action is None or self.recovery_action.action != "human_only"
        ):
            raise ValueError("HumanInterruption requires a human_only RecoveryAction")
        if (
            self.recovery_action is not None
            and self.recovery_action.action == "human_only"
            and self.human_interruption is None
        ):
            raise ValueError("human_only RecoveryAction requires a HumanInterruption")


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    event_id: str
    attempt_id: str
    run_id: str
    phase: str
    actor: str
    event_type: str
    payload: Mapping[str, object] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    idempotency_key: str | None = None
    actor_id: str | None = None
    turn_id: str | None = None
    schema_version: str = "1"
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("event_id", "attempt_id", "run_id", "phase", "actor", "event_type"):
            _required(getattr(self, name), name)
        if self.schema_version not in {"1", "2"}:
            raise ValueError("unsupported ApplicationEvent schema_version")
        if self.schema_version == "2":
            if self.actor_id is None or self.turn_id is None:
                raise ValueError("ApplicationEvent v2 requires actor_id and turn_id")
            _required(self.actor_id, "actor_id")
            _required(self.turn_id, "turn_id")
            if self.actor_id != application_actor_id(self.attempt_id):
                raise ValueError("actor_id must be the canonical identity for attempt_id")
            if self.turn_id != self.run_id:
                raise ValueError("run_id must remain the compatibility alias for turn_id")
        if self.idempotency_key is not None:
            _required(self.idempotency_key, "idempotency_key")
        _aware(self.occurred_at, "occurred_at")
        ensure_persistable(self.payload, path="$.payload")
        _strings(self.evidence_refs, "evidence_refs")


@dataclass(frozen=True, slots=True)
class AgentCheckpoint:
    checkpoint_id: str
    run_id: str
    attempt_id: str
    phase: str
    sequence: int
    state: Mapping[str, object]
    actor_id: str | None = None
    turn_id: str | None = None
    idempotency_key: str | None = None
    expected_sequence: int | None = None
    fresh_turn_resume_authorized: bool = False
    schema_version: str = "1"
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("checkpoint_id", "run_id", "attempt_id", "phase"):
            _required(getattr(self, name), name)
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        if self.schema_version not in {"1", "2"}:
            raise ValueError("unsupported AgentCheckpoint schema_version")
        if self.fresh_turn_resume_authorized is not False:
            raise ValueError("fresh-turn resume is not authorized by this checkpoint contract")
        if self.schema_version == "2":
            if self.actor_id is None or self.turn_id is None or self.idempotency_key is None:
                raise ValueError("AgentCheckpoint v2 requires actor, turn, and idempotency identity")
            _required(self.actor_id, "actor_id")
            _required(self.turn_id, "turn_id")
            _required(self.idempotency_key, "idempotency_key")
            if self.actor_id != application_actor_id(self.attempt_id):
                raise ValueError("actor_id must be the canonical identity for attempt_id")
            if self.turn_id != self.run_id:
                raise ValueError("run_id must remain the compatibility alias for turn_id")
            if (
                isinstance(self.expected_sequence, bool)
                or not isinstance(self.expected_sequence, int)
                or self.expected_sequence < 0
            ):
                raise ValueError("AgentCheckpoint v2 requires a non-negative expected_sequence")
            if self.sequence != self.expected_sequence + 1:
                raise ValueError("checkpoint sequence must equal expected_sequence + 1")
        _aware(self.created_at, "created_at")
        ensure_persistable(self.state, path="$.state")


@dataclass(frozen=True, slots=True)
class HumanRequest:
    request_id: str
    run_id: str
    attempt_id: str
    request_type: str
    prompt: str
    context: Mapping[str, object] = field(default_factory=dict)
    status: str = "open"
    created_at: datetime = field(default_factory=utc_now)
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("request_id", "run_id", "attempt_id", "request_type", "prompt", "status"):
            _required(getattr(self, name), name)
        if self.status not in {"open", "resolved", "consumed", "expired"}:
            raise ValueError(f"unsupported human request status: {self.status}")
        _aware(self.created_at, "created_at")
        if self.resolved_at is not None:
            _aware(self.resolved_at, "resolved_at")
        ensure_persistable(self.context, path="$.context")


def contract_json(value: object) -> dict[str, JsonValue]:
    """Return a JSON-safe dictionary for a contract dataclass."""
    raw = asdict(value)
    # Keep canonical bytes stable for TaskSpec/TaskResult instances created
    # before the optional P2 control fields existed. Durable journal identity
    # must not change merely because a reader upgraded its contract library.
    if isinstance(value, TaskSpec):
        defaults: dict[str, object] = {
            "authority_scope": (),
            "retry_categories": (),
            "cancellation_mode": "cooperative",
            "partial_allowed": False,
            "conflict_keys": (),
        }
        for key, default in defaults.items():
            if getattr(value, key) == default:
                raw.pop(key, None)
    elif isinstance(value, TaskResult):
        defaults = {
            "authority_scope": (),
            "partial": False,
            "conflict_keys": (),
            "checkpoint": {},
        }
        for key, default in defaults.items():
            if getattr(value, key) == default:
                raw.pop(key, None)
    for key, item in tuple(raw.items()):
        if isinstance(item, datetime):
            raw[key] = item.isoformat()
    result = ensure_json_safe(raw)
    if not isinstance(result, dict):  # pragma: no cover - asdict always returns a dict
        raise TypeError("contract must serialize to an object")
    return result


def decision_envelope_from_mapping(value: Mapping[str, object]) -> DecisionEnvelope:
    """Read a v2 decision or upcast a legacy v1 decision as read-only state."""

    def required_text(source: Mapping[str, object], key: str) -> str:
        item = source.get(key)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"DecisionEnvelope {key} is required")
        return item

    def optional_text(source: Mapping[str, object], key: str) -> str | None:
        item = source.get(key)
        if item is None:
            return None
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"DecisionEnvelope {key} must be a non-empty string or null")
        return item

    source_schema_version = value.get("schema_version")
    if source_schema_version not in {"1", "2"}:
        raise ValueError("unsupported DecisionEnvelope schema_version")
    if value.get("shadow_only") is not True:
        raise ValueError("DecisionEnvelope must remain shadow-only")

    run_id = required_text(value, "run_id")
    if source_schema_version == "1":
        actor_id = f"legacy:{run_id}"
        turn_id = run_id
        upcast_from_schema_version: str | None = "1"
    else:
        actor_id = required_text(value, "actor_id")
        turn_id = required_text(value, "turn_id")
        raw_upcast_source = value.get("upcast_from_schema_version")
        if raw_upcast_source not in {None, "1"}:
            raise ValueError("unsupported DecisionEnvelope upcast source")
        upcast_from_schema_version = cast(str | None, raw_upcast_source)
        if value.get("fresh_turn_resume_authorized") is not False:
            raise ValueError("fresh-turn resume is not authorized by DecisionEnvelope v2")

    raw_recovery = value.get("recovery_action")
    recovery: RecoveryAction | None = None
    if raw_recovery is not None:
        if not isinstance(raw_recovery, Mapping):
            raise TypeError("DecisionEnvelope recovery_action must be an object or null")
        raw_budget = raw_recovery.get("retry_budget_remaining", 0)
        if isinstance(raw_budget, bool) or not isinstance(raw_budget, int):
            raise TypeError("DecisionEnvelope retry_budget_remaining must be an integer")
        recovery = RecoveryAction(
            action=cast(RecoveryActionName, required_text(raw_recovery, "action")),
            failure_category=required_text(raw_recovery, "failure_category"),
            next_action=required_text(raw_recovery, "next_action"),
            retry_budget_remaining=raw_budget,
            missing_capability=optional_text(raw_recovery, "missing_capability"),
            missing_material=optional_text(raw_recovery, "missing_material"),
        )

    raw_interruption = value.get("human_interruption")
    interruption: HumanInterruption | None = None
    if raw_interruption is not None:
        if not isinstance(raw_interruption, Mapping):
            raise TypeError("DecisionEnvelope human_interruption must be an object or null")
        interruption = HumanInterruption(
            interruption_type=cast(
                HumanInterruptionType,
                required_text(raw_interruption, "interruption_type"),
            ),
            reason=required_text(raw_interruption, "reason"),
            next_action=required_text(raw_interruption, "next_action"),
        )

    return DecisionEnvelope(
        run_id=run_id,
        attempt_id=required_text(value, "attempt_id"),
        phase=required_text(value, "phase"),
        disposition=cast(
            DecisionDisposition,
            required_text(value, "disposition"),
        ),
        next_phase=required_text(value, "next_phase"),
        recovery_action=recovery,
        human_interruption=interruption,
        actor_id=actor_id,
        turn_id=turn_id,
        upcast_from_schema_version=upcast_from_schema_version,
        fresh_turn_resume_authorized=False,
        shadow_only=True,
        schema_version="2",
    )


def agent_proposal_from_mapping(
    value: Mapping[str, object],
    *,
    default_proposal_id: str | None = None,
) -> AgentProposal:
    """Parse one provider-neutral proposal from a structured tool result."""
    payload = value.get("payload") or {}
    if not isinstance(payload, Mapping):
        raise TypeError("proposal payload must be an object")
    depends_on = value.get("depends_on") or ()
    if not isinstance(depends_on, (list, tuple)):
        raise TypeError("proposal depends_on must be an array")
    concurrency_key = value.get("concurrency_key")
    return AgentProposal(
        proposal_id=str(
            value.get("proposal_id")
            or default_proposal_id
            or f"proposal-{uuid.uuid4()}"
        ),
        kind=str(value.get("kind") or ""),
        summary=str(value.get("summary") or ""),
        payload=dict(payload),
        depends_on=tuple(str(item) for item in depends_on),
        concurrency_mode=str(value.get("concurrency_mode") or "adaptive"),
        concurrency_key=None if concurrency_key is None else str(concurrency_key),
        priority=int(value.get("priority") or 0),
    )


def failure_observation_from_mapping(value: Mapping[str, object]) -> FailureObservation:
    """Parse the exact v1 failure-observation wire contract."""
    allowed_keys = {
        "code",
        "source",
        "provider",
        "phase",
        "submit_started",
        "field_semantic",
        "page_epoch",
        "evidence_refs",
        "detail_ref",
        "missing_capability",
        "missing_material",
        "schema_version",
    }
    unknown_keys = set(value) - allowed_keys
    if unknown_keys:
        raise ValueError(
            "unsupported FailureObservation fields: " + ", ".join(sorted(unknown_keys))
        )
    evidence_refs = value.get("evidence_refs", ())
    if not isinstance(evidence_refs, (list, tuple)):
        raise TypeError("FailureObservation evidence_refs must be an array")

    def optional_text(key: str) -> str | None:
        item = value.get(key)
        if item is None:
            return None
        if not isinstance(item, str):
            raise TypeError(f"FailureObservation {key} must be a string or null")
        return item

    submit_started = value.get("submit_started")
    if not isinstance(submit_started, bool):
        raise TypeError("FailureObservation submit_started must be a boolean")
    page_epoch = value.get("page_epoch")
    if page_epoch is not None and (
        isinstance(page_epoch, bool) or not isinstance(page_epoch, int)
    ):
        raise TypeError("FailureObservation page_epoch must be an integer or null")
    return FailureObservation(
        code=str(value.get("code") or ""),
        source=str(value.get("source") or ""),
        provider=str(value.get("provider") or ""),
        phase=str(value.get("phase") or ""),
        submit_started=submit_started,
        field_semantic=optional_text("field_semantic"),
        page_epoch=page_epoch,
        evidence_refs=tuple(str(item) for item in evidence_refs),
        detail_ref=optional_text("detail_ref"),
        missing_capability=optional_text("missing_capability"),
        missing_material=optional_text("missing_material"),
        schema_version=(
            str(value["schema_version"])
            if "schema_version" in value
            else "1"
        ),
    )


def agent_turn_result_from_mapping(
    value: Mapping[str, object],
    *,
    expected_run_id: str | None = None,
) -> AgentTurnResult:
    """Parse and validate a structured Agent turn result.

    A launcher-supplied ``expected_run_id`` prevents a runtime from attaching a
    report to another run.  Status values remain open for future agents; the
    application-state adapter separately decides which values are admissible.
    """
    supplied_run_id = str(value.get("run_id") or "")
    if expected_run_id and supplied_run_id and supplied_run_id != expected_run_id:
        raise ValueError("structured result run_id does not match the active run")
    run_id = expected_run_id or supplied_run_id
    proposals_raw = value.get("proposals") or ()
    if not isinstance(proposals_raw, (list, tuple)):
        raise TypeError("proposals must be an array")
    if len(proposals_raw) > MAX_AGENT_PROPOSALS:
        raise ValueError(f"a result may contain at most {MAX_AGENT_PROPOSALS} proposals")
    proposals: list[AgentProposal] = []
    for index, raw in enumerate(proposals_raw):
        if not isinstance(raw, Mapping):
            raise TypeError("each proposal must be an object")
        proposals.append(
            agent_proposal_from_mapping(
                raw,
                default_proposal_id=(
                    f"{run_id}:proposal:{index + 1}" if run_id else None
                ),
            )
        )
    observations = value.get("observations") or {}
    if not isinstance(observations, Mapping):
        raise TypeError("observations must be an object")
    requested = value.get("requested_human_input")
    failure_raw = value.get("failure")
    if failure_raw is not None and not isinstance(failure_raw, Mapping):
        raise TypeError("failure must be an object or null")
    return AgentTurnResult(
        run_id=run_id,
        status=str(value.get("status") or ""),
        summary=str(value.get("summary") or ""),
        proposals=tuple(proposals),
        observations=dict(observations),
        failure=(
            failure_observation_from_mapping(failure_raw)
            if isinstance(failure_raw, Mapping)
            else None
        ),
        requested_human_input=None if requested is None else str(requested),
    )


@runtime_checkable
class AgentRuntime(Protocol):
    """Minimal port implemented by Codex, Claude, SDK, or local runtimes."""

    def run(self, request: AgentRunRequest) -> AgentTurnResult: ...

    def resume(self, run_id: str, human_input: Mapping[str, object]) -> AgentTurnResult: ...

    def cancel(self, run_id: str) -> None: ...
