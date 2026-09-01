"""Read/proposal-only ATS tools exposed to one application Agent turn.

The launcher stages a bounded context file containing semantic fact names, not
their values.  These tools never connect to a browser, write the application
ledger, or authorize a submission.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from applypilot import config
from applypilot.apply.answer_policy import field_risk
from applypilot.apply.answer_provenance import (
    envelope_binding,
    field_key_hash,
    selected_option_digest,
)
from applypilot.apply.answer_resolution import AnswerRequest, resolve_answer
from applypilot.apply.application_facts import (
    FactResolution,
    current_profile_facts,
    resolve_application_fact_ref,
)
from applypilot.apply.ats import (
    ATS_SCHEMA_VERSION,
    adapter_prompt_context,
    adapter_prompt_guidance,
    build_form_ir,
    detect_ats_site,
    propose_fill_plan,
)
from applypilot.apply.workday_state import (
    evaluate_page_progress,
    observation_from_mapping,
)

ATS_CONTEXT_PATH_ENV = "APPLYPILOT_ATS_CONTEXT_PATH"
MAX_CONTEXT_BYTES = 128 * 1024
MAX_REQUEST_BYTES = 512 * 1024
MAX_FACT_NAMES = 200
MAX_RESOLUTION_OPTIONS = 50


def _result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32000, "message": message},
    }


def _content(payload: object, *, is_error: bool = False) -> dict[str, object]:
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    str(payload)
                    if is_error
                    else json.dumps(payload, ensure_ascii=False, sort_keys=True)
                ),
            }
        ],
        "isError": is_error,
        **({} if is_error else {"structuredContent": payload}),
    }


def _tools() -> list[dict[str, object]]:
    structural_field = {
        "type": "object",
        "properties": {
            "field_key": {"type": "string", "maxLength": 160},
            "id": {"type": "string", "maxLength": 160},
            "name": {"type": "string", "maxLength": 160},
            "label": {"type": "string", "maxLength": 240},
            "aria_label": {"type": "string", "maxLength": 240},
            "autocomplete": {"type": "string", "maxLength": 120},
            "placeholder": {"type": "string", "maxLength": 240},
            "control": {"type": "string", "maxLength": 80},
            "type": {"type": "string", "maxLength": 80},
            "tag": {"type": "string", "maxLength": 80},
            "required": {"type": "boolean"},
            "disabled": {"type": "boolean"},
            "readonly": {"type": "boolean"},
            "options": {
                "type": "array",
                "maxItems": 100,
                "items": {"type": "string", "maxLength": 120},
            },
        },
        "additionalProperties": False,
    }
    workday_observation = {
        "type": "object",
        "properties": {
            "page_kind": {"type": "string"},
            "step_index": {"type": ["integer", "null"]},
            "step_count": {"type": ["integer", "null"]},
            "visible_controls": {
                "type": "array",
                "maxItems": 128,
                "items": {"type": "string"},
            },
            "required_count": {"type": "integer"},
            "invalid_count": {"type": "integer"},
            "has_next": {"type": "boolean"},
            "has_review": {"type": "boolean"},
            "has_submit": {"type": "boolean"},
            "has_confirmation": {"type": "boolean"},
            "has_manual_gate": {"type": "boolean"},
            "repairable_validation": {"type": "boolean"},
        },
        "required": ["page_kind"],
        "additionalProperties": False,
    }
    return [
        {
            "name": "detect_ats",
            "description": "Detect an ATS adapter from a URL without interacting with the page.",
            "inputSchema": {
                "type": "object",
                "properties": {"url": {"type": "string", "maxLength": 4000}},
                "required": ["url"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_application_context",
            "description": (
                "Read launcher-staged adapter guidance and available semantic fact names. "
                "No applicant values are returned."
            ),
            "inputSchema": {"type": "object", "additionalProperties": False},
        },
        {
            "name": "build_fill_plan",
            "description": (
                "Build a value-free semantic fill proposal from already-observed field metadata. "
                "This does not fill controls or authorize actions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "maxLength": 4000},
                    "fields": {
                        "type": "array",
                        "maxItems": 200,
                        "items": structural_field,
                    },
                    "available_facts": {
                        "type": "array",
                        "maxItems": MAX_FACT_NAMES,
                        "items": {"type": "string", "maxLength": 120},
                    },
                },
                "required": ["fields"],
                "additionalProperties": False,
            },
        },
        {
            "name": "resolve_answer",
            "description": (
                "Propose and audit the nearest truthful answer for one observed field. "
                "It never interacts with a browser or authorizes submission, and refuses "
                "credential, security-code, and identity-number fields."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "field_semantic": {"type": "string", "maxLength": 240},
                    "options": {
                        "type": "array",
                        "maxItems": MAX_RESOLUTION_OPTIONS,
                        "items": {"type": "string", "maxLength": 120},
                    },
                    "required": {"type": "boolean"},
                    "direct_impact": {"type": "boolean"},
                    "declaration": {"type": "boolean"},
                    "can_explain": {"type": "boolean"},
                    "preference": {"type": "boolean"},
                },
                "required": ["field_semantic"],
                "additionalProperties": False,
            },
        },
        {
            "name": "build_answer_mapping",
            "description": (
                "Build one v2 provenance mapping from the launcher-staged observed form and a "
                "trusted fact reference. This is proposal-only and is not a general hash tool."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "field_key": {"type": "string", "maxLength": 160},
                    "control": {
                        "enum": [
                            "select", "radio", "text", "textarea", "email", "tel",
                            "number", "date", "combobox"
                        ]
                    },
                    "visible_options": {
                        "type": "array",
                        "maxItems": MAX_RESOLUTION_OPTIONS,
                        "items": {"type": "string", "maxLength": 120},
                    },
                    "selected_option": {"type": "string", "maxLength": 120},
                    "selected_value": {"type": "string", "maxLength": 240},
                    "fact_ref": {"type": "string", "maxLength": 160},
                },
                "required": [
                    "field_key",
                    "control",
                    "fact_ref",
                ],
                "additionalProperties": False,
            },
        },
        {
            "name": "evaluate_workday_progress",
            "description": (
                "Evaluate one bounded Workday page transition. It returns guidance only and "
                "always forbids a runtime switch after Submit starts."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "previous_signature": {"type": ["string", "null"], "maxLength": 64},
                    "observation": workday_observation,
                    "repair_used": {"type": "boolean"},
                    "submit_started": {"type": "boolean"},
                },
                "required": ["observation", "repair_used"],
                "additionalProperties": False,
            },
        },
    ]


def _read_context_payload() -> dict[str, object]:
    raw_path = os.environ.get(ATS_CONTEXT_PATH_ENV, "").strip()
    if not raw_path:
        raise ValueError("ATS application context is not configured for this Agent turn")
    path = Path(raw_path)
    if path.stat().st_size > MAX_CONTEXT_BYTES:
        raise ValueError("ATS application context is too large")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("ATS application context must be a JSON object")
    return payload


def _load_context() -> dict[str, object]:
    payload = _read_context_payload()
    target = str(payload.get("target_url") or "")
    parsed = urlsplit(target)
    public_target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    guidance = payload.get("guidance")
    result: dict[str, object] = {
        "schema_version": str(payload.get("schema_version") or ATS_SCHEMA_VERSION),
        "adapter": str(payload.get("adapter") or "generic")[:80],
        "target_url": public_target,
        "guidance": list(_bounded_strings(guidance, limit=20, item_limit=240)),
        "available_fact_names": _fact_names(payload.get("available_fact_names")),
        "side_effect": "proposal-only",
    }
    provenance = payload.get("answer_provenance")
    if isinstance(provenance, Mapping):
        result["answer_provenance"] = dict(provenance)
    observed_form = payload.get("observed_form")
    if isinstance(observed_form, Mapping):
        result["observed_form"] = dict(observed_form)
    available_fact_refs = payload.get("available_fact_refs")
    if isinstance(available_fact_refs, list):
        result["available_fact_refs"] = [
            dict(item) for item in available_fact_refs[:MAX_FACT_NAMES] if isinstance(item, Mapping)
        ]
    return result


def _fact_names(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip()[:120] for item in value[:MAX_FACT_NAMES] if str(item).strip()]


def _bounded_strings(value: object, *, limit: int, item_limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    if len(value) > limit:
        raise ValueError(f"array exceeds maximum of {limit} items")
    return tuple(str(item).strip()[:item_limit] for item in value if str(item).strip())


def _trusted_fact_resolution(
    fact_ref: str,
    *,
    semantic: str,
    direct_impact: bool,
    declaration: bool,
) -> FactResolution:
    context = _read_context_payload()
    raw_scopes = context.get("_trusted_fact_scopes")
    scopes = {
        str(item).strip()
        for item in raw_scopes
        if str(item).strip()
    } if isinstance(raw_scopes, list) else set()
    if not scopes:
        return FactResolution("out_of_scope", "", reason="trusted_host_fact_scope_missing")
    facts = current_profile_facts(config.load_profile())
    referenced = [fact for fact in facts if fact.fact_ref == fact_ref]
    if len(referenced) != 1 or referenced[0].scope not in scopes:
        return FactResolution("out_of_scope", "", reason="trusted_host_fact_scope_missing")
    risk = field_risk(
        semantic,
        direct_impact=direct_impact,
        declaration=declaration,
    )
    return resolve_application_fact_ref(
        facts,
        fact_ref=fact_ref,
        scope=str(referenced[0].scope),
        minimum_sensitivity=risk,
    )


def _options_digest(options: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(
            options,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _normalized_value(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _build_answer_mapping(arguments: Mapping[str, object]) -> dict[str, object]:
    allowed = {
        "field_key", "control", "visible_options", "selected_option", "selected_value", "fact_ref"
    }
    if set(arguments) - allowed:
        raise ValueError("build_answer_mapping arguments do not match the public schema")
    context = _read_context_payload()
    provenance = context.get("answer_provenance")
    observed = context.get("observed_form")
    if not isinstance(provenance, Mapping) or not isinstance(observed, Mapping):
        raise TypeError("launcher-staged provenance and observed form are required")
    snapshot_digest = str(provenance.get("expected_snapshot_digest") or "")
    binding_seed = str(provenance.get("opaque_binding_seed") or "")
    adapter = str(provenance.get("adapter") or "")
    adapter_version = str(provenance.get("adapter_version") or "")
    if not all(re.fullmatch(r"[0-9a-f]{64}", item) for item in (snapshot_digest, binding_seed)):
        raise ValueError("launcher-staged provenance binding is incomplete")
    fields = observed.get("fields")
    if not isinstance(fields, list):
        raise TypeError("launcher-staged observed fields are missing")
    field_key = str(arguments.get("field_key") or "").strip()
    control = str(arguments.get("control") or "").strip().casefold()
    matches = [
        item
        for item in fields
        if isinstance(item, Mapping)
        and str(item.get("field_key") or "") == field_key
        and str(item.get("control") or "").casefold() == control
    ]
    if len(matches) != 1:
        raise ValueError("field_key/control does not uniquely match the staged form")
    field = matches[0]
    supported = {"select", "radio", "text", "textarea", "email", "tel", "number", "date", "combobox"}
    if control not in supported or field.get("protected_identifier") is True:
        raise ValueError("staged control is unsupported or protected")
    visible_options = list(
        _bounded_strings(arguments.get("visible_options"), limit=MAX_RESOLUTION_OPTIONS, item_limit=120)
    )
    option_control = control in {"select", "radio"} or (control == "combobox" and bool(visible_options))
    if option_control:
        if field.get("options_source_truncated") is not False:
            raise ValueError("launcher-staged option set is truncated")
        if field.get("options_source_count") != len(visible_options):
            raise ValueError("visible option count does not match the launcher-staged option set")
        if _options_digest(visible_options) != field.get("options_sha256"):
            raise ValueError("visible options do not match the launcher-staged option set")
    elif visible_options:
        raise ValueError("free-text staged controls cannot accept caller option sets")
    semantic = str(field.get("semantic") or "unknown")
    risk = str(field.get("risk") or "")
    if risk not in {"low", "medium", "high"}:
        raise ValueError("launcher-staged field risk is missing")
    fact_ref = str(arguments.get("fact_ref") or "").strip()
    resolution = _trusted_fact_resolution(
        fact_ref,
        semantic=semantic,
        direct_impact=risk in {"medium", "high"},
        declaration=risk == "high",
    )
    if option_control:
        if "selected_value" in arguments:
            raise ValueError("option controls require selected_option")
        resolved = resolve_answer(
            AnswerRequest(
                field_semantic=semantic,
                options=tuple(visible_options),
                fact_resolution=resolution,
                required=field.get("required") is True,
                direct_impact=risk in {"medium", "high"},
                declaration=risk == "high",
                adapter=adapter,
                adapter_version=adapter_version,
            )
        )
        selected = str(arguments.get("selected_option") or "").strip()
        if resolved.selected_option is None or resolved.selected_option != selected:
            raise ValueError("selected option is not reproduced by the trusted fact resolver")
    else:
        if "selected_option" in arguments:
            raise ValueError("free-text controls require selected_value")
        selected = str(arguments.get("selected_value") or "").strip()
        if not resolution.production_ready or _normalized_value(resolution.value) != _normalized_value(selected):
            raise ValueError("selected value is not reproduced by the trusted fact resolver")
    mapping = {
        "field_key_hash": field_key_hash(
            adapter=adapter,
            adapter_version=adapter_version,
            control=control,
            field_key=field_key,
        ),
        "semantic": semantic,
        "risk": risk,
        "selected_option_digest": selected_option_digest(selected),
        "fact_ref": fact_ref,
    }
    binding = {"opaque_binding_seed": binding_seed}
    result = {
        "schema_version": "2",
        "adapter": adapter,
        "adapter_version": adapter_version,
        "opaque_binding": envelope_binding(binding, snapshot_digest),
        "snapshot_digest": snapshot_digest,
        "mappings": [mapping],
        "side_effect": "proposal-only",
        "authority": "none",
    }
    if option_control:
        result["selected_option"] = selected
    return result


def _call_tool(name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    if name == "detect_ats":
        url = str(arguments.get("url") or "")
        return {
            "schema_version": ATS_SCHEMA_VERSION,
            "adapter": detect_ats_site(url),
            "guidance": list(adapter_prompt_guidance(url)),
        }
    if name == "get_application_context":
        return _load_context()
    if name == "build_fill_plan":
        context = _load_context()
        url = str(arguments.get("url") or context.get("target_url") or "")
        raw_fields = arguments.get("fields")
        if not isinstance(raw_fields, list):
            raise TypeError("fields must be an array")
        fields = [item for item in raw_fields if isinstance(item, Mapping)]
        allowed_facts = set(_fact_names(context.get("available_fact_names")))
        requested_facts = set(_fact_names(arguments.get("available_facts")))
        facts = allowed_facts if not requested_facts else allowed_facts.intersection(requested_facts)
        form = build_form_ir(url, fields)
        plan = propose_fill_plan(form, facts)
        return adapter_prompt_context(form, plan)
    if name == "resolve_answer":
        allowed_arguments = {
            "field_semantic",
            "options",
            "required",
            "direct_impact",
            "declaration",
            "can_explain",
            "preference",
        }
        if set(arguments) - allowed_arguments:
            raise ValueError("resolve_answer arguments do not match the public schema")
        semantic = str(arguments.get("field_semantic") or "").strip()[:240]
        if not semantic:
            raise ValueError("field_semantic is required")
        direct_impact = bool(arguments.get("direct_impact", False))
        declaration = bool(arguments.get("declaration", False))
        resolution = resolve_answer(
            AnswerRequest(
                field_semantic=semantic,
                options=_bounded_strings(
                    arguments.get("options"), limit=MAX_RESOLUTION_OPTIONS, item_limit=120
                ),
                required=bool(arguments.get("required", False)),
                direct_impact=direct_impact,
                declaration=declaration,
                can_explain=bool(arguments.get("can_explain", False)),
                preference=bool(arguments.get("preference", False)),
            )
        )
        return {
            "relation": resolution.relation,
            "action": resolution.action,
            "selected_option": resolution.selected_option,
            "confidence": resolution.confidence,
            "audit": dict(resolution.audit),
            "side_effect": "proposal-only",
        }
    if name == "build_answer_mapping":
        return _build_answer_mapping(arguments)
    if name == "evaluate_workday_progress":
        raw_observation = arguments.get("observation")
        if not isinstance(raw_observation, Mapping):
            raise TypeError("observation must be an object")
        observation = observation_from_mapping(raw_observation)
        decision = evaluate_page_progress(
            str(arguments["previous_signature"])
            if arguments.get("previous_signature")
            else None,
            observation,
            repair_used=bool(arguments.get("repair_used", False)),
            submit_started=bool(arguments.get("submit_started", False)),
        )
        return {
            "action": decision.action.value,
            "state": decision.state.value,
            "signature": decision.signature,
            "repeated": decision.repeated,
            "repair_used": decision.repair_used,
            "runtime_switch_allowed": decision.runtime_switch_allowed,
        }
    raise ValueError("Unknown tool")


def _handle(message: dict[str, object]) -> dict[str, object] | None:
    method = str(message.get("method") or "")
    request_id = message.get("id")
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "applypilot-ats-tools", "version": ATS_SCHEMA_VERSION},
            },
        )
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return _result(request_id, {"tools": _tools()})
    if method == "tools/call":
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            payload = _call_tool(name, arguments)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return _result(request_id, _content(str(exc), is_error=True))
        return _result(request_id, _content(payload))
    return _error(request_id, f"Unsupported method: {method}")


def main() -> int:
    for line in sys.stdin:
        if len(line.encode("utf-8")) > MAX_REQUEST_BYTES:
            response = _error(None, "Request is too large")
        else:
            try:
                message = json.loads(line)
                response = _handle(message) if isinstance(message, dict) else None
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                response = _error(None, f"Invalid request: {exc}")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
