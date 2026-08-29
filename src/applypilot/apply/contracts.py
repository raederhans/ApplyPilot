"""Provider-neutral contracts for agent-assisted application work.

These contracts describe proposals and observations, not the authoritative job
state.  Phases, side effects, and concurrency modes are deliberately strings so
new runtimes and workflows can extend them without changing the core package.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

MAX_AGENT_REPORT_BYTES = 131_072
MAX_AGENT_PROPOSALS = 50
MAX_PROPOSAL_DEPENDENCIES = 50
MAX_TASK_DEPENDENCIES = 100

_FORBIDDEN_PERSISTED_KEYS = {
    "api_key",
    "authorization",
    "browser_handle",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "otp",
    "body",
    "email_body",
    "mailbox_content",
    "message_body",
    "page_handle",
    "password",
    "secret",
    "session_cookie",
    "security_code",
    "token",
    "verification_code",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


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
    concurrency_mode: str = "adaptive"
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required(self.name, "name")
        _required(self.description, "description")
        _required(self.side_effect, "side_effect")
        _required(self.concurrency_mode, "concurrency_mode")
        ensure_json_safe(self.input_schema, path="$.input_schema")
        ensure_json_safe(self.output_schema, path="$.output_schema")
        ensure_json_safe(self.metadata, path="$.metadata")
        _strings(self.phases, "phases")
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
    resource_claims: tuple[ResourceClaim, ...] = ()
    retry_budget: int = 0
    deadline_at: datetime | None = None
    idempotency_key: str | None = None
    priority: int = 0
    counts_toward_target: bool = False
    resume_cursor: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("task_id", "kind", "objective", "effect_class"):
            _required(getattr(self, name), name)
        ensure_persistable(self.inputs, path="$.inputs")
        ensure_persistable(self.resume_cursor, path="$.resume_cursor")
        _strings(self.depends_on, "depends_on")
        _strings(self.required_results, "required_results")
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
    evidence_refs: tuple[str, ...] = ()
    metrics: Mapping[str, object] = field(default_factory=dict)
    failure_category: str | None = None
    retryable: bool = False
    resume_cursor: Mapping[str, object] = field(default_factory=dict)
    completed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _required(self.task_id, "task_id")
        _required(self.status, "status")
        ensure_persistable(self.output, path="$.output")
        ensure_persistable(self.metrics, path="$.metrics")
        ensure_persistable(self.resume_cursor, path="$.resume_cursor")
        _strings(self.evidence_refs, "evidence_refs")
        if self.failure_category is not None:
            _required(self.failure_category, "failure_category")
        _aware(self.completed_at, "completed_at")

    @property
    def succeeded(self) -> bool:
        return self.status.casefold() in {"completed", "succeeded", "ok"}


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    run_id: str
    attempt_id: str
    agent_role: str
    phase: str
    objective: str
    context: Mapping[str, object] = field(default_factory=dict)
    available_tools: tuple[str, ...] = ()
    parent_run_id: str | None = None
    proposal_group_id: str | None = None
    concurrency_mode: str = "adaptive"
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("run_id", "attempt_id", "agent_role", "phase", "objective", "concurrency_mode"):
            _required(getattr(self, name), name)
        _aware(self.created_at, "created_at")
        ensure_persistable(self.context, path="$.context")
        _strings(self.available_tools, "available_tools")


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    run_id: str
    status: str
    summary: str
    proposals: tuple[AgentProposal, ...] = ()
    observations: Mapping[str, object] = field(default_factory=dict)
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
        if self.requested_human_input is not None:
            _required(self.requested_human_input, "requested_human_input")


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
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("event_id", "attempt_id", "run_id", "phase", "actor", "event_type"):
            _required(getattr(self, name), name)
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
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("checkpoint_id", "run_id", "attempt_id", "phase"):
            _required(getattr(self, name), name)
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
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
        _aware(self.created_at, "created_at")
        if self.resolved_at is not None:
            _aware(self.resolved_at, "resolved_at")
        ensure_persistable(self.context, path="$.context")


def contract_json(value: object) -> dict[str, JsonValue]:
    """Return a JSON-safe dictionary for a contract dataclass."""
    raw = asdict(value)
    for key, item in tuple(raw.items()):
        if isinstance(item, datetime):
            raw[key] = item.isoformat()
    result = ensure_json_safe(raw)
    if not isinstance(result, dict):  # pragma: no cover - asdict always returns a dict
        raise TypeError("contract must serialize to an object")
    return result


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
    return AgentTurnResult(
        run_id=run_id,
        status=str(value.get("status") or ""),
        summary=str(value.get("summary") or ""),
        proposals=tuple(proposals),
        observations=dict(observations),
        requested_human_input=None if requested is None else str(requested),
    )


@runtime_checkable
class AgentRuntime(Protocol):
    """Minimal port implemented by Codex, Claude, SDK, or local runtimes."""

    def run(self, request: AgentRunRequest) -> AgentTurnResult: ...

    def resume(self, run_id: str, human_input: Mapping[str, object]) -> AgentTurnResult: ...

    def cancel(self, run_id: str) -> None: ...
