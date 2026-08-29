"""Minimal stdio MCP server for one structured Agent-turn report.

The browser tools perform application work.  This server only records the
Agent's final structured observation into its launcher-assigned worker file;
it cannot mutate application state or submit anything.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from applypilot.apply.contracts import (
    MAX_AGENT_PROPOSALS,
    MAX_AGENT_REPORT_BYTES,
    MAX_PROPOSAL_DEPENDENCIES,
    agent_turn_result_from_mapping,
    contract_json,
)

REPORT_SCHEMA_VERSION = "1"
REPORT_PATH_ENV = "APPLYPILOT_AGENT_REPORT_PATH"
RUN_ID_ENV = "APPLYPILOT_AGENT_RUN_ID"


def _result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32000, "message": message},
    }


def _report_tool() -> dict[str, object]:
    return {
        "name": "report_agent_turn",
        "description": (
            "Record the final structured result for the active ApplyPilot Agent turn. "
            "This is reporting only and never changes or submits the application."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "minLength": 1, "maxLength": 200},
                "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
                "observations": {"type": "object"},
                "proposals": {
                    "type": "array",
                    "maxItems": MAX_AGENT_PROPOSALS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "proposal_id": {"type": "string", "maxLength": 200},
                            "kind": {"type": "string", "minLength": 1, "maxLength": 100},
                            "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
                            "payload": {"type": "object"},
                            "depends_on": {
                                "type": "array",
                                "maxItems": MAX_PROPOSAL_DEPENDENCIES,
                                "items": {"type": "string", "maxLength": 200},
                            },
                            "concurrency_mode": {"type": "string", "maxLength": 100},
                            "concurrency_key": {"type": "string", "maxLength": 200},
                            "priority": {"type": "integer"},
                        },
                        "required": ["kind", "summary"],
                        "additionalProperties": False,
                    },
                },
                "requested_human_input": {"type": "string", "maxLength": 4000},
            },
            "required": ["status", "summary"],
            "additionalProperties": False,
        },
    }


def _write_report(arguments: dict[str, object]) -> dict[str, object]:
    run_id = os.environ.get(RUN_ID_ENV, "").strip()
    raw_path = os.environ.get(REPORT_PATH_ENV, "").strip()
    if not run_id or not raw_path:
        raise ValueError("structured reporting is not configured for this Agent turn")
    if len(str(arguments.get("status") or "")) > 200:
        raise ValueError("status is too long")
    if len(str(arguments.get("summary") or "")) > 2000:
        raise ValueError("summary is too long")
    if len(str(arguments.get("requested_human_input") or "")) > 4000:
        raise ValueError("requested_human_input is too long")
    proposals = arguments.get("proposals") or ()
    if isinstance(proposals, (list, tuple)) and len(proposals) > MAX_AGENT_PROPOSALS:
        raise ValueError(f"a report may contain at most {MAX_AGENT_PROPOSALS} proposals")
    if isinstance(proposals, (list, tuple)):
        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            bounded_fields = {
                "proposal_id": 200,
                "kind": 100,
                "summary": 1000,
                "concurrency_mode": 100,
                "concurrency_key": 200,
            }
            for field_name, maximum in bounded_fields.items():
                value = proposal.get(field_name)
                if value is not None and len(str(value)) > maximum:
                    raise ValueError(f"proposal {field_name} is too long")
            dependencies = proposal.get("depends_on") or ()
            if isinstance(dependencies, (list, tuple)):
                if len(dependencies) > MAX_PROPOSAL_DEPENDENCIES:
                    raise ValueError(
                        "a proposal may have at most "
                        f"{MAX_PROPOSAL_DEPENDENCIES} dependencies"
                    )
                if any(len(str(value)) > 200 for value in dependencies):
                    raise ValueError("proposal dependency id is too long")
    report = agent_turn_result_from_mapping(arguments, expected_run_id=run_id)
    report_payload = contract_json(report)
    # The launcher owns timing. Omitting a call-time timestamp also makes an
    # exact tool retry byte-for-byte idempotent.
    report_payload.pop("completed_at", None)
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        **report_payload,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_AGENT_REPORT_BYTES:
        raise ValueError("structured Agent report is too large")
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == serialized:
            return {"recorded": False, "run_id": run_id, "status": report.status}
        raise ValueError("a different report is already recorded for this Agent turn")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(serialized, encoding="utf-8")
    os.replace(temporary, path)
    return {"recorded": True, "run_id": run_id, "status": report.status}


def _handle(message: dict[str, object]) -> dict[str, object] | None:
    method = str(message.get("method") or "")
    request_id = message.get("id")
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "applypilot-agent-report", "version": REPORT_SCHEMA_VERSION},
            },
        )
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return _result(request_id, {"tools": [_report_tool()]})
    if method == "tools/call":
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if params.get("name") != "report_agent_turn":
            return _error(request_id, "Unknown tool")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            recorded = _write_report(arguments)
        except (OSError, TypeError, ValueError) as exc:
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(recorded)}],
                "structuredContent": recorded,
                "isError": False,
            },
        )
    return _error(request_id, f"Unsupported method: {method}")


def main() -> int:
    for line in sys.stdin:
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
