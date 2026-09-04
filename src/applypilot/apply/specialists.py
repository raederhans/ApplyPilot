"""Allowlisted deterministic specialists for production preflight work."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from applypilot.apply.ats import (
    ATS_SCHEMA_VERSION,
    adapter_prompt_context,
    build_form_ir,
    detect_ats_site,
    propose_fill_plan,
)
from applypilot.apply.contracts import (
    ApplicationEvent,
    TaskResult,
    TaskSpec,
    ensure_persistable,
)
from applypilot.apply.material_readiness import (
    evaluate_material_readiness,
    material_snapshot_identity,
)
from applypilot.storage import task_journal

Specialist = Callable[[Mapping[str, object]], dict[str, object]]

ATS_FORM_SNAPSHOT_SCHEMA_VERSION = "ats-form-snapshot-v1"
ATS_FILL_PLAN_INPUT_SCHEMA_VERSION = "ats-fill-plan-input-v1"
ATS_FILL_PLAN_OUTPUT_SCHEMA_VERSION = "ats-fill-plan-output-v1"
SPECIALIST_MODES = frozenset({"off", "shadow", "advisory", "required"})
READ_ONLY_SPECIALIST_AUTHORITY = (
    "read:bounded_snapshot",
    "write:control_heartbeat",
    "write:control_checkpoint",
    "emit:advisory_context",
)
def normalize_specialist_mode(mode: str) -> str:
    normalized = mode.casefold().strip()
    if normalized == "enforce":
        normalized = "required"
    if normalized not in SPECIALIST_MODES:
        raise ValueError("specialist mode must be off, shadow, advisory, or required")
    return normalized


@dataclass(frozen=True, slots=True)
class ProductionSpecialistSpec:
    """Static admission contract for one production specialist.

    Admission limits are enforced by ``dispatch_production_specialist``.  The
    execution budget is explicitly descriptive until a scheduler owns timeout
    cancellation; none of this metadata grants browser, persistence, or submit
    authority.
    """

    specialist_id: str
    phases: tuple[str, ...]
    effect_class: str
    input_schema_version: str
    output_schema_version: str
    execution_budget_seconds: int
    max_output_bytes: int
    authority_scope: tuple[str, ...]
    read_only: bool
    capabilities: tuple[str, ...]
    retry_categories: tuple[str, ...]
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.effect_class != "read" or not self.read_only:
            raise ValueError("production specialists must be read-only")
        if self.capabilities:
            raise ValueError("production specialists may not receive runtime capabilities")
        if not set(self.authority_scope) <= set(READ_ONLY_SPECIALIST_AUTHORITY):
            raise ValueError("production specialist authority exceeds the read-only boundary")
        ensure_persistable(self.metadata, path="$.specialist_metadata")


@dataclass(frozen=True, slots=True)
class AtsFillPlanSpecialistRun:
    task_id: str
    proposal_id: str
    result: dict[str, object]
    replay: bool
    telemetry: tuple[dict[str, object], ...]


class AtsFillPlanOutputLimitError(RuntimeError):
    def __init__(self, stdout_prefix: bytes, stderr_prefix: bytes) -> None:
        super().__init__("ATS fill-plan subprocess output exceeds limit")
        self.stdout_prefix = stdout_prefix
        self.stderr_prefix = stderr_prefix


class SpecialistDeadlineExceeded(RuntimeError):
    pass


class SpecialistCancelled(RuntimeError):
    pass


def _run_deterministic_read_specialist(
    runner: Specialist,
    snapshot: Mapping[str, object],
    *,
    timeout_seconds: float,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Run a deterministic read synchronously with cooperative boundary checks."""
    frozen = ensure_persistable(snapshot, path="$.specialist_input")
    assert isinstance(frozen, dict)
    if cancelled is not None and cancelled():
        raise SpecialistCancelled("specialist execution cancelled")
    started = time.monotonic()
    value = runner(frozen)
    if cancelled is not None and cancelled():
        raise SpecialistCancelled("specialist execution cancelled")
    if time.monotonic() - started >= max(timeout_seconds, 0.001):
        raise SpecialistDeadlineExceeded("specialist execution deadline exceeded")
    if not isinstance(value, dict):
        raise TypeError("specialist result must be an object")
    return value


_PRODUCTION_SPECIALIST_SPECS = {
    "ats-fill-plan-v1": ProductionSpecialistSpec(
        specialist_id="ats-fill-plan-v1",
        phases=("prepare",),
        effect_class="read",
        input_schema_version=ATS_FILL_PLAN_INPUT_SCHEMA_VERSION,
        output_schema_version=ATS_FILL_PLAN_OUTPUT_SCHEMA_VERSION,
        execution_budget_seconds=5,
        max_output_bytes=16 * 1024,
        authority_scope=READ_ONLY_SPECIALIST_AUTHORITY,
        read_only=True,
        capabilities=(),
        retry_categories=("specialist_timeout", "specialist_transient"),
        metadata={
            "execution_budget_seconds": 5,
            "execution_budget_enforced": False,
            "prepare_only": True,
        },
    ),
}

for _specialist_id, _phase in (
    ("field-semantic-v1", "prepare"),
    ("provider-classifier-v1", "preflight"),
    ("application-facts-v1", "preflight"),
    ("work-authorization-v1", "preflight"),
    ("page-failure-v1", "observe"),
):
    _PRODUCTION_SPECIALIST_SPECS[_specialist_id] = ProductionSpecialistSpec(
        specialist_id=_specialist_id,
        phases=(_phase,),
        effect_class="read",
        input_schema_version=f"{_specialist_id}-input",
        output_schema_version=f"{_specialist_id}-output",
        execution_budget_seconds=2,
        max_output_bytes=4096,
        authority_scope=READ_ONLY_SPECIALIST_AUTHORITY,
        read_only=True,
        capabilities=(),
        retry_categories=("specialist_timeout", "specialist_transient"),
        metadata={
            "deterministic": True,
            "proposal_only": True,
            "execution_budget_enforced": False,
        },
    )


