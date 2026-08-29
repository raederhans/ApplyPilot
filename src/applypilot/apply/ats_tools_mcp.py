"""Read/proposal-only ATS tools exposed to one application Agent turn.

The launcher stages a bounded context file containing semantic fact names, not
their values.  These tools never connect to a browser, write the application
ledger, or authorize a submission.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from applypilot.apply.answer_resolution import AnswerRequest, resolve_answer
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
MAX_RESOLUTION_ALIASES = 20


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
                    "confirmed_fact": {
                        "oneOf": [
                            {"type": "string", "maxLength": 500},
                            {"type": "number"},
                            {"type": "boolean"},
                            {
                                "type": "object",
                                "properties": {
                                    "value": {"type": ["string", "number", "boolean", "null"]},
                                    "start": {"type": "string", "maxLength": 32},
                                    "end": {"type": "string", "maxLength": 32},
                                    "aliases": {
                                        "type": "array",
                                        "maxItems": MAX_RESOLUTION_ALIASES,
                                        "items": {"type": "string", "maxLength": 120},
                                    },
                                },
                                "additionalProperties": False,
                            },
                            {"type": "null"},
                        ]
                    },
                    "aliases": {
                        "type": "array",
                        "maxItems": MAX_RESOLUTION_ALIASES,
                        "items": {"type": "string", "maxLength": 120},
                    },
                    "required": {"type": "boolean"},
                    "direct_impact": {"type": "boolean"},
                    "declaration": {"type": "boolean"},
                    "can_explain": {"type": "boolean"},
                    "preference": {"type": "boolean"},
                },
                "required": ["field_semantic", "confirmed_fact"],
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


def _load_context() -> dict[str, object]:
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


def _bounded_fact(value: object) -> object | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if not isinstance(value, Mapping):
        raise TypeError("confirmed_fact must be a scalar or supported object")
    allowed = {"value", "start", "end", "aliases"}
    if set(value) - allowed:
        raise ValueError("confirmed_fact contains unsupported properties")
    bounded: dict[str, object] = {}
    for name in ("value", "start", "end"):
        item = value.get(name)
        if item is not None:
            if not isinstance(item, (str, bool, int, float)):
                raise TypeError(f"confirmed_fact.{name} must be a scalar")
            bounded[name] = item[:500] if isinstance(item, str) else item
    if "aliases" in value:
        bounded["aliases"] = _bounded_strings(
            value.get("aliases"), limit=MAX_RESOLUTION_ALIASES, item_limit=120
        )
    return bounded


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
        semantic = str(arguments.get("field_semantic") or "").strip()[:240]
        if not semantic:
            raise ValueError("field_semantic is required")
        resolution = resolve_answer(
            AnswerRequest(
                field_semantic=semantic,
                options=_bounded_strings(
                    arguments.get("options"), limit=MAX_RESOLUTION_OPTIONS, item_limit=120
                ),
                confirmed_fact=_bounded_fact(arguments.get("confirmed_fact")),
                aliases=_bounded_strings(
                    arguments.get("aliases"), limit=MAX_RESOLUTION_ALIASES, item_limit=120
                ),
                required=bool(arguments.get("required", False)),
                direct_impact=bool(arguments.get("direct_impact", False)),
                declaration=bool(arguments.get("declaration", False)),
                can_explain=bool(arguments.get("can_explain", False)),
                preference=bool(arguments.get("preference", False)),
            )
        )
        return {
            "relation": resolution.relation,
            "action": resolution.action,
            "selected_option": resolution.selected_option,
            "value": resolution.value,
            "confidence": resolution.confidence,
            "audit": dict(resolution.audit),
            "side_effect": "proposal-only",
        }
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
