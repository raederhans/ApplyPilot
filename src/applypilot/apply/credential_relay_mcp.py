"""Minimal stdio MCP surface for the password-safe ATS credential relay."""

from __future__ import annotations

import json
import os
import sys

from applypilot.apply.credential_relay import (
    CredentialRelayError,
    _credential_path,
    _decrypt_password,
    _fill_fields,
    _read_record,
)


def _result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32000, "message": message},
    }


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
        return _result(
            request_id,
            {
                "tools": [
                    {
                        "name": "fill_ats_credentials",
                        "description": (
                            "Fill visible credential fields only in the bound application tab. "
                            "Never submits and never returns the password."
                        ),
                        "inputSchema": {
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
                    }
                ]
            },
        )
    if method == "tools/call":
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if params.get("name") != "fill_ats_credentials":
            return _error(request_id, "Unknown tool")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
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
