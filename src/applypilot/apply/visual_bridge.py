"""Bounded file transport for supervised visual browser operations.

The bridge deliberately contains no desktop automation.  A separately supervised
host owns one fixed browser target, claims requests by atomic rename, invokes a
supported browser or Computer Use API, and writes the result back to this queue.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
HOST_MAX_AGE_SECONDS = 120.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 45.0
MAX_REQUEST_TIMEOUT_SECONDS = 120.0
OPERATIONS = frozenset({"observe", "click", "scroll", "type_text", "press_key", "navigate", "upload_artifact",
                        "fill_control", "select_control", "set_checked", "fill_batch"})
SURFACES = frozenset({"computer_use", "browser"})
PRESS_KEYS = frozenset(
    {
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
    }
)


class VisualBridgeError(RuntimeError):
    """A bounded bridge failure suitable for a structured MCP tool error."""

    def __init__(self, code: str, message: str, *, outcome: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.outcome = outcome or code


@dataclass(frozen=True)
class HostBinding:
    root: Path
    session_id: str
    token_epoch: str
    surface: str
    phase: str
    target: dict[str, object]
    submission_authorized: bool = False


def bootstrap_bridge_directory(root: Path) -> dict[str, Path]:
    """Create and return the small, stable queue layout used by the host."""

    resolved = Path(root).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    paths = {
        "root": resolved,
        "host": resolved / "host.json",
        "pending": resolved / "pending",
        "claimed": resolved / "claimed",
        "responses": resolved / "responses",
        "cancelled": resolved / "cancelled",
        "lock": resolved / ".active_request.lock",
    }
    for name in ("pending", "claimed", "responses", "cancelled"):
        paths[name].mkdir(exist_ok=True)
    return paths


def write_host_metadata(
    root: Path,
    *,
    session_id: str,
    token_epoch: str,
    surface: str,
    target: Mapping[str, object],
    status: str = "active",
    heartbeat_at: float | None = None,
    phase: str = "prepare",
    submission_authorized: bool = False,
) -> dict[str, object]:
    """Publish host metadata atomically; useful to supervised host launchers."""

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "heartbeat_at": time.time() if heartbeat_at is None else heartbeat_at,
        "session_id": session_id,
        "token_epoch": token_epoch,
        "surface": surface,
        "phase": phase,
        "submission_authorized": submission_authorized,
        "target": dict(target),
    }
    _validate_host_payload(payload, now=time.time(), allow_inactive=True)
    paths = bootstrap_bridge_directory(root)
    _atomic_write_json(paths["host"], payload)
    return payload


def read_active_host(
    root: Path,
    *,
    now: float | None = None,
    max_age_seconds: float = HOST_MAX_AGE_SECONDS,
) -> HostBinding:
    paths = bootstrap_bridge_directory(root)
    try:
        payload = json.loads(paths["host"].read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise VisualBridgeError("not_available", "Visual bridge host is not available.") from exc
    if not isinstance(payload, dict):
        raise VisualBridgeError("not_available", "Visual bridge host metadata is invalid.")
    _validate_host_payload(
        payload,
        now=time.time() if now is None else now,
        max_age_seconds=max_age_seconds,
    )
    return HostBinding(
        root=paths["root"],
        session_id=str(payload["session_id"]),
        token_epoch=str(payload["token_epoch"]),
        surface=str(payload["surface"]),
        phase=str(payload["phase"]),
        target=dict(payload["target"]),
        submission_authorized=payload.get("submission_authorized") is True,
    )


def request_visual_operation(
    root: Path,
    *,
    operation: str,
    observation_id: str | None = None,
    arguments: Mapping[str, object] | None = None,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    poll_interval_seconds: float = 0.05,
    now: Callable[[], float] = time.time,
) -> dict[str, object]:
    """Send one operation to a fresh, fixed-target host and await its response."""

    args = dict(arguments or {})
    _validate_operation(operation, observation_id, args)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise VisualBridgeError("invalid_request", "Visual bridge timeout must be positive.")
    timeout_seconds = min(timeout_seconds, MAX_REQUEST_TIMEOUT_SECONDS)
    host = read_active_host(root, now=now())
    if operation == "fill_batch" and (host.phase != "prepare" or host.target.get("runtime") != "iab"):
        raise VisualBridgeError("invalid_request", "Field batch requires an IAB prepare host.")
    if operation in {"navigate", "upload_artifact", "fill_control", "select_control", "set_checked", "fill_batch"} and host.surface != "browser":
        raise VisualBridgeError("invalid_request", f"{operation} is only available on the browser surface.")
    if operation == "type_text" and "node_id" in args and host.surface != "browser":
        raise VisualBridgeError("invalid_request", "Targeted text entry is only available on the browser surface.")
    paths = bootstrap_bridge_directory(host.root)
    request_id = str(uuid.uuid4())
    created_at = now()
    request = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "session_id": host.session_id,
        "token_epoch": host.token_epoch,
        "surface": host.surface,
        "phase": host.phase,
        "target": host.target,
        "operation": operation,
        "arguments": args,
        "created_at": created_at,
        "deadline_at": created_at + timeout_seconds,
    }
    if observation_id is not None:
        request["observation_id"] = observation_id

    lock_fd = _acquire_request_lock(paths["lock"], request_id, request["deadline_at"])
    pending = paths["pending"] / f"{request_id}.json"
    claimed = paths["claimed"] / f"{request_id}.json"
    response_path = paths["responses"] / f"{request_id}.json"
    cancelled = paths["cancelled"] / f"{request_id}.json"
    try:
        if next(paths["claimed"].glob("*.json"), None) is not None:
            raise VisualBridgeError(
                "busy",
                "Visual bridge has a previously claimed request awaiting supervisor reconciliation.",
            )
        _publish_exclusive_json(pending, request)
        deadline = created_at + timeout_seconds
        last_host_check = created_at
        while now() < deadline:
            if response_path.exists():
                return _consume_response(response_path, claimed, request)
            if now() - last_host_check >= 0.5:
                last_host_check = now()
                try:
                    current_host = read_active_host(root, now=now())
                    if current_host.session_id != host.session_id or current_host.token_epoch != host.token_epoch:
                        raise VisualBridgeError("not_available", "Browser host session changed.")
                except VisualBridgeError as exc:
                    # Stop waiting when the attending owner pauses or disconnects.
                    # Cancelling can win only while the operation is still unclaimed.
                    if response_path.exists():
                        return _consume_response(response_path, claimed, request)
                    try:
                        os.replace(pending, cancelled)
                    except FileNotFoundError:
                        if response_path.exists():
                            return _consume_response(response_path, claimed, request)
                        raise VisualBridgeError("outcome_unknown", "Host stopped after claiming an operation; inspect before resuming.") from exc
                    raise VisualBridgeError("not_available", "Browser host paused, stopped or disconnected; pending input cancelled.") from exc
            time.sleep(max(0.001, min(poll_interval_seconds, max(0.0, deadline - now()))))

        # A response may have won the deadline race.  Consume it before trying
        # cancellation so a completed operation is never reported as unknown.
        if response_path.exists():
            return _consume_response(response_path, claimed, request)
        try:
            os.replace(pending, cancelled)
        except FileNotFoundError:
            if response_path.exists():
                return _consume_response(response_path, claimed, request)
            if claimed.exists():
                raise VisualBridgeError(
                    "outcome_unknown",
                    "Visual host claimed the request but did not report an outcome before the deadline.",
                )
            raise VisualBridgeError(
                "outcome_unknown",
                "Visual request disappeared before an outcome was recorded.",
            )
        raise VisualBridgeError(
            "cancelled",
            "Visual request expired before the host claimed it; it was cancelled and will not be replayed.",
        )
    finally:
        os.close(lock_fd)
        paths["lock"].unlink(missing_ok=True)


def write_bridge_response(root: Path, response: Mapping[str, object]) -> Path:
    """Atomically publish a host result after the request has been claimed."""

    request_id = str(response.get("request_id") or "")
    try:
        canonical_request_id = str(uuid.UUID(request_id))
    except ValueError as exc:
        raise ValueError("response request_id must be a UUID") from exc
    if request_id != canonical_request_id:
        raise ValueError("response request_id is required")
    paths = bootstrap_bridge_directory(root)
    destination = paths["responses"] / f"{request_id}.json"
    _atomic_write_json(destination, dict(response))
    return destination


def timeout_from_environment() -> float:
    raw = os.environ.get("APPLYPILOT_VISUAL_BRIDGE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    return min(value, MAX_REQUEST_TIMEOUT_SECONDS)


def _validate_host_payload(
    payload: Mapping[str, object],
    *,
    now: float,
    max_age_seconds: float = HOST_MAX_AGE_SECONDS,
    allow_inactive: bool = False,
) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise VisualBridgeError("not_available", "Visual bridge host schema is unsupported.")
    if not allow_inactive and payload.get("status") != "active":
        raise VisualBridgeError("not_available", "Visual bridge host is not active.")
    if payload.get("status") not in {"active", "paused", "stopped"}:
        raise VisualBridgeError("not_available", "Visual bridge host status is invalid.")
    heartbeat = payload.get("heartbeat_at")
    if not isinstance(heartbeat, (int, float)) or not math.isfinite(float(heartbeat)):
        raise VisualBridgeError("not_available", "Visual bridge heartbeat is invalid.")
    if not allow_inactive and (float(heartbeat) > now + 5 or now - float(heartbeat) >= max_age_seconds):
        raise VisualBridgeError("not_available", "Visual bridge host heartbeat is stale.")
    for field in ("session_id", "token_epoch"):
        if not isinstance(payload.get(field), str) or not str(payload[field]).strip():
            raise VisualBridgeError("not_available", f"Visual bridge {field} is missing.")
    if payload.get("surface") not in SURFACES or payload.get("phase") not in {"discovery", "prepare", "submit"}:
        raise VisualBridgeError("not_available", "Visual bridge surface or phase is invalid.")
    if payload.get("phase") == "submit" and payload.get("submission_authorized") is not True:
        raise VisualBridgeError("not_available", "Submission requires explicit host authorization.")
    if payload.get("phase") != "submit" and payload.get("submission_authorized", False) is not False:
        raise VisualBridgeError("not_available", "Submission authorization requires submit phase.")
    target = payload.get("target")
    if not isinstance(target, dict):
        raise VisualBridgeError("not_available", "Visual bridge target is missing.")
    cdp_port = target.get("cdp_port")
    application_url = target.get("application_url")
    parsed = urlsplit(str(application_url or ""))
    if target.get("runtime") == "iab":
        if payload.get("surface") != "browser" or not isinstance(target.get("tab_id"), str) or not target["tab_id"].strip():
            raise VisualBridgeError("not_available", "In-app browser target requires its actual tab identity.")
        if cdp_port is not None or target.get("worker_session_verified") is not None:
            raise VisualBridgeError("not_available", "In-app browser target must not impersonate a CDP worker.")
    elif payload.get("phase") != "prepare" or not isinstance(cdp_port, int) or isinstance(cdp_port, bool) or not 1 <= cdp_port <= 65535:
        raise VisualBridgeError("not_available", "Visual bridge target CDP port is invalid.")
    loopback_http = False
    if parsed.scheme == "http" and parsed.hostname:
        try:
            loopback_http = parsed.hostname == "localhost" or ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback_http = parsed.hostname == "localhost"
    if (
        (parsed.scheme != "https" and not loopback_http)
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise VisualBridgeError("not_available", "Visual bridge target application URL is invalid.")


def _validate_operation(
    operation: str,
    observation_id: str | None,
    arguments: Mapping[str, object],
) -> None:
    if operation not in OPERATIONS:
        raise VisualBridgeError("invalid_request", "Unsupported visual operation.")
    if operation != "observe" and "mode" in arguments:
        if arguments["mode"] not in {"dom", "screenshot"}:
            raise VisualBridgeError("invalid_request", "Response mode must be dom or screenshot.")
        arguments = {key: value for key, value in arguments.items() if key != "mode"}
    allowed: dict[str, set[str]] = {
        "observe": {"mode"},
        "click": {"node_id", "element_index", "x", "y"},
        "scroll": {"scroll_x", "scroll_y", "x", "y"},
        "type_text": {"text", "node_id"},
        "press_key": {"key", "keys"},
        "navigate": {"url"},
        "upload_artifact": {"artifact_id", "node_id"},
        "fill_control": {"field_key", "value"},
        "select_control": {"field_key", "value"},
        "set_checked": {"field_key", "checked"},
        "fill_batch": {"steps"},
    }
    if not set(arguments).issubset(allowed[operation]):
        raise VisualBridgeError(
            "invalid_request",
            f"Arguments for {operation} contain unsupported fields.",
        )
    if operation == "observe":
        if observation_id is not None:
            raise VisualBridgeError("invalid_request", "observe must not include observation_id.")
        if set(arguments) not in (set(), {"mode"}) or arguments.get("mode") not in {None, "dom", "screenshot"}:
            raise VisualBridgeError("invalid_request", "observe mode must be dom or screenshot.")
        return
    if not isinstance(observation_id, str) or not observation_id.strip():
        raise VisualBridgeError("invalid_request", f"{operation} requires observation_id.")
    if operation == "fill_batch":
        steps = arguments.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 4:
            raise VisualBridgeError("invalid_request", "Field batch requires one to four steps.")
        seen = set()
        for step in steps:
            if not isinstance(step, dict) or step.get("operation") not in {"fill_control", "select_control"}:
                raise VisualBridgeError("invalid_request", "Only routine text and native selects may be batched.")
            _validate_operation(step["operation"], observation_id, {k: v for k, v in step.items() if k != "operation"})
            if step["field_key"] in seen:
                raise VisualBridgeError("invalid_request", "Duplicate batch field.")
            seen.add(step["field_key"])
    elif operation in {"fill_control", "select_control", "set_checked"}:
        value_key = "checked" if operation == "set_checked" else "value"
        if set(arguments) != {"field_key", value_key} or not isinstance(arguments.get("field_key"), str) or not arguments["field_key"].strip():
            raise VisualBridgeError("invalid_request", "Control operation requires an observed field_key and value.")
        if value_key == "checked":
            valid_value = isinstance(arguments[value_key], bool)
        else:
            valid_value = isinstance(arguments[value_key], str) and len(arguments[value_key]) <= 12000
        if not valid_value:
            raise VisualBridgeError("invalid_request", "Control value has an invalid type or length.")
    elif operation == "navigate":
        if set(arguments) != {"url"} or not isinstance(arguments["url"], str):
            raise VisualBridgeError("invalid_request", "navigate requires an observed URL.")
        parsed = urlsplit(arguments["url"])
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise VisualBridgeError("invalid_request", "navigate requires a web URL without credentials.")
    elif operation == "upload_artifact":
        if set(arguments) != {"artifact_id", "node_id"} or any(
            not isinstance(arguments.get(name), str) or not str(arguments[name]).strip()
            for name in ("artifact_id", "node_id")
        ):
            raise VisualBridgeError("invalid_request", "Upload requires a host artifact reference and observed control node_id.")
    elif operation == "click":
        locator_shapes = ({"node_id"}, {"element_index"}, {"x", "y"})
        if set(arguments) not in locator_shapes:
            raise VisualBridgeError("invalid_request", "click requires exactly one bounded locator.")
        if "node_id" in arguments and (
            not isinstance(arguments["node_id"], str) or not arguments["node_id"].strip()
        ):
            raise VisualBridgeError("invalid_request", "click node_id must be a non-empty string.")
        if "element_index" in arguments and (
            not isinstance(arguments["element_index"], int)
            or isinstance(arguments["element_index"], bool)
            or arguments["element_index"] < 0
        ):
            raise VisualBridgeError("invalid_request", "click element_index must be a non-negative integer.")
        if "x" in arguments and any(
            not isinstance(arguments[name], int)
            or isinstance(arguments[name], bool)
            or arguments[name] < 0
            for name in ("x", "y")
        ):
            raise VisualBridgeError("invalid_request", "click coordinates must be non-negative integers.")
    elif operation == "scroll":
        if "scroll_y" not in arguments or not set(arguments).issubset({"scroll_x", "scroll_y", "x", "y"}):
            raise VisualBridgeError("invalid_request", "scroll requires scroll_y and optional scroll_x/x/y.")
        for name in ("scroll_x", "scroll_y"):
            value = arguments.get(name, 0)
            if not isinstance(value, int) or isinstance(value, bool) or not -2000 <= value <= 2000:
                raise VisualBridgeError("invalid_request", "scroll deltas must be integers from -2000 to 2000.")
        if arguments.get("scroll_x", 0) == 0 and arguments["scroll_y"] == 0:
            raise VisualBridgeError("invalid_request", "scroll requires a non-zero delta.")
        if ("x" in arguments) != ("y" in arguments):
            raise VisualBridgeError("invalid_request", "scroll coordinates require both x and y.")
        if "x" in arguments and any(
            not isinstance(arguments[name], int)
            or isinstance(arguments[name], bool)
            or arguments[name] < 0
            for name in ("x", "y")
        ):
            raise VisualBridgeError("invalid_request", "scroll coordinates must be non-negative integers.")
    elif operation == "type_text":
        if set(arguments) not in ({"text"}, {"text", "node_id"}):
            raise VisualBridgeError("invalid_request", "type_text requires text and an optional observed input node_id.")
        if "node_id" in arguments and (not isinstance(arguments["node_id"], str) or not arguments["node_id"].strip()):
            raise VisualBridgeError("invalid_request", "type_text node_id must be a non-empty string.")
        text = arguments["text"]
        if not isinstance(text, str) or not text or len(text) > 4000:
            raise VisualBridgeError("invalid_request", "type_text text must contain 1 to 4000 characters.")
    else:
        if set(arguments) == {"key"}:
            keys = [arguments["key"]]
        elif set(arguments) == {"keys"} and isinstance(arguments["keys"], list):
            keys = arguments["keys"]
        else:
            raise VisualBridgeError("invalid_request", "press_key requires exactly key or keys.")
        if not 1 <= len(keys) <= 8 or any(key not in PRESS_KEYS for key in keys):
            raise VisualBridgeError("invalid_request", "press_key contains an unsupported key sequence.")


def _acquire_request_lock(path: Path, request_id: str, deadline_at: object) -> int:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise VisualBridgeError("busy", "Visual bridge already has an outstanding request.") from exc
    os.write(fd, json.dumps({"request_id": request_id, "deadline_at": deadline_at}).encode("utf-8"))
    return fd


def _publish_exclusive_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _consume_response(path: Path, claimed: Path, request: Mapping[str, object]) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VisualBridgeError("invalid_response", "Visual host response is unreadable.") from exc
    if not isinstance(payload, dict):
        raise VisualBridgeError("invalid_response", "Visual host response is invalid.")
    for field in ("request_id", "session_id", "token_epoch"):
        if payload.get(field) != request.get(field):
            raise VisualBridgeError("invalid_response", f"Visual host response {field} does not match.")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("outcome") not in {
        "completed",
        "failed",
        "outcome_unknown",
    }:
        raise VisualBridgeError("invalid_response", "Visual host response schema or outcome is invalid.")
    if not isinstance(payload.get("ok"), bool) or not isinstance(payload.get("content"), list):
        raise VisualBridgeError("invalid_response", "Visual host response body is invalid.")
    if (payload["outcome"] == "completed") != payload["ok"]:
        raise VisualBridgeError("invalid_response", "Visual host response success and outcome disagree.")
    for block in payload["content"]:
        if not isinstance(block, dict) or block.get("type") not in {"text", "image"}:
            raise VisualBridgeError("invalid_response", "Visual host returned an unsupported content block.")
        if block["type"] == "text" and not isinstance(block.get("text"), str):
            raise VisualBridgeError("invalid_response", "Visual host returned invalid text content.")
        if block["type"] == "image" and (
            not isinstance(block.get("data"), str)
            or block.get("mimeType") not in {"image/png", "image/jpeg", "image/webp"}
        ):
            raise VisualBridgeError("invalid_response", "Visual host returned invalid image content.")
    path.unlink(missing_ok=True)
    claimed.unlink(missing_ok=True)
    return payload


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "HOST_MAX_AGE_SECONDS",
    "MAX_REQUEST_TIMEOUT_SECONDS",
    "HostBinding",
    "VisualBridgeError",
    "bootstrap_bridge_directory",
    "read_active_host",
    "request_visual_operation",
    "timeout_from_environment",
    "write_bridge_response",
    "write_host_metadata",
]
