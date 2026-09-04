"""Minimal stdio MCP surface for the password-safe ATS credential relay."""

from __future__ import annotations

import json
import os
import sys

from applypilot.apply.capabilities import CapabilityRegistry
from applypilot.apply.contracts import ToolSpec
from applypilot.apply.credential_relay import (
    CredentialRelayError,
    _credential_path,
    _decrypt_fin,
    _decrypt_password,
    _fill_fields,
    _fill_protected_identifier,
    _identity_credential_path,
    _read_identity_record,
    _read_record,
)
from applypilot.apply.tool_broker import ToolBroker, ToolSurface

TOOL_BROKER_MODE_ENV = "APPLYPILOT_TOOL_BROKER_MODE"


def _result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32000, "message": message},
    }


def _tool_specs() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="fill_ats_credentials",
            description=(
                "Fill visible credential fields only in the bound application tab. "
                "Never submits and never returns the password."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "enum": ["email", "password", "both"],
                    }
                },
                "required": ["field"],
                "additionalProperties": False,
            },
            phases=("prepare", "submit"),
            effect_class="write",
            idempotency="conditional",
            authority="credential_relay",
            sensitivity="high",
            namespace="credential",
            concurrency_mode="serial_per_page",
        ),
        ToolSpec(
            name="fill_protected_identifier",
            description=(
                "Fill one required FIN/NRIC field in the bound application tab "
                "from a local DPAPI record. Never returns the identifier and never submits."
            ),
            input_schema={
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": ["fin"]}},
                "required": ["kind"],
                "additionalProperties": False,
            },
            phases=("prepare", "submit"),
            effect_class="write",
            idempotency="conditional",
            authority="protected_identifier_relay",
            sensitivity="high",
            namespace="credential",
            concurrency_mode="serial_per_page",
        ),
    )


def _mcp_tool(spec: ToolSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "description": spec.description,
        "inputSchema": dict(spec.input_schema),
    }


def _broker_surface() -> tuple[ToolBroker, ToolSurface]:
    broker = ToolBroker(
        CapabilityRegistry(_tool_specs()),
        mode=os.environ.get(TOOL_BROKER_MODE_ENV, "shadow"),
    )
    surface = broker.compile_surface(phase="prepare")
    return broker, surface


def _handle(message: dict[str, object]) -> dict[str, object] | None:
    method = str(message.get("method") or "")
    request_id = message.get("id")
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "applypilot-credential-relay", "version": "1"},
            },
        )
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        _broker, surface = _broker_surface()
        return _result(
            request_id,
            {"tools": [_mcp_tool(spec) for spec in surface.registry.values()]},
        )
    if method == "tools/call":
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        tool_name = str(params.get("name") or "")
        if tool_name not in {"fill_ats_credentials", "fill_protected_identifier"}:
            return _error(request_id, "Unknown tool")
        broker, _surface = _broker_surface()
        if not broker.admit_call(tool_name):
            decision = broker.classify_call(tool_name)
            return _result(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"tool broker denied {tool_name}: {decision.reason}",
                        }
                    ],
                    "isError": True,
                },
            )
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        if tool_name == "fill_protected_identifier":
            kind = str(arguments.get("kind") or "")
            if kind != "fin":
                return _error(request_id, "kind must be fin")
            if os.environ.get("APPLYPILOT_IDENTITY_RELAY_AUTHORIZED", "").strip() != "1":
                return _result(
                    request_id,
                    {
                        "content": [
                            {"type": "text", "text": "Protected-identity relay is not authorized."}
                        ],
                        "isError": True,
                    },
                )
            fin = ""
            try:
                path = _identity_credential_path().resolve()
                _read_identity_record(path)
                fin = _decrypt_fin(path)
                port = int(os.environ.get("APPLYPILOT_CDP_PORT", "9222"))
                outcome = _fill_protected_identifier(port, kind, fin)
            except (CredentialRelayError, OSError, ValueError) as exc:
                return _result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                )
            finally:
                fin = ""
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(outcome)}],
                    "isError": False,
                },
            )

        field = str(arguments.get("field") or "")
        if field not in {"email", "password", "both"}:
            return _error(request_id, "field must be email, password, or both")
        if os.environ.get("APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED", "").strip() != "1":
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": "Credential relay is not authorized."}],
                    "isError": True,
                },
            )
        password = ""
        try:
            path = _credential_path().resolve()
            record = _read_record(path)
            password = _decrypt_password(path)
            port = int(os.environ.get("APPLYPILOT_CDP_PORT", "9222"))
            outcome = _fill_fields(port, field, record["email"], password)
        except (CredentialRelayError, OSError, ValueError) as exc:
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
        finally:
            password = ""
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(outcome)}],
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
