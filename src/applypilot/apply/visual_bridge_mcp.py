"""Minimal stdio MCP surface for the supervised visual-operation bridge."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from applypilot.apply.visual_bridge import (
    VisualBridgeError,
    request_visual_operation,
    timeout_from_environment,
)

BRIDGE_DIR_ENV = "APPLYPILOT_VISUAL_BRIDGE_DIR"
TOOL_NAME = "visual_operation"


def _result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_result(code: str, message: str, *, outcome: str | None = None) -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {"code": code, "outcome": outcome or code},
        "isError": True,
    }


def _tool() -> dict[str, object]:
    argument_properties: dict[str, object] = {
        "x": {"type": "integer", "minimum": 0},
        "y": {"type": "integer", "minimum": 0},
        "mode": {"type": "string", "enum": ["dom", "screenshot"], "description": "Observation format, also optional after an action (default dom)."},
        "node_id": {"type": "string", "minLength": 1, "description": "Observed node for click, or optional observed text input for type_text (browser only)."},
        "element_index": {"type": "integer", "minimum": 0},
        "scroll_x": {"type": "integer", "minimum": -2000, "maximum": 2000},
        "scroll_y": {"type": "integer", "minimum": -2000, "maximum": 2000},
        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
        "artifact_id": {"type": "string", "minLength": 1, "description": "Material reference supplied by the host; never an arbitrary file path."},
        "url": {"type": "string", "description": "An exact web link from the current observation; opens in the same tab."},
        "key": {
            "type": "string",
            "enum": [
                "Enter",
                "Tab",
                "Escape",
                "ArrowUp",
                "ArrowDown",
                "PageUp",
                "PageDown",
                "Home",
                "End",
                "Space",
                "Backspace",
            ],
        },
    }
    argument_properties["keys"] = {
        "type": "array",
        "items": argument_properties["key"],
        "minItems": 1,
        "maxItems": 8,
    }
    return {
        "name": TOOL_NAME,
        "description": (
            "Ask the supervised visual host to observe or operate its fixed application tab. "
            "Actions after observe must reference the returned observation_id. "
            "navigate opens a currently observed link in the same tab; it is browser-only. "
            "upload_artifact selects one host-provided artifact through an observed upload control node_id. "
            "Passwords and OTPs must never be placed in this tool; request secure host authentication instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["observe", "click", "scroll", "type_text", "press_key", "navigate", "upload_artifact"],
                },
                "observation_id": {"type": "string", "minLength": 1},
                "arguments": {
                    "type": "object",
                    "properties": argument_properties,
                    "additionalProperties": False,
                },
            },
            "required": ["operation", "arguments"],
            "additionalProperties": False,
        },
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
                "serverInfo": {"name": "applypilot-visual-bridge", "version": "1"},
            },
        )
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return _result(request_id, {"tools": [_tool()]})
    if method != "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }

    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if params.get("name") != TOOL_NAME:
        return _result(request_id, _error_result("unknown_tool", "Unknown tool."))
    bridge_dir = os.environ.get(BRIDGE_DIR_ENV, "").strip()
    if not bridge_dir:
        return _result(
            request_id,
            _error_result("not_available", f"{BRIDGE_DIR_ENV} is not configured."),
        )
    arguments = params.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    unexpected = set(arguments) - {"operation", "observation_id", "arguments"}
    if unexpected:
        return _result(
            request_id,
            _error_result("invalid_request", "Visual operation contains unsupported fields."),
        )
    operation = str(arguments.get("operation") or "")
    observation_id = arguments.get("observation_id")
    observation_id = observation_id if isinstance(observation_id, str) else None
    operation_arguments = arguments.get("arguments")
    operation_arguments = operation_arguments if isinstance(operation_arguments, dict) else {}
    try:
        response = request_visual_operation(
            Path(bridge_dir),
            operation=operation,
            observation_id=observation_id,
            arguments=operation_arguments,
            timeout_seconds=timeout_from_environment(),
        )
    except VisualBridgeError as exc:
        return _result(
            request_id,
            _error_result(exc.code, str(exc), outcome=exc.outcome),
        )

    content = response["content"]
    structured = {key: value for key, value in response.items() if key != "content"}
    return _result(
        request_id,
        {
            "content": content,
            "structuredContent": structured,
            "isError": not bool(response["ok"]),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--bridge-dir")
    options, _unknown = parser.parse_known_args()
    if options.bridge_dir:
        os.environ[BRIDGE_DIR_ENV] = options.bridge_dir
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = _handle(message) if isinstance(message, dict) else None
        except (TypeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