def production_specialist_spec(name: str) -> ProductionSpecialistSpec:
    try:
        return _PRODUCTION_SPECIALIST_SPECS[name]
    except KeyError:
        raise ValueError(f"specialist is not allowlisted: {name}") from None


def _json_domain_copy(value: object, *, path: str = "$") -> object:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{path} contains a non-JSON number")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string JSON object key")
            copied[key] = _json_domain_copy(item, path=f"{path}.{key}")
        return copied
    if isinstance(value, list):
        return [
            _json_domain_copy(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains a non-JSON value: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError("specialist data must be JSON-safe") from exc


def specialist_snapshot_digest(snapshot: Mapping[str, object]) -> str:
    """Return the stable digest used to bind an Agent reference to a catalog item."""
    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be an object")
    copied = _json_domain_copy(snapshot)
    return hashlib.sha256(_canonical_json_bytes(copied)).hexdigest()


_SNAPSHOT_KEYS = frozenset(
    {"schema_version", "target_url", "form_fields", "available_fact_names"}
)
_FIELD_KEYS = frozenset(
    {
        "field_key",
        "id",
        "name",
        "selector",
        "label",
        "aria_label",
        "type",
        "tag",
        "control",
        "autocomplete",
        "placeholder",
        "required",
        "disabled",
        "readonly",
        "options",
        "minlength",
        "maxlength",
        "min",
        "max",
        "pattern",
        "multiple",
        # Observers may supply these, but build_form_ir intentionally strips them.
        "value",
        "current_value",
        "default_value",
        "text_content",
        "inner_text",
        "files",
        "selected_value",
    }
)
_VALUE_SHAPED_OUTPUT_KEYS = frozenset(
    {
        "value",
        "current_value",
        "default_value",
        "text_content",
        "inner_text",
        "files",
        "selected_value",
        "password",
        "credential",
        "credentials",
        "token",
        "cookie",
        "browser_port",
        "mailbox",
        "database",
    }
)
_PLAN_ACTIONS = frozenset({"fill", "select", "upload", "skip", "request_fact", "review"})
_PLAN_KEYS = frozenset(
    {"schema_version", "adapter", "site", "field_count", "truncated", "fields", "actions"}
)
_PLAN_FIELD_KEYS = frozenset(
    {
        "field_key",
        "semantic",
        "control",
        "required",
        "writable",
        "option_count",
        "options",
        "options_truncated",
    }
)
_PLAN_ACTION_KEYS = frozenset(
    {"field_key", "semantic", "action", "source_key", "requires_review"}
)
_MAX_SPECIALIST_TEXT = 240
_MAX_SPECIALIST_KEY = 160
_MAX_SPECIALIST_OPTION_TEXT = 80


def _validated_snapshot(snapshot: object) -> dict[str, object]:
    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot catalog entry must be an object")
    copied = dict(snapshot)
    if set(copied) != _SNAPSHOT_KEYS:
        raise ValueError("snapshot keys do not match the allowlisted schema")
    if copied.get("schema_version") != ATS_FORM_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("snapshot schema mismatch")
    if not isinstance(copied.get("target_url"), str) or not copied["target_url"].strip():
        raise ValueError("snapshot target_url is required")
    fields = copied.get("form_fields")
    if not isinstance(fields, list):
        raise TypeError("snapshot form_fields must be an array")
    sanitized_fields: list[dict[str, object]] = []
    for field in fields:
        if not isinstance(field, Mapping):
            raise TypeError("snapshot form field must be an object")
        if not set(field) <= _FIELD_KEYS:
            raise ValueError("snapshot form field contains unsupported keys")
        sanitized = {
            key: value
            for key, value in field.items()
            if key.casefold() not in _VALUE_SHAPED_OUTPUT_KEYS
        }
        options = sanitized.get("options")
        if options is not None:
            if not isinstance(options, list):
                raise TypeError("snapshot field options must be an array")
            normalized_options: list[str] = []
            for option in options:
                if isinstance(option, str):
                    option_text = option
                elif isinstance(option, Mapping):
                    if not option or not set(option) <= {"label", "text"}:
                        raise ValueError("snapshot option object has unsupported keys")
                    if any(not isinstance(value, str) for value in option.values()):
                        raise TypeError("snapshot option label/text must be strings")
                    option_text = str(option.get("label") or option.get("text") or "")
                else:
                    raise TypeError("snapshot option must be a string or label/text object")
                option_text = " ".join(option_text.split())[:_MAX_SPECIALIST_OPTION_TEXT]
                if not option_text:
                    raise ValueError("snapshot option text must not be empty")
                normalized_options.append(option_text)
            sanitized["options"] = normalized_options
        sanitized_fields.append(sanitized)
    facts = copied.get("available_fact_names")
    if not isinstance(facts, list) or any(
        not isinstance(fact, str)
        or not fact.strip()
        or len(fact) > _MAX_SPECIALIST_KEY
        for fact in facts
    ):
        raise TypeError("snapshot available_fact_names must contain non-empty strings")
    if len(set(facts)) != len(facts):
        raise ValueError("snapshot available_fact_names must be unique")
    copied["form_fields"] = sanitized_fields
    return copied


def _run_ats_fill_plan(snapshot: Mapping[str, object]) -> dict[str, object]:
    form = build_form_ir(str(snapshot["target_url"]), snapshot["form_fields"])
    plan = propose_fill_plan(form, snapshot["available_fact_names"])
    return {
        "schema_version": ATS_FILL_PLAN_OUTPUT_SCHEMA_VERSION,
        "plan": adapter_prompt_context(form, plan),
    }


_PRODUCTION_DISPATCH_RUNNERS: dict[str, Specialist] = {
    "ats-fill-plan-v1": _run_ats_fill_plan,
}


def _contains_value_shaped_output(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in _VALUE_SHAPED_OUTPUT_KEYS
            or _contains_value_shaped_output(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_value_shaped_output(child) for child in value)
    return False


def _validate_dispatch_output(
    output: object,
    *,
    spec: ProductionSpecialistSpec,
    snapshot: Mapping[str, object],
) -> tuple[dict[str, object], bytes]:
    if not isinstance(output, Mapping):
        raise TypeError("specialist output must be an object")
    copied = dict(output)
    encoded = _canonical_json_bytes(copied)
    if len(encoded) > spec.max_output_bytes:
        raise ValueError("specialist output exceeds the configured byte limit")
    if set(copied) != {"schema_version", "plan"}:
        raise ValueError("specialist output does not match the output schema")
    if copied.get("schema_version") != spec.output_schema_version:
        raise ValueError("specialist output schema mismatch")
    if _contains_value_shaped_output(copied):
        raise ValueError("specialist output contains value-shaped data")
    expected = _run_ats_fill_plan(snapshot)
    expected_plan = expected["plan"]
    expected_fields = {
        field["field_key"]: field["semantic"] for field in expected_plan["fields"]
    }
    plan = copied.get("plan")
    if not isinstance(plan, Mapping):
        raise TypeError("specialist output plan must be an object")
    if set(plan) != _PLAN_KEYS:
        raise ValueError("specialist output plan does not match the output schema")
    if plan.get("schema_version") != ATS_SCHEMA_VERSION:
        raise ValueError("specialist output plan schema mismatch")
    for key in ("adapter", "site"):
        value = plan.get(key)
        if not isinstance(value, str) or not value or len(value) > _MAX_SPECIALIST_TEXT:
            raise TypeError(f"specialist output {key} must be a bounded string")
    field_count = plan.get("field_count")
    if (
        isinstance(field_count, bool)
        or not isinstance(field_count, int)
        or field_count < 0
        or field_count > 200
    ):
        raise TypeError("specialist output field_count must be a bounded integer")
    if not isinstance(plan.get("truncated"), bool):
        raise TypeError("specialist output truncated must be a boolean")
    fields = plan.get("fields")
    if not isinstance(fields, list):
        raise TypeError("specialist output fields must be an array")
    field_semantics: dict[str, str] = {}
    for field in fields:
        if not isinstance(field, Mapping) or set(field) != _PLAN_FIELD_KEYS:
            raise ValueError("specialist output field does not match the output schema")
        field_key = field.get("field_key")
        semantic = field.get("semantic")
        control = field.get("control")
        if (
            not isinstance(field_key, str)
            or not field_key
            or len(field_key) > _MAX_SPECIALIST_KEY
            or not isinstance(semantic, str)
            or not semantic
            or len(semantic) > _MAX_SPECIALIST_KEY
            or not isinstance(control, str)
            or not control
            or len(control) > _MAX_SPECIALIST_KEY
        ):
            raise TypeError("specialist output field identifiers must be bounded strings")
        if field_key in field_semantics:
            raise ValueError("specialist output field keys must be unique")
        if field_key not in expected_fields or semantic != expected_fields[field_key]:
            raise ValueError("specialist output field is not bound to the snapshot")
        field_semantics[field_key] = semantic
        if not isinstance(field.get("required"), bool) or not isinstance(
            field.get("writable"), bool
        ):
            raise TypeError("specialist output field flags must be booleans")
        option_count = field.get("option_count")
        options = field.get("options")
        if (
            isinstance(option_count, bool)
            or not isinstance(option_count, int)
            or option_count < 0
            or not isinstance(options, list)
            or any(
                not isinstance(option, str)
                or not option
                or len(option) > _MAX_SPECIALIST_OPTION_TEXT
                for option in options
            )
            or option_count < len(options)
            or not isinstance(field.get("options_truncated"), bool)
        ):
            raise TypeError("specialist output options do not match the bounded schema")
    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise TypeError("specialist output actions must be an array")
    action_keys: set[str] = set()
    allowed_facts = set(snapshot["available_fact_names"])
    for action in actions:
        if (
            not isinstance(action, Mapping)
            or set(action) != _PLAN_ACTION_KEYS
            or action.get("action") not in _PLAN_ACTIONS
        ):
            raise ValueError("specialist output contains an unauthorized action")
        field_key = action.get("field_key")
        semantic = action.get("semantic")
        source_key = action.get("source_key")
        if not isinstance(field_key, str) or field_key not in field_semantics:
            raise ValueError("specialist output action references an unknown field")
        if field_key in action_keys:
            raise ValueError("specialist output actions must be unique per field")
        action_keys.add(field_key)
        if semantic != field_semantics[field_key]:
            raise ValueError("specialist output field/action semantics do not match")
        if source_key is not None and (
            not isinstance(source_key, str)
            or not source_key
            or len(source_key) > _MAX_SPECIALIST_KEY
            or source_key not in allowed_facts
        ):
            raise ValueError("specialist output source_key is not an available fact name")
        if not isinstance(action.get("requires_review"), bool):
            raise TypeError("specialist output requires_review must be a boolean")
    if action_keys != set(field_semantics):
        raise ValueError("specialist output must contain exactly one action per field")
    if copied != expected:
        raise ValueError("specialist output does not match the deterministic snapshot plan")
    return copied, encoded


def dispatch_production_specialist(
    kind: str,
    *,
    phase: str,
    payload: Mapping[str, object],
    snapshot_catalog: Mapping[str, object],
) -> dict[str, object]:
    """Resolve and run a bounded read-only specialist request.

    Agent-controlled payloads contain only an opaque snapshot reference and its
    digest.  The caller owns the catalog and no live handles or callables cross
    this boundary.
    """
    spec = production_specialist_spec(kind)
    if spec.effect_class != "read" or not spec.read_only:
        raise PermissionError("production specialist is not admitted as read-only")
    if phase not in spec.phases:
        raise ValueError(f"specialist phase is not allowed: {phase}")
    if not isinstance(payload, Mapping):
        raise TypeError("specialist payload must be an object")
    required_keys = {"snapshot_ref", "snapshot_sha256", "schema_version"}
    if set(payload) != required_keys:
        raise ValueError("specialist payload keys do not match the input schema")
    if payload.get("schema_version") != spec.input_schema_version:
        raise ValueError("specialist input schema mismatch")
    snapshot_ref = payload.get("snapshot_ref")
    supplied_digest = payload.get("snapshot_sha256")
    if not isinstance(snapshot_ref, str) or not snapshot_ref.strip():
        raise ValueError("snapshot ref is required")
    if not isinstance(supplied_digest, str) or len(supplied_digest) != 64:
        raise ValueError("snapshot digest is invalid")
    if not isinstance(snapshot_catalog, Mapping) or snapshot_ref not in snapshot_catalog:
        raise ValueError("snapshot ref is missing from the catalog")
    catalog_copy = _json_domain_copy(snapshot_catalog[snapshot_ref])
    catalog_bytes = _canonical_json_bytes(catalog_copy)
    actual_digest = hashlib.sha256(catalog_bytes).hexdigest()
    if not hmac.compare_digest(supplied_digest.casefold(), actual_digest):
        raise ValueError("snapshot digest mismatch")
    # The runner receives only an independent JSON round-trip of the exact bytes
    # used for the digest, never caller-owned objects or a second observation.
    snapshot = _validated_snapshot(json.loads(catalog_bytes))

    output, encoded = _validate_dispatch_output(
        _PRODUCTION_DISPATCH_RUNNERS[kind](snapshot),
        spec=spec,
        snapshot=snapshot,
    )
    plan_digest = hashlib.sha256(encoded).hexdigest()
    result = {
        "kind": kind,
        "schema_version": spec.output_schema_version,
        "snapshot_ref": snapshot_ref,
        "snapshot_sha256": actual_digest,
        "plan_sha256": plan_digest,
        "plan": output["plan"],
    }
    if len(_canonical_json_bytes(result)) > spec.max_output_bytes:
        raise ValueError("specialist output exceeds the configured byte limit")
    return result


def freeze_ats_fill_plan_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    """Freeze a caller-owned observation into the value-free catalog schema."""
    copied = _json_domain_copy(snapshot)
    encoded = _canonical_json_bytes(copied)
    return _validated_snapshot(json.loads(encoded))


def prompt_safe_ats_fill_plan(result: Mapping[str, object]) -> dict[str, object]:
    """Project a validated plan for Agent consumption without visible option text."""
    copied = _json_domain_copy(result)
    if not isinstance(copied, dict):  # pragma: no cover - Mapping copies to dict
        raise TypeError("ATS fill plan result must be an object")
    plan = copied.get("plan")
    if not isinstance(plan, dict):
        raise TypeError("ATS fill plan result has no plan")
    fields = plan.get("fields")
    if not isinstance(fields, list):
        raise TypeError("ATS fill plan has no fields")
    projected_fields: list[dict[str, object]] = []
    for field in fields:
        if not isinstance(field, dict):
            raise TypeError("ATS fill plan field must be an object")
        options = field.get("options")
        if not isinstance(options, list):
            raise TypeError("ATS fill plan options must be an array")
        projected = {
            key: value
            for key, value in field.items()
            if key not in {"options", "options_truncated"}
        }
        projected["options_sha256"] = hashlib.sha256(
            _canonical_json_bytes(options)
        ).hexdigest()
        projected_fields.append(projected)
    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise TypeError("ATS fill plan has no actions")
    safe_plan = {
        key: plan[key]
        for key in (
            "schema_version",
            "adapter",
            "site",
            "field_count",
            "truncated",
        )
    }
    safe_plan["fields"] = projected_fields
    safe_plan["actions"] = [
        {
            key: action[key]
            for key in (
                "field_key",
                "semantic",
                "action",
                "source_key",
                "requires_review",
            )
        }
        for action in actions
        if isinstance(action, Mapping)
    ]
    return {
        "kind": copied["kind"],
        "schema_version": copied["schema_version"],
        "snapshot_ref": copied["snapshot_ref"],
        "snapshot_sha256": copied["snapshot_sha256"],
        "plan_sha256": copied["plan_sha256"],
        "plan": safe_plan,
        "side_effect": "read-only-advisory",
        "submit_authority": False,
    }


def validate_ats_fill_plan_result(
    result: object, snapshot: Mapping[str, object]
) -> dict[str, object]:
    if not isinstance(result, Mapping):
        raise TypeError("ATS fill-plan result must be an object")
    copied = _json_domain_copy(result)
    if not isinstance(copied, dict):  # pragma: no cover
        raise TypeError("ATS fill-plan result must be an object")
    expected_keys = {
        "kind",
        "schema_version",
        "snapshot_ref",
        "snapshot_sha256",
        "plan_sha256",
        "plan",
    }
    if set(copied) != expected_keys:
        raise ValueError("ATS fill-plan result keys do not match the outer schema")
    frozen = freeze_ats_fill_plan_snapshot(snapshot)
    snapshot_digest = specialist_snapshot_digest(frozen)
    snapshot_ref = f"ats-form:{snapshot_digest}"
    spec = production_specialist_spec("ats-fill-plan-v1")
    if copied["kind"] != "ats-fill-plan-v1":
        raise ValueError("ATS fill-plan result kind mismatch")
    if copied["schema_version"] != spec.output_schema_version:
        raise ValueError("ATS fill-plan result schema mismatch")
    if copied["snapshot_ref"] != snapshot_ref:
        raise ValueError("ATS fill-plan result snapshot ref mismatch")
    if copied["snapshot_sha256"] != snapshot_digest:
        raise ValueError("ATS fill-plan result snapshot digest mismatch")
    inner = {"schema_version": copied["schema_version"], "plan": copied["plan"]}
    _, encoded = _validate_dispatch_output(inner, spec=spec, snapshot=frozen)
    expected_plan_digest = hashlib.sha256(encoded).hexdigest()
    if copied["plan_sha256"] != expected_plan_digest:
        raise ValueError("ATS fill-plan result plan digest mismatch")
    return copied


def _ats_fill_plan_task_identity(
    *, attempt_id: str, snapshot_digest: str
) -> tuple[str, str]:
    proposal_id = f"ats-fill-plan-v1:{attempt_id}:{snapshot_digest[:16]}"
    return f"task:{proposal_id}", proposal_id


def _ats_fill_plan_subprocess_request(
    *, payload: Mapping[str, object], snapshot_ref: str, snapshot: Mapping[str, object]
) -> str:
    return json.dumps(
        {
            "kind": "ats-fill-plan-v1",
            "phase": "prepare",
            "payload": dict(payload),
            "snapshot_catalog": {snapshot_ref: dict(snapshot)},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _ats_fill_plan_subprocess_environment() -> dict[str, str]:
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        source_root if not existing else os.pathsep.join((source_root, existing))
    )
    return env


def _run_bounded_subprocess(
    command: list[str],
    *,
    input_text: str,
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int = 4096,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=None if env is None else dict(env),
    )
    stdout = bytearray()
    stderr = bytearray()
    over_limit = threading.Event()

    def read_bounded(stream, target: bytearray, limit: int) -> None:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            remaining = max(0, limit - len(target))
            target.extend(chunk[:remaining])
            if len(chunk) > remaining:
                over_limit.set()
                try:
                    process.terminate()
                except OSError:
                    pass
                return

    readers = (
        threading.Thread(target=read_bounded, args=(process.stdout, stdout, stdout_limit)),
        threading.Thread(target=read_bounded, args=(process.stderr, stderr, stderr_limit)),
    )
    for reader in readers:
        reader.start()
    assert process.stdin is not None
    try:
        process.stdin.write(input_text.encode("utf-8"))
        process.stdin.close()
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if over_limit.is_set():
                process.terminate()
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            over_limit.wait(timeout=min(0.02, remaining))
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        for reader in readers:
            reader.join(timeout=1)
        if process.poll() is None:
            process.kill()
            process.wait()
    if over_limit.is_set():
        if process.poll() is None:
            process.kill()
            process.wait()
        raise AtsFillPlanOutputLimitError(bytes(stdout), bytes(stderr))
    return subprocess.CompletedProcess(
        command,
        int(process.returncode or 0),
        stdout.decode("utf-8", errors="strict"),
        stderr.decode("utf-8", errors="replace"),
    )


def run_durable_ats_fill_plan_specialist(
    connection: sqlite3.Connection,
    snapshot: Mapping[str, object],
    *,
    attempt_id: str,
    workflow_id: str,
) -> AtsFillPlanSpecialistRun:
    """Journal and execute one repair-only plan in a killable subprocess."""
    frozen = freeze_ats_fill_plan_snapshot(snapshot)
    snapshot_digest = specialist_snapshot_digest(frozen)
    snapshot_ref = f"ats-form:{snapshot_digest}"
    payload = {
        "snapshot_ref": snapshot_ref,
        "snapshot_sha256": snapshot_digest,
        "schema_version": ATS_FILL_PLAN_INPUT_SCHEMA_VERSION,
    }
    task_id, proposal_id = _ats_fill_plan_task_identity(
        attempt_id=attempt_id,
        snapshot_digest=snapshot_digest,
    )
    task_spec = TaskSpec(
        task_id=task_id,
        kind="ats-fill-plan-v1",
        objective="Produce a value-free repair plan from a launcher-owned form snapshot.",
        inputs={
            "phase": "prepare",
            "payload": payload,
            "snapshot_catalog": {snapshot_ref: frozen},
        },
        effect_class="read",
        authority_scope=production_specialist_spec("ats-fill-plan-v1").authority_scope,
        retry_budget=0,
        retry_categories=production_specialist_spec("ats-fill-plan-v1").retry_categories,
        cancellation_mode="killable_subprocess",
        partial_allowed=False,
        conflict_keys=(f"ats-snapshot:{snapshot_digest}",),
        idempotency_key=proposal_id,
    )
    entry = task_journal.register(
        connection,
        task_spec,
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        proposal_id=proposal_id,
    )
    base = {
        "task_id": task_id,
        "proposal_id": proposal_id,
        "snapshot_ref": snapshot_ref,
        "snapshot_sha256": snapshot_digest,
    }
    if entry.status == "completed" and isinstance(entry.result, Mapping):
        durable_output = entry.result.get("output")
        result = (
            durable_output.get("ats_fill_plan")
            if isinstance(durable_output, Mapping)
            else None
        )
        if not isinstance(result, dict):
            raise RuntimeError("completed ATS fill-plan task has no durable result")
        result = validate_ats_fill_plan_result(result, frozen)
        replay_key = uuid.uuid4().hex[:12]
        for suffix, event_type in (
            ("emitted", "agent.proposal.emitted"),
            ("executed", "agent.proposal.executed"),
        ):
            _append_feedback_event(
                connection,
                event_id=f"{task_id}:replay:{replay_key}:{suffix}",
                attempt_id=attempt_id,
                workflow_id=workflow_id,
                event_type=event_type,
                payload={**base, "status": "completed", "replay": True},
                phase="prepare",
            )
        telemetry = (
            {"event_type": "agent.proposal.emitted", **base, "replay": True},
            {
                "event_type": "agent.proposal.executed",
                **base,
                "status": "completed",
                "replay": True,
            },
        )
        return AtsFillPlanSpecialistRun(task_id, proposal_id, result, True, telemetry)

    owner_id = f"ats-fill-{uuid.uuid4()}"
    claimed = task_journal.claim(connection, task_id, owner_id, lease_seconds=10)
    if claimed is None:
        raise RuntimeError("ATS fill-plan task is already claimed")
    _append_feedback_event(
        connection,
        event_id=f"{task_id}:emitted",
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        event_type="agent.proposal.emitted",
        payload={**base, "replay": False},
        phase="prepare",
    )
    durable_spec = task_journal.load_spec(connection, task_id)
    durable_inputs = durable_spec.get("inputs")
    if not isinstance(durable_inputs, Mapping):
        raise TypeError("durable ATS fill-plan task has no inputs")
    durable_payload = durable_inputs.get("payload")
    durable_catalog = durable_inputs.get("snapshot_catalog")
    if not isinstance(durable_payload, Mapping) or not isinstance(
        durable_catalog, Mapping
    ):
        raise TypeError("durable ATS fill-plan inputs are invalid")
    durable_snapshot = durable_catalog.get(snapshot_ref)
    if not isinstance(durable_snapshot, Mapping):
        raise TypeError("durable ATS fill-plan snapshot is missing")
    request_text = _ats_fill_plan_subprocess_request(
        payload=durable_payload,
        snapshot_ref=snapshot_ref,
        snapshot=durable_snapshot,
    )
    specialist_spec = production_specialist_spec("ats-fill-plan-v1")
    try:
        completed = _run_bounded_subprocess(
            [sys.executable, "-m", "applypilot.apply.ats_fill_plan_worker"],
            input_text=request_text,
            timeout_seconds=specialist_spec.execution_budget_seconds,
            stdout_limit=specialist_spec.max_output_bytes,
            stderr_limit=4096,
            env=_ats_fill_plan_subprocess_environment(),
        )
        if completed.returncode != 0:
            raise RuntimeError("ATS fill-plan subprocess failed")
        parsed = json.loads(completed.stdout)
        result = validate_ats_fill_plan_result(parsed, frozen)
        task_journal.complete(
            connection,
            task_id,
            owner_id,
            TaskResult(
                task_id=task_id,
                status="completed",
                output={"ats_fill_plan": result},
                authority_scope=specialist_spec.authority_scope,
            ),
        )
        outcome_status = "completed"
    except subprocess.TimeoutExpired as exc:
        task_journal.fail(
            connection,
            task_id,
            owner_id,
            TaskResult(
                task_id=task_id,
                status="timed_out",
                failure_category="specialist_timeout",
            ),
        )
        _append_feedback_event(
            connection,
            event_id=f"{task_id}:executed",
            attempt_id=attempt_id,
            workflow_id=workflow_id,
            event_type="agent.proposal.executed",
            payload={**base, "status": "timed_out", "replay": False},
            phase="prepare",
        )
        raise RuntimeError("ATS fill-plan subprocess timed out") from exc
    except Exception as exc:
        task_journal.fail(
            connection,
            task_id,
            owner_id,
            TaskResult(
                task_id=task_id,
                status="failed",
                failure_category="specialist_failure",
                output={"error_type": type(exc).__name__},
            ),
        )
        _append_feedback_event(
            connection,
            event_id=f"{task_id}:executed",
            attempt_id=attempt_id,
            workflow_id=workflow_id,
            event_type="agent.proposal.executed",
            payload={**base, "status": "failed", "replay": False},
            phase="prepare",
        )
        raise RuntimeError("ATS fill-plan subprocess failed") from exc
    _append_feedback_event(
        connection,
        event_id=f"{task_id}:executed",
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        event_type="agent.proposal.executed",
        payload={**base, "status": outcome_status, "replay": False},
        phase="prepare",
    )
    telemetry = (
        {"event_type": "agent.proposal.emitted", **base, "replay": False},
        {
            "event_type": "agent.proposal.executed",
            **base,
            "status": outcome_status,
            "replay": False,
        },
    )
    return AtsFillPlanSpecialistRun(task_id, proposal_id, result, False, telemetry)


@dataclass(frozen=True, slots=True)
class SpecialistRun:
    specialist_id: str
    mode: str
    result: dict[str, object]
    enforced: bool
    telemetry: tuple[dict[str, object], ...]
    task_id: str | None = None
    proposal_id: str | None = None
    replay: bool = False


_PRODUCTION_SPECIALISTS: dict[str, Specialist] = {
    "material-readiness-v1": evaluate_material_readiness,
}

_MATERIAL_SPECIALIST_SPEC = ProductionSpecialistSpec(
    specialist_id="material-readiness-v1",
    phases=("preflight",),
    effect_class="read",
    input_schema_version="material-snapshot-v1",
    output_schema_version="material-readiness-v1",
    execution_budget_seconds=5,
    max_output_bytes=16 * 1024,
    authority_scope=READ_ONLY_SPECIALIST_AUTHORITY,
    read_only=True,
    capabilities=(),
    retry_categories=("specialist_timeout", "specialist_transient"),
    metadata={
        "deterministic": True,
        "prepare_only": True,
        "execution_budget_enforced": False,
    },
)
_PRODUCTION_SPECIALIST_SPECS["material-readiness-v1"] = _MATERIAL_SPECIALIST_SPEC


def run_context_specialist(
    name: str,
    snapshot: Mapping[str, object],
    *,
    mode: str = "shadow",
) -> SpecialistRun | None:
    """Run one bounded deterministic context specialist without effect authority."""
    normalized = normalize_specialist_mode(mode)
    if normalized == "off":
        return None
    spec = production_specialist_spec(name)
    if name in {"ats-fill-plan-v1", "material-readiness-v1"}:
        raise ValueError("specialist requires its dedicated validated dispatcher")
    copied = ensure_persistable(snapshot, path="$.specialist_input")
    assert isinstance(copied, dict)
    result: dict[str, object]
    if name == "provider-classifier-v1":
        url = str(copied.get("application_url") or copied.get("url") or "")[:2048]
        provider = detect_ats_site(url) if url else "unknown"
        result = {
            "state": "ready" if url else "insufficient",
            "ready": bool(url),
            "provider": provider,
            "summary": f"provider={provider}" if url else "provider unavailable: URL missing",
        }
    elif name == "application-facts-v1":
        facts = {
            key: copied[key]
            for key in ("title", "company", "location", "employment_type")
            if isinstance(copied.get(key), (str, bool, int, float))
        }
        result = {
            "state": "ready" if facts else "insufficient",
            "ready": bool(facts),
            "facts": facts,
            "summary": f"{len(facts)} bounded application facts",
        }
    elif name == "work-authorization-v1":
        facts = {
            key: copied[key]
            for key in (
                "legally_authorized_to_work",
                "requires_sponsorship",
                "require_sponsorship",
                "visa_status",
            )
            if isinstance(copied.get(key), (str, bool))
        }
        result = {
            "state": "ready" if facts else "insufficient",
            "ready": bool(facts),
            "facts": facts,
            "summary": (
                "bounded work-authorization facts available"
                if facts
                else "work-authorization facts unavailable"
            ),
        }
    elif name == "field-semantic-v1":
        semantics = copied.get("field_semantics")
        semantics = semantics if isinstance(semantics, Mapping) else {}
        bounded = {
            str(key)[:100]: str(value)[:100]
            for key, value in list(semantics.items())[:100]
            if str(key) and str(value)
        }
        result = {
            "state": "ready" if bounded else "insufficient",
            "ready": bool(bounded),
            "facts": bounded,
            "summary": f"{len(bounded)} field semantics",
        }
    elif name == "page-failure-v1":
        code = str(copied.get("failure_code") or copied.get("failure_category") or "")[:100]
        result = {
            "state": "ready" if code else "insufficient",
            "ready": bool(code),
            "facts": {"failure_code": code} if code else {},
            "summary": f"page failure={code}" if code else "page failure unavailable",
        }
    else:  # pragma: no cover - registry/spec lookup is the closed admission gate
        raise ValueError(f"specialist is not allowlisted: {name}")
    ensure_persistable(result, path="$.specialist_output")
    if len(_canonical_json_bytes(result)) > spec.max_output_bytes:
        raise ValueError("specialist output exceeds the configured byte limit")
    enforced = normalized == "required" and not bool(result["ready"])
    telemetry = (
        {"event_type": "agent.proposal.emitted", "specialist_id": name},
        {
            "event_type": "agent.proposal.executed",
            "specialist_id": name,
            "status": "completed",
        },
        {"event_type": "agent.proposal.consumed", "specialist_id": name},
    )
    return SpecialistRun(name, normalized, result, enforced, telemetry)


def production_specialist(name: str) -> Specialist:
    try:
        return _PRODUCTION_SPECIALISTS[name]
    except KeyError:
        raise ValueError(f"specialist is not allowlisted: {name}") from None


def run_system_specialist(
    name: str,
    job: Mapping[str, object],
    *,
    mode: str = "shadow",
    timeout_seconds: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> SpecialistRun | None:
    normalized = normalize_specialist_mode(mode)
    if normalized == "off":
        return None
    if _MATERIAL_SPECIALIST_SPEC.effect_class != "read":
        raise PermissionError("material specialist is not admitted as read-only")
    ensure_persistable(job, path="$.specialist_input")
    result = _run_deterministic_read_specialist(
        production_specialist(name),
        job,
        timeout_seconds=(
            _MATERIAL_SPECIALIST_SPEC.execution_budget_seconds
            if timeout_seconds is None
            else timeout_seconds
        ),
        cancelled=cancelled,
    )
    ensure_persistable(result, path="$.specialist_output")
    changed = normalized == "required" and not bool(result.get("ready"))
    telemetry = (
        {"event_type": "agent.proposal.emitted", "specialist_id": name},
        {"event_type": "agent.proposal.executed", "specialist_id": name, "status": "completed"},
        {"event_type": "agent.proposal.consumed", "specialist_id": name},
        {
            "event_type": "agent.proposal.changed_decision",
            "specialist_id": name,
            "changed": changed,
        },
    )
    return SpecialistRun(name, normalized, result, changed, telemetry)


def _append_feedback_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    attempt_id: str,
    workflow_id: str,
    event_type: str,
    payload: Mapping[str, object],
    phase: str = "preflight",
) -> None:
    from applypilot.database import append_agent_event

    append_agent_event(
        ApplicationEvent(
            event_id=event_id,
            attempt_id=attempt_id,
            run_id=workflow_id,
            phase=phase,
            actor="system-specialist",
            event_type=event_type,
            payload=payload,
            idempotency_key=event_id,
        ),
        conn=connection,
    )


def run_durable_material_specialist(
    connection: sqlite3.Connection,
    job: Mapping[str, object],
    *,
    mode: str,
    attempt_id: str,
    workflow_id: str,
    cancelled: Callable[[], bool] | None = None,
    timeout_seconds: float | None = None,
) -> SpecialistRun | None:
    """Journal, execute, replay, and persist feedback for material readiness."""
    normalized = normalize_specialist_mode(mode)
    if normalized == "off":
        return None
    ensure_persistable(job, path="$.specialist_input")
    identity = material_snapshot_identity(job)
    version = "material-readiness-v1"
    fingerprint = identity["job_fingerprint"]
    snapshot = identity["material_snapshot_digest"]
    proposal_id = f"{version}:{fingerprint[:12]}:{snapshot[:12]}"
    task_id = f"task:{proposal_id}"
    spec = TaskSpec(
        task_id=task_id,
        kind=version,
        objective="Evaluate byte-bound submission material readiness.",
        inputs={
            "specialist_version": version,
            "job_fingerprint": fingerprint,
            "material_snapshot_digest": snapshot,
        },
        effect_class="read",
        authority_scope=_MATERIAL_SPECIALIST_SPEC.authority_scope,
        retry_categories=_MATERIAL_SPECIALIST_SPEC.retry_categories,
        cancellation_mode="cooperative",
        partial_allowed=False,
        conflict_keys=(f"material-snapshot:{snapshot}",),
        idempotency_key=proposal_id,
    )
    entry = task_journal.register(
        connection,
        spec,
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        proposal_id=proposal_id,
    )
    base_payload = {"task_id": task_id, "proposal_id": proposal_id}
    if entry.status == "completed" and isinstance(entry.result, Mapping):
        output = entry.result.get("output")
        result = output.get("material_readiness") if isinstance(output, Mapping) else None
        if not isinstance(result, dict):
            raise RuntimeError("completed material task has no durable result")
        changed = normalized == "required" and result.get("state") != "ready"
        replay_key = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{attempt_id}:{normalized}",
        ).hex[:12]
        replay_events = (
            ("emitted", "agent.proposal.emitted", {"before_decision": "unchecked"}),
            ("executed", "agent.proposal.executed", {"status": "completed"}),
            ("consumed", "agent.proposal.consumed", {"state": result.get("state")}),
            (
                "changed",
                "agent.proposal.changed_decision",
                {
                    "before_decision": "unchecked",
                    "after_decision": result.get("state"),
                    "changed": changed,
                },
            ),
        )
        for suffix, event_type, payload in replay_events:
            _append_feedback_event(
                connection,
                event_id=f"{task_id}:replay:{replay_key}:{suffix}",
                attempt_id=attempt_id,
                workflow_id=workflow_id,
                event_type=event_type,
                payload={**base_payload, **payload, "replay": True},
            )
        telemetry = (
            {"event_type": "agent.proposal.emitted", **base_payload, "replay": True},
            {"event_type": "agent.proposal.executed", **base_payload, "replay": True},
            {"event_type": "agent.proposal.consumed", **base_payload, "replay": True},
            {
                "event_type": "agent.proposal.changed_decision",
                **base_payload,
                "replay": True,
                "changed": changed,
            },
        )
        return SpecialistRun(version, normalized, result, changed, telemetry, task_id, proposal_id, True)

    owner_id = f"preflight-{uuid.uuid4()}"
    claimed = task_journal.claim(connection, task_id, owner_id, lease_seconds=120)
    if claimed is None:
        raise RuntimeError("material specialist task is already claimed")
    _append_feedback_event(
        connection,
        event_id=f"{task_id}:emitted",
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        event_type="agent.proposal.emitted",
        payload={**base_payload, "replay": False, "before_decision": "unchecked"},
    )
    try:
        result = _run_deterministic_read_specialist(
            production_specialist(version),
            job,
            timeout_seconds=(
                _MATERIAL_SPECIALIST_SPEC.execution_budget_seconds
                if timeout_seconds is None
                else timeout_seconds
            ),
            cancelled=cancelled,
        )
        ensure_persistable(result, path="$.specialist_output")
        durable_result = TaskResult(
            task_id=task_id,
            status="completed",
            output={"material_readiness": result},
            authority_scope=_MATERIAL_SPECIALIST_SPEC.authority_scope,
        )
        task_journal.complete(connection, task_id, owner_id, durable_result)
    except Exception as exc:
        task_journal.fail(
            connection,
            task_id,
            owner_id,
            TaskResult(
                task_id=task_id,
                status="failed",
                output={"error_type": type(exc).__name__},
                failure_category="specialist_failure",
            ),
        )
        raise
    changed = normalized == "required" and result.get("state") != "ready"
    feedback = (
        ("executed", "agent.proposal.executed", {"status": "completed", "replay": False}),
        ("consumed", "agent.proposal.consumed", {"state": result.get("state"), "replay": False}),
        (
            "changed",
            "agent.proposal.changed_decision",
            {
                "before_decision": "unchecked",
                "after_decision": result.get("state"),
                "changed": changed,
                "replay": False,
            },
        ),
    )
    for suffix, event_type, payload in feedback:
        _append_feedback_event(
            connection,
            event_id=f"{task_id}:{suffix}",
            attempt_id=attempt_id,
            workflow_id=workflow_id,
            event_type=event_type,
            payload={**base_payload, **payload},
        )
    telemetry = (
        {"event_type": "agent.proposal.emitted", **base_payload, "replay": False},
        {"event_type": "agent.proposal.executed", **base_payload, "replay": False},
        {"event_type": "agent.proposal.consumed", **base_payload, "replay": False},
        {
            "event_type": "agent.proposal.changed_decision",
            **base_payload,
            "replay": False,
            "changed": changed,
        },
    )
    return SpecialistRun(version, normalized, result, changed, telemetry, task_id, proposal_id, False)
