"""Codex App Server stdio transport and Runtime Cell adapter.

The transport owns one long-lived local ``codex app-server`` process and the
newline-delimited JSON-RPC protocol spoken over its standard streams.  The
adapter gives each application actor one Codex thread and starts one turn at a
time on that thread.  It never owns browser, submission, ledger, or receipt
authority and never falls back to another runtime after a request is sent.
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from applypilot.apply.agent_runtime import resolve_codex_command
from applypilot.apply.runtime_cell import (
    CODEX_APP_SERVER_REQUIRED_CAPABILITIES,
    RuntimeAdapterHealth,
    RuntimeCellExecutionState,
    RuntimeCellRequest,
    RuntimeCellTurn,
)

# App Server v2 deliberately uses different spellings for these two surfaces:
# thread start/resume accepts SandboxMode, while turn/start accepts SandboxPolicy.
_THREAD_SANDBOX_MODE = "read-only"
_TURN_SANDBOX_POLICY_TYPE = "readOnly"


class CodexAppServerError(RuntimeError):
    """Base error for the local App Server lifecycle."""


class CodexAppServerProtocolError(CodexAppServerError):
    """A response or notification violated the expected JSON-RPC contract."""

    def __init__(self, message: str, *, method: str | None = None, code: object = None) -> None:
        super().__init__(message)
        self.method = method
        self.code = code


class CodexAppServerTimeout(CodexAppServerError, TimeoutError):
    """A bounded App Server operation did not complete in time."""


class CodexAppServerExecutionError(CodexAppServerError):
    """An App Server request failed after runtime ownership became ambiguous."""

    def __init__(
        self,
        message: str,
        *,
        execution_state: RuntimeCellExecutionState,
    ) -> None:
        super().__init__(message)
        self.execution_state = execution_state


@dataclass(frozen=True, slots=True)
class _TransportFailure:
    error: BaseException


@dataclass(slots=True)
class _Subscription:
    token: int
    messages: queue.Queue[Mapping[str, object] | _TransportFailure]
    thread_id: str
    turn_id: str | None = None


_READ_ONLY_MCP_TOOLS = frozenset(
    {
        ("playwright", "browser_console_messages"),
        ("playwright", "browser_network_requests"),
        ("playwright", "browser_snapshot"),
        ("playwright", "browser_take_screenshot"),
        ("playwright", "browser_wait_for"),
    }
)

_DYNAMIC_TOOL_CALL_METHOD = "item/tool/call"
_MAX_DYNAMIC_SCHEMA_BYTES = 16_384
_MAX_DYNAMIC_SCHEMA_DEPTH = 8
_FORBIDDEN_DYNAMIC_TOOL_NAME_PARTS = frozenset(
    {"click", "create", "delete", "fill", "press", "send", "submit", "type", "update", "upload", "write"}
)


def _json_size(value: object) -> int:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("dynamic tool values must be JSON serializable") from exc
    return len(encoded.encode("utf-8"))


def _validate_bounded_schema(schema: Mapping[str, object], *, label: str) -> dict[str, object]:
    rendered = dict(schema)
    if rendered.get("type") != "object":
        raise ValueError(f"{label} must be an object schema")
    if rendered.get("additionalProperties") is not False:
        raise ValueError(f"{label} must set additionalProperties=false")
    if _json_size(rendered) > _MAX_DYNAMIC_SCHEMA_BYTES:
        raise ValueError(f"{label} exceeds the schema byte limit")

    def visit(value: object, depth: int) -> None:
        if depth > _MAX_DYNAMIC_SCHEMA_DEPTH:
            raise ValueError(f"{label} exceeds the schema depth limit")
        if isinstance(value, Mapping):
            if any(key in value for key in ("$ref", "allOf", "anyOf", "oneOf")):
                raise ValueError(f"{label} contains an unbounded schema construct")
            for nested in value.values():
                visit(nested, depth + 1)
        elif isinstance(value, list):
            for nested in value:
                visit(nested, depth + 1)

    visit(rendered, 0)
    return rendered


def _validate_json_object(value: object, schema: Mapping[str, object], *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    rendered = dict(value)
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise TypeError(f"{label} schema properties must be an object")
    required = schema.get("required", ())
    if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
        raise TypeError(f"{label} schema required must be an array")
    missing = [name for name in required if isinstance(name, str) and name not in rendered]
    unknown = set(rendered).difference(properties)
    if missing:
        raise ValueError(f"{label} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(sorted(unknown))}")
    for name, item in rendered.items():
        field_schema = properties.get(name)
        if not isinstance(field_schema, Mapping):
            raise TypeError(f"{label} field {name!r} has no valid schema")
        expected = field_schema.get("type")
        matches = {
            "string": isinstance(item, str),
            "integer": isinstance(item, int) and not isinstance(item, bool),
            "number": isinstance(item, (int, float)) and not isinstance(item, bool),
            "boolean": isinstance(item, bool),
            "object": isinstance(item, Mapping),
            "array": isinstance(item, list),
            "null": item is None,
        }.get(expected, False)
        if not matches:
            raise ValueError(f"{label} field {name!r} does not match type {expected!r}")
        if (
            isinstance(item, str)
            and isinstance(field_schema.get("maxLength"), int)
            and len(item) > int(field_schema["maxLength"])
        ):
            raise ValueError(f"{label} field {name!r} exceeds maxLength")
        if "enum" in field_schema and item not in field_schema["enum"]:
            raise ValueError(f"{label} field {name!r} is outside enum")
    return rendered


DynamicToolHandler = Callable[[Mapping[str, object]], Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class DynamicToolSpec:
    """One bounded, read-only dynamic tool exposed to App Server."""

    name: str
    description: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    handler: DynamicToolHandler
    read_only: bool = True
    defer_loading: bool = False
    timeout: float = 2.0
    max_input_bytes: int = 16_384
    max_output_bytes: int = 65_536

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise ValueError("dynamic tool name is invalid")
        name_parts = frozenset(part for part in re.split(r"[_-]+", self.name.casefold()) if part)
        if name_parts.intersection(_FORBIDDEN_DYNAMIC_TOOL_NAME_PARTS):
            raise ValueError("dynamic tool name describes a forbidden effect")
        if not self.description.strip() or len(self.description) > 500:
            raise ValueError("dynamic tool description is invalid")
        if not callable(self.handler):
            raise TypeError("dynamic tool handler must be callable")
        if self.read_only is not True:
            raise ValueError("App Server dynamic tools must be read-only")
        if self.defer_loading:
            raise ValueError(
                "deferred App Server functions require a namespace and are not enabled"
            )
        _validate_bounded_schema(self.input_schema, label="dynamic tool inputSchema")
        _validate_bounded_schema(self.output_schema, label="dynamic tool outputSchema")
        _validate_timeout(self.timeout, "dynamic tool timeout")
        for value, label in (
            (self.max_input_bytes, "max_input_bytes"),
            (self.max_output_bytes, "max_output_bytes"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")

    def descriptor(self) -> dict[str, object]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description.strip(),
            "inputSchema": dict(self.input_schema),
            "deferLoading": self.defer_loading,
        }


class DynamicToolSurface:
    """Validated descriptors and bounded handlers for one App Server thread."""

    def __init__(self, specs: Sequence[DynamicToolSpec]) -> None:
        by_name: dict[str, DynamicToolSpec] = {}
        for spec in specs:
            if not isinstance(spec, DynamicToolSpec):
                raise TypeError("dynamic tool surface entries must be DynamicToolSpec")
            if spec.name in by_name:
                raise ValueError(f"duplicate dynamic tool name: {spec.name}")
            by_name[spec.name] = spec
        if not by_name:
            raise ValueError("dynamic tool surface must not be empty")
        self._specs = by_name
        descriptors = [spec.descriptor() for spec in by_name.values()]
        self.descriptors = tuple(descriptors)
        durable_manifest = [
            {
                "descriptor": spec.descriptor(),
                "outputSchema": dict(spec.output_schema),
                "readOnly": spec.read_only,
                "timeout": spec.timeout,
                "maxInputBytes": spec.max_input_bytes,
                "maxOutputBytes": spec.max_output_bytes,
            }
            for spec in by_name.values()
        ]
        digest_input = json.dumps(
            durable_manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        self.digest = "sha256:" + hashlib.sha256(digest_input).hexdigest()
        self._responses: dict[
            tuple[str, str, str], tuple[str, Mapping[str, object]]
        ] = {}
        self._lock = threading.RLock()

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def dispatch(self, *, thread_id: str, turn_id: str, call_id: str, name: str, arguments: object) -> Mapping[str, object]:
        key = (thread_id, turn_id, call_id)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"name": name, "arguments": arguments},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        with self._lock:
            cached = self._responses.get(key)
        if cached is not None:
            if cached[0] != fingerprint:
                return _dynamic_tool_failure("callId replay changed tool name or arguments")
            return cached[1]
        spec = self._specs.get(name)
        if spec is None:
            return self._remember(
                key, fingerprint, _dynamic_tool_failure("unknown or disabled dynamic tool")
            )
        try:
            if _json_size(arguments) > spec.max_input_bytes:
                raise ValueError("dynamic tool input exceeds byte limit")
            validated = _validate_json_object(arguments, spec.input_schema, label="dynamic tool input")
            outcome: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

            def invoke_handler() -> None:
                try:
                    outcome.put((True, spec.handler(validated)))
                except Exception as exc:  # noqa: BLE001 - cross the daemon boundary as data
                    outcome.put((False, exc))

            worker = threading.Thread(
                target=invoke_handler,
                name=f"applypilot-dynamic-tool-{spec.name}",
                daemon=True,
            )
            worker.start()
            try:
                succeeded, raw_output = outcome.get(timeout=spec.timeout)
            except queue.Empty as exc:
                raise TimeoutError("dynamic tool handler timed out") from exc
            if not succeeded:
                assert isinstance(raw_output, Exception)
                raise raw_output
            output = raw_output
            validated_output = _validate_json_object(
                output, spec.output_schema, label="dynamic tool output"
            )
            if _json_size(validated_output) > spec.max_output_bytes:
                raise ValueError("dynamic tool output exceeds byte limit")
            response: Mapping[str, object] = {
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": json.dumps(
                            validated_output,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    }
                ],
                "success": True,
            }
        except Exception as exc:  # noqa: BLE001 - isolate untrusted dynamic handlers
            response = _dynamic_tool_failure(str(exc))
        return self._remember(key, fingerprint, response)

    def _remember(
        self,
        key: tuple[str, str, str],
        fingerprint: str,
        response: Mapping[str, object],
    ) -> Mapping[str, object]:
        with self._lock:
            existing = self._responses.setdefault(key, (fingerprint, dict(response)))
        return existing[1]


def _dynamic_tool_failure(message: str) -> Mapping[str, object]:
    return {
        "contentItems": [{"type": "inputText", "text": message[:500]}],
        "success": False,
    }


def app_server_item_starts_effect(
    item: Mapping[str, object],
    *,
    read_only_dynamic_tools: frozenset[str] = frozenset(),
) -> bool:
    """Classify provider items without treating read observations as effects."""

    item_type = item.get("type")
    if item_type in {"command_execution", "file_change"}:
        return True
    if item_type not in {"dynamic_tool_call", "mcp_tool_call", "tool_call"}:
        return False
    server = item.get("server")
    tool = item.get("tool", item.get("name"))
    if item_type == "dynamic_tool_call" and tool in read_only_dynamic_tools:
        return False
    return (server, tool) not in _READ_ONLY_MCP_TOOLS


def _validate_timeout(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return float(value)


class CodexAppServerStdioTransport:
    """Bidirectional, newline-delimited JSON-RPC over a child process's stdio."""

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        startup_timeout: float = 15.0,
        request_timeout: float = 30.0,
        shutdown_timeout: float = 5.0,
        subscription_queue_size: int = 256,
        popen_factory: Any = subprocess.Popen,
    ) -> None:
        resolved = tuple(command or (*resolve_codex_command(), "app-server", "--stdio"))
        if not resolved or any(not isinstance(part, str) or not part for part in resolved):
            raise ValueError("App Server command must contain non-empty strings")
        self.command = resolved
        self.cwd = Path(cwd).resolve() if cwd is not None else None
        self.env = dict(env) if env is not None else None
        self.startup_timeout = _validate_timeout(startup_timeout, "startup_timeout")
        self.request_timeout = _validate_timeout(request_timeout, "request_timeout")
        self.shutdown_timeout = _validate_timeout(shutdown_timeout, "shutdown_timeout")
        if (
            isinstance(subscription_queue_size, bool)
            or not isinstance(subscription_queue_size, int)
            or subscription_queue_size < 1
        ):
            raise ValueError("subscription_queue_size must be a positive integer")
        self.subscription_queue_size = subscription_queue_size
        self.experimental_api = False
        self._popen_factory = popen_factory
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._request_ids = iter(range(1, 2**63))
        self._subscription_ids = iter(range(1, 2**63))
        self._pending: dict[int, queue.Queue[Mapping[str, object] | _TransportFailure]] = {}
        self._subscriptions: dict[int, _Subscription] = {}
        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._ready = False
        self._shutting_down = False
        self._reader_failure: BaseException | None = None
        self._server_request_handler: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None

    def configure_dynamic_tool_handler(
        self,
        handler: Callable[[Mapping[str, object]], Mapping[str, object]] | None,
    ) -> None:
        """Opt into the experimental client API before the process handshake."""

        with self._lifecycle_lock:
            if self._process is not None:
                raise CodexAppServerError("dynamic tools must be configured before App Server starts")
            self._server_request_handler = handler
            self.experimental_api = handler is not None

    @property
    def process(self) -> subprocess.Popen[str] | None:
        return self._process

    @property
    def is_ready(self) -> bool:
        process = self._process
        return bool(self._ready and process is not None and process.poll() is None)

    def start(self) -> None:
        """Spawn the child and complete ``initialize``/``initialized`` once."""

        with self._lifecycle_lock:
            if self.is_ready:
                return
            if self._process is not None:
                raise CodexAppServerError("Codex App Server transport cannot be restarted")
            self._shutting_down = False
            self._reader_failure = None
            try:
                process = self._popen_factory(
                    list(self.command),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    cwd=str(self.cwd) if self.cwd is not None else None,
                    env=self.env,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise CodexAppServerError("Codex App Server process could not start") from exc
            if process.stdin is None or process.stdout is None or process.stderr is None:
                process.terminate()
                raise CodexAppServerError("Codex App Server stdio pipes are unavailable")
            self._process = process
            self._reader = threading.Thread(
                target=self._read_stdout,
                name="applypilot-codex-app-server-stdout",
                daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._read_stderr,
                name="applypilot-codex-app-server-stderr",
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader.start()
            try:
                result = self._request_raw(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "applypilot",
                            "title": "ApplyPilot",
                            "version": "0.4.0",
                        },
                        **(
                            {"capabilities": {"experimentalApi": True}}
                            if self.experimental_api
                            else {}
                        ),
                    },
                    timeout=self.startup_timeout,
                )
                if not isinstance(result, Mapping):
                    raise CodexAppServerProtocolError(
                        "initialize returned a non-object result",
                        method="initialize",
                    )
                self._notify_raw("initialized", {})
                self._ready = True
            except BaseException:
                self.shutdown()
                raise

    def initialize(self) -> None:
        """Explicit handshake alias for callers that model protocol phases."""

        self.start()

    def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object]:
        self.start()
        return self._request_raw(method, params or {}, timeout=timeout)

    def notify(self, method: str, params: Mapping[str, object] | None = None) -> None:
        self.start()
        self._notify_raw(method, params or {})

    def subscribe(self, *, thread_id: str, turn_id: str | None = None) -> _Subscription:
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("subscription thread_id is required")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
            raise ValueError("subscription turn_id must be a non-empty string")
        self.start()
        token = next(self._subscription_ids)
        subscription = _Subscription(
            token=token,
            messages=queue.Queue(maxsize=self.subscription_queue_size),
            thread_id=thread_id,
            turn_id=turn_id,
        )
        with self._state_lock:
            self._subscriptions[token] = subscription
        return subscription

    def bind_subscription_turn(self, subscription: _Subscription, turn_id: str) -> None:
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("subscription turn_id is required")
        with self._state_lock:
            if self._subscriptions.get(subscription.token) is not subscription:
                raise CodexAppServerError("Codex App Server subscription is no longer active")
            subscription.turn_id = turn_id

    def unsubscribe(self, subscription: _Subscription) -> None:
        with self._state_lock:
            self._subscriptions.pop(subscription.token, None)

    def receive(
        self,
        subscription: _Subscription,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object]:
        try:
            item = subscription.messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexAppServerTimeout("Codex App Server event drain timed out") from exc
        if isinstance(item, _TransportFailure):
            if isinstance(item.error, CodexAppServerError):
                raise item.error
            raise CodexAppServerError("Codex App Server event stream closed") from item.error
        return item

    def _request_raw(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float | None,
    ) -> Mapping[str, object]:
        if not isinstance(method, str) or not method:
            raise ValueError("JSON-RPC method is required")
        request_id = next(self._request_ids)
        responses: queue.Queue[Mapping[str, object] | _TransportFailure] = queue.Queue(maxsize=1)
        with self._state_lock:
            self._pending[request_id] = responses
        try:
            self._write_message({"method": method, "id": request_id, "params": dict(params)})
            try:
                response = responses.get(timeout=self.request_timeout if timeout is None else timeout)
            except queue.Empty as exc:
                raise CodexAppServerTimeout(f"{method} request timed out") from exc
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        if isinstance(response, _TransportFailure):
            raise CodexAppServerError(f"{method} response stream closed") from response.error
        if "error" in response:
            raw_error = response.get("error")
            error = raw_error if isinstance(raw_error, Mapping) else {}
            code = error.get("code")
            raise CodexAppServerProtocolError(
                f"{method} failed with JSON-RPC code {code!r}",
                method=method,
                code=code,
            )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise CodexAppServerProtocolError(
                f"{method} returned a non-object result",
                method=method,
            )
        return result

    def _notify_raw(self, method: str, params: Mapping[str, object]) -> None:
        self._write_message({"method": method, "params": dict(params)})

    def _write_message(self, message: Mapping[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise CodexAppServerError("Codex App Server process is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise CodexAppServerError("Codex App Server request write failed") from exc

    def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        failure: BaseException | None = None
        try:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CodexAppServerProtocolError("Codex App Server emitted invalid JSONL") from exc
                if not isinstance(message, dict):
                    raise CodexAppServerProtocolError("Codex App Server emitted a non-object message")
                self._dispatch_message(message)
        except (OSError, RuntimeError, ValueError) as exc:
            failure = exc
        finally:
            if failure is None and not self._shutting_down:
                failure = CodexAppServerError("Codex App Server stdout closed")
            if failure is not None:
                self._reader_failure = failure
                self._fail_waiters(failure)
            self._ready = False

    def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            for raw_line in process.stderr:
                line = raw_line.rstrip()
                if line:
                    self._stderr_tail.append(line[:500])
        except (OSError, ValueError):
            return

    def _dispatch_message(self, message: Mapping[str, object]) -> None:
        request_id = message.get("id")
        if request_id is not None and ("result" in message or "error" in message):
            if isinstance(request_id, bool) or not isinstance(request_id, int):
                raise CodexAppServerProtocolError("JSON-RPC response id must be an integer")
            with self._state_lock:
                pending = self._pending.get(request_id)
            if pending is not None:
                try:
                    pending.put_nowait(message)
                except queue.Full as exc:
                    raise CodexAppServerProtocolError("JSON-RPC response id was delivered more than once") from exc
            return
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise CodexAppServerProtocolError("JSON-RPC message has no method")
        if request_id is not None:
            if method == _DYNAMIC_TOOL_CALL_METHOD and self._server_request_handler is not None:
                self._handle_server_request(request_id, message)
            else:
                self._reject_server_request(request_id)
            return
        with self._state_lock:
            subscriptions = tuple(self._subscriptions.values())
        for subscription in subscriptions:
            params = message.get("params")
            if not isinstance(params, Mapping):
                continue
            thread_id = _notification_thread_id(params)
            turn_id = _notification_turn_id(params)
            if thread_id != subscription.thread_id:
                continue
            if subscription.turn_id is not None and turn_id != subscription.turn_id:
                continue
            try:
                subscription.messages.put_nowait(message)
            except queue.Full:
                self._fail_subscription_overflow(subscription)

    def _fail_subscription_overflow(self, subscription: _Subscription) -> None:
        failure = _TransportFailure(
            CodexAppServerError("Codex App Server subscription event queue overflow")
        )
        with self._state_lock:
            if self._subscriptions.get(subscription.token) is subscription:
                self._subscriptions.pop(subscription.token, None)
        try:
            subscription.messages.get_nowait()
        except queue.Empty:
            pass
        try:
            subscription.messages.put_nowait(failure)
        except queue.Full:
            pass

    def _reject_server_request(self, request_id: object) -> None:
        # ApplyPilot does not delegate approval or elicitation authority to the
        # runtime host. ``approvalPolicy=never`` should prevent these requests;
        # if a future server emits one, fail it closed instead of hanging.
        self._write_message(
            {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": "ApplyPilot client request handling is disabled",
                },
            }
        )

    def _handle_server_request(
        self,
        request_id: object,
        message: Mapping[str, object],
    ) -> None:
        handler = self._server_request_handler
        if handler is None:
            self._reject_server_request(request_id)
            return
        try:
            result = handler(message)
            if not isinstance(result, Mapping):
                raise TypeError("dynamic tool dispatcher returned a non-object")
        except Exception as exc:  # noqa: BLE001 - isolate the optional dispatcher
            result = _dynamic_tool_failure(str(exc))
        self._write_message({"id": request_id, "result": dict(result)})

    def _fail_waiters(self, error: BaseException) -> None:
        failure = _TransportFailure(error)
        with self._state_lock:
            pending = tuple(self._pending.values())
            subscriptions = tuple(self._subscriptions.values())
        for waiter in pending:
            try:
                waiter.put_nowait(failure)
            except queue.Full:
                pass
        for subscription in subscriptions:
            try:
                subscription.messages.put_nowait(failure)
            except queue.Full:
                self._fail_subscription_overflow(subscription)

    def shutdown(self) -> None:
        """Close stdin, then terminate only this owned child if it does not exit."""

        with self._lifecycle_lock:
            process = self._process
            if process is None:
                return
            self._shutting_down = True
            self._ready = False
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=self.shutdown_timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=self.shutdown_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self.shutdown_timeout)
            finally:
                failure = CodexAppServerError("Codex App Server transport shut down")
                self._fail_waiters(failure)
                with self._state_lock:
                    self._pending.clear()
                    self._subscriptions.clear()
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                if self._reader is not None:
                    self._reader.join(timeout=self.shutdown_timeout)
                if self._stderr_reader is not None:
                    self._stderr_reader.join(timeout=self.shutdown_timeout)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()


CodexAppServerTransport = CodexAppServerStdioTransport


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _snake_case(value: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", value).casefold()


def _notification_thread_id(params: Mapping[str, object]) -> str | None:
    direct = params.get("threadId")
    if isinstance(direct, str) and direct:
        return direct
    thread = params.get("thread")
    if isinstance(thread, Mapping):
        nested = thread.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _notification_turn_id(params: Mapping[str, object]) -> str | None:
    direct = params.get("turnId")
    if isinstance(direct, str) and direct:
        return direct
    turn = params.get("turn")
    if isinstance(turn, Mapping):
        nested = turn.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _normalize_item(raw_item: object) -> dict[str, object]:
    if not isinstance(raw_item, Mapping):
        raise CodexAppServerProtocolError("item notification has no object item")
    item = dict(raw_item)
    raw_type = item.get("type")
    if isinstance(raw_type, str) and raw_type:
        item["type"] = _snake_case(raw_type)
    return item


def normalize_app_server_event(message: Mapping[str, object]) -> dict[str, object]:
    """Normalize one v2 App Server notification to the existing CLI event shape."""

    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str) or not isinstance(params, Mapping):
        raise CodexAppServerProtocolError("App Server notification is malformed")
    normalized_type = ".".join(_snake_case(part) for part in method.split("/"))
    event: dict[str, object] = {"type": normalized_type}
    thread_id = _notification_thread_id(params)
    turn_id = _notification_turn_id(params)
    if thread_id is not None:
        event["thread_id"] = thread_id
    if turn_id is not None:
        event["turn_id"] = turn_id
    if "item" in params:
        event["item"] = _normalize_item(params["item"])
    for key in ("itemId", "delta", "diff", "plan", "explanation", "message"):
        if key in params:
            event[_snake_case(key)] = params[key]
    if method == "turn/completed":
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            raise CodexAppServerProtocolError("turn/completed has no turn object")
        status = turn.get("status")
        if not isinstance(status, str) or status not in {
            "completed",
            "interrupted",
            "failed",
        }:
            raise CodexAppServerProtocolError("turn/completed has invalid status")
        event["status"] = status
        event["turn"] = dict(turn)
        if turn.get("error") is not None:
            event["error"] = turn["error"]
    return event


@dataclass(slots=True)
class _ActiveTurn:
    actor_id: str
    thread_id: str
    turn_id: str
    subscription: _Subscription
    effect_started: bool = False
    submit_started: bool = False
    terminal: bool = False
    consumer_lock: threading.Lock = field(default_factory=threading.Lock)

    def execution_state(self) -> RuntimeCellExecutionState:
        return RuntimeCellExecutionState(
            request_accepted=True,
            tool_or_effect_started=self.effect_started,
            submit_started=self.submit_started,
            bound_backend="codex-app-server",
        )


class CodexAppServerAdapter:
    """Runtime Cell adapter with one persistent Codex thread per application."""

    backend = "codex-app-server"

    def __init__(
        self,
        transport: CodexAppServerStdioTransport | None = None,
        *,
        command: Sequence[str] | None = None,
        thread_config: Mapping[str, object] | None = None,
        drain_timeout: float = 5.0,
    ) -> None:
        if transport is not None and command is not None:
            raise ValueError("pass transport or command, not both")
        self.transport = transport or CodexAppServerStdioTransport(command)
        self.thread_config = dict(thread_config or {})
        self.drain_timeout = _validate_timeout(drain_timeout, "drain_timeout")
        self._application_threads: dict[str, str] = {}
        self._thread_applications: dict[str, str] = {}
        self._active_turns: dict[str, _ActiveTurn] = {}
        self._opening_applications: set[str] = set()
        self._dynamic_surface: DynamicToolSurface | None = None
        self._application_surface_digests: dict[str, str] = {}
        self._lock = threading.RLock()

    def configure_dynamic_tools(self, surface: DynamicToolSurface | None) -> None:
        """Configure a validated read-only surface while this worker is idle."""

        with self._lock:
            if self._active_turns or self._opening_applications:
                raise CodexAppServerExecutionError(
                    "dynamic tool surface cannot change during an active turn",
                    execution_state=RuntimeCellExecutionState(
                        request_accepted=True,
                        tool_or_effect_started=False,
                        submit_started=False,
                        bound_backend="codex-app-server",
                    ),
                )
            current_digest = self._dynamic_surface.digest if self._dynamic_surface is not None else None
            next_digest = surface.digest if surface is not None else None
            if current_digest == next_digest:
                return
            self.transport.configure_dynamic_tool_handler(
                self._handle_dynamic_tool_request if surface is not None else None
            )
            self._dynamic_surface = surface

    @property
    def dynamic_surface_digest(self) -> str | None:
        surface = self._dynamic_surface
        return surface.digest if surface is not None else None

    def bind_dynamic_surface_digest(self, actor_id: str, digest: str) -> None:
        """Restore the durable surface identity before resuming a thread."""

        if not actor_id or not digest.startswith("sha256:"):
            raise ValueError("actor_id and dynamic surface digest are required")
        with self._lock:
            previous = self._application_surface_digests.setdefault(actor_id, digest)
            if previous != digest:
                raise CodexAppServerProtocolError(
                    "dynamic tool surface digest changed for the application thread"
                )

    def configure_thread(self, config: Mapping[str, object]) -> None:
        """Replace per-turn configuration only while this worker is idle.

        The launcher owns tool scoping and per-turn MCP environment bindings.
        Keeping this mutation behind the adapter lock prevents one application
        from observing another application's paths or tool surface.
        """

        if not isinstance(config, Mapping):
            raise TypeError("App Server thread config must be a mapping")
        with self._lock:
            if self._active_turns or self._opening_applications:
                raise CodexAppServerExecutionError(
                    "App Server thread config cannot change during an active turn",
                    execution_state=RuntimeCellExecutionState(
                        request_accepted=True,
                        tool_or_effect_started=False,
                        submit_started=False,
                        bound_backend="codex-app-server",
                    ),
                )
            self.thread_config = dict(config)

    def health(self) -> RuntimeAdapterHealth:
        try:
            self.transport.start()
        except (OSError, RuntimeError, TimeoutError, ValueError):
            return RuntimeAdapterHealth(
                backend="codex-app-server",
                status="unavailable",
                reason_code="CODEX_APP_SERVER_UNAVAILABLE",
                capabilities=CODEX_APP_SERVER_REQUIRED_CAPABILITIES,
            )
        status = "ready" if self.transport.is_ready else "unavailable"
        return RuntimeAdapterHealth(
            backend="codex-app-server",
            status=status,
            reason_code=("CODEX_APP_SERVER_READY" if status == "ready" else "CODEX_APP_SERVER_UNAVAILABLE"),
            capabilities=CODEX_APP_SERVER_REQUIRED_CAPABILITIES,
        )

    def start(self, request: RuntimeCellRequest) -> RuntimeCellTurn:
        self._claim_application_turn(request.actor_id)
        try:
            with self._lock:
                existing = self._application_threads.get(request.actor_id)
            if existing is not None:
                return self._open_turn(request, thread_id=existing, resume=True)
            return self._open_turn(request, thread_id=None, resume=False)
        finally:
            with self._lock:
                self._opening_applications.discard(request.actor_id)

    def resume(self, request: RuntimeCellRequest) -> RuntimeCellTurn:
        self._claim_application_turn(request.actor_id)
        try:
            with self._lock:
                existing = self._application_threads.get(request.actor_id)
            parent = request.parent_provider_session_id or existing
            if parent is None:
                raise ValueError("resume requires parent_provider_session_id")
            if existing is not None and existing != parent:
                raise CodexAppServerExecutionError(
                    "application thread identity changed during repair",
                    execution_state=RuntimeCellExecutionState(
                        request_accepted=True,
                        tool_or_effect_started=False,
                        submit_started=False,
                        bound_backend="codex-app-server",
                    ),
                )
            return self._open_turn(request, thread_id=parent, resume=True)
        finally:
            with self._lock:
                self._opening_applications.discard(request.actor_id)

    def _claim_application_turn(self, actor_id: str) -> None:
        with self._lock:
            active = next(
                (state for state in self._active_turns.values() if state.actor_id == actor_id and not state.terminal),
                None,
            )
            opening = actor_id in self._opening_applications
            if active is None and not opening:
                self._opening_applications.add(actor_id)
                return
        if active is not None:
            raise CodexAppServerExecutionError(
                "application already has an active App Server turn",
                execution_state=active.execution_state(),
            )
        raise CodexAppServerExecutionError(
            "application App Server turn is already opening",
            execution_state=RuntimeCellExecutionState(
                request_accepted=True,
                tool_or_effect_started=False,
                submit_started=False,
                bound_backend="codex-app-server",
            ),
        )

    def _open_turn(
        self,
        request: RuntimeCellRequest,
        *,
        thread_id: str | None,
        resume: bool,
    ) -> RuntimeCellTurn:
        self.transport.start()
        thread_method = "thread/resume" if resume else "thread/start"
        thread_params: dict[str, object] = {
            "model": request.model,
            "cwd": str(request.cwd.resolve()),
            "approvalPolicy": "never",
            "sandbox": _THREAD_SANDBOX_MODE,
        }
        if not resume:
            thread_params.update(
                {
                    "ephemeral": False,
                    "serviceName": "applypilot",
                }
            )
            if self._dynamic_surface is not None:
                thread_params["dynamicTools"] = list(self._dynamic_surface.descriptors)
        if self.thread_config:
            thread_params["config"] = dict(self.thread_config)
        if thread_id is not None:
            thread_params["threadId"] = thread_id
        try:
            thread_result = self.transport.request(thread_method, thread_params)
            returned_thread = thread_result.get("thread")
            if not isinstance(returned_thread, Mapping):
                raise CodexAppServerProtocolError(
                    f"{thread_method} returned no thread",
                    method=thread_method,
                )
            returned_thread_id = returned_thread.get("id")
            if not isinstance(returned_thread_id, str) or not returned_thread_id:
                raise CodexAppServerProtocolError(
                    f"{thread_method} returned an invalid thread id",
                    method=thread_method,
                )
            if thread_id is not None and returned_thread_id != thread_id:
                raise CodexAppServerProtocolError(
                    "thread/resume changed the application thread id",
                    method=thread_method,
                )
            surface_digest = self._dynamic_surface.digest if self._dynamic_surface is not None else None
            with self._lock:
                bound_digest = self._application_surface_digests.get(request.actor_id)
                if resume and bound_digest != surface_digest:
                    raise CodexAppServerProtocolError(
                        "thread/resume dynamic tool surface digest does not match the durable binding",
                        method=thread_method,
                    )
                if not resume and surface_digest is not None:
                    self._application_surface_digests[request.actor_id] = surface_digest
            self._bind_application_thread(request.actor_id, returned_thread_id)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            raise CodexAppServerExecutionError(
                f"{thread_method} did not produce a usable application thread",
                execution_state=RuntimeCellExecutionState(
                    request_accepted=True,
                    tool_or_effect_started=False,
                    submit_started=False,
                    bound_backend="codex-app-server",
                ),
            ) from exc

        subscription = self.transport.subscribe(thread_id=returned_thread_id)
        try:
            turn_result = self.transport.request(
                "turn/start",
                {
                    "threadId": returned_thread_id,
                    "input": [{"type": "text", "text": request.prompt}],
                    "cwd": str(request.cwd.resolve()),
                    "approvalPolicy": "never",
                    "sandboxPolicy": {
                        "type": _TURN_SANDBOX_POLICY_TYPE,
                        "access": {"type": "fullAccess"},
                    },
                    "model": request.model,
                },
            )
            raw_turn = turn_result.get("turn")
            if not isinstance(raw_turn, Mapping):
                raise CodexAppServerProtocolError(
                    "turn/start returned no turn",
                    method="turn/start",
                )
            turn_id_value = raw_turn.get("id")
            if not isinstance(turn_id_value, str) or not turn_id_value:
                raise CodexAppServerProtocolError(
                    "turn/start returned an invalid turn id",
                    method="turn/start",
                )
            self.transport.bind_subscription_turn(subscription, turn_id_value)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            self.transport.unsubscribe(subscription)
            raise CodexAppServerExecutionError(
                "turn/start failed after the application thread was accepted",
                execution_state=RuntimeCellExecutionState(
                    request_accepted=True,
                    tool_or_effect_started=False,
                    submit_started=False,
                    bound_backend="codex-app-server",
                ),
            ) from exc

        state = _ActiveTurn(
            actor_id=request.actor_id,
            thread_id=returned_thread_id,
            turn_id=turn_id_value,
            subscription=subscription,
        )
        with self._lock:
            if turn_id_value in self._active_turns:
                self.transport.unsubscribe(subscription)
                raise CodexAppServerExecutionError(
                    "App Server returned a duplicate active turn id",
                    execution_state=state.execution_state(),
                )
            self._active_turns[turn_id_value] = state
        return RuntimeCellTurn(
            backend="codex-app-server",
            provider_session_id=returned_thread_id,
            provider_turn_id=turn_id_value,
            events=self._event_iterator(state),
        )

    def _bind_application_thread(self, actor_id: str, thread_id: str) -> None:
        with self._lock:
            previous_thread = self._application_threads.get(actor_id)
            previous_actor = self._thread_applications.get(thread_id)
            if previous_thread is not None and previous_thread != thread_id:
                raise CodexAppServerProtocolError("one application cannot own multiple App Server threads")
            if previous_actor is not None and previous_actor != actor_id:
                raise CodexAppServerProtocolError("one App Server thread cannot serve multiple applications")
            self._application_threads[actor_id] = thread_id
            self._thread_applications[thread_id] = actor_id

    def execution_state(self, provider_turn_id: str) -> RuntimeCellExecutionState:
        with self._lock:
            state = self._active_turns.get(provider_turn_id)
        if state is None:
            raise KeyError(provider_turn_id)
        return state.execution_state()

    def mark_submit_started(self, provider_turn_id: str) -> RuntimeCellExecutionState:
        """Record host-observed Submit activation; the adapter cannot infer it."""

        with self._lock:
            state = self._active_turns.get(provider_turn_id)
            if state is None:
                raise KeyError(provider_turn_id)
            state.submit_started = True
        return state.execution_state()

    def cancel(self, provider_turn_id: str) -> None:
        with self._lock:
            state = self._active_turns.get(provider_turn_id)
        if state is None or state.terminal:
            return
        self._interrupt_state(state)

    def _interrupt_state(self, state: _ActiveTurn, *, timeout: float | None = None) -> None:
        try:
            self.transport.request(
                "turn/interrupt",
                {"threadId": state.thread_id, "turnId": state.turn_id},
                timeout=timeout,
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            raise CodexAppServerExecutionError(
                "turn/interrupt failed after the turn was accepted",
                execution_state=state.execution_state(),
            ) from exc

    def interrupt(self, provider_turn_id: str) -> None:
        """Protocol-named alias for Runtime Cell's ``cancel`` operation."""

        self.cancel(provider_turn_id)

    def steer(
        self,
        provider_turn_id: str,
        *,
        expected_turn_id: str,
        prompt: str,
    ) -> None:
        """Steer exactly the active turn, failing closed on stale identity."""

        if not isinstance(expected_turn_id, str) or not expected_turn_id:
            raise ValueError("expected_turn_id is required")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("steer prompt is required")
        with self._lock:
            state = self._active_turns.get(provider_turn_id)
            if (
                state is None
                or state.terminal
                or state.turn_id != expected_turn_id
                or provider_turn_id != expected_turn_id
            ):
                raise CodexAppServerExecutionError(
                    "turn/steer expectedTurnId does not bind the active turn",
                    execution_state=(
                        state.execution_state()
                        if state is not None
                        else RuntimeCellExecutionState(
                            request_accepted=True,
                            tool_or_effect_started=False,
                            submit_started=False,
                            bound_backend="codex-app-server",
                        )
                    ),
                )
            thread_id = state.thread_id
        try:
            self.transport.request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": expected_turn_id,
                    "input": [{"type": "text", "text": prompt.strip()}],
                },
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            raise CodexAppServerExecutionError(
                "turn/steer failed after the turn was accepted",
                execution_state=state.execution_state(),
            ) from exc

    def drain(
        self,
        provider_turn_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        """Consume remaining notifications through each selected terminal turn."""

        timeout_value = self.drain_timeout if timeout is None else _validate_timeout(timeout, "timeout")
        deadline = time.monotonic() + timeout_value
        with self._lock:
            if provider_turn_id is None:
                states = tuple(self._active_turns.values())
            else:
                state = self._active_turns.get(provider_turn_id)
                states = () if state is None else (state,)
        drained: list[Mapping[str, object]] = []
        for state in states:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerTimeout("Codex App Server drain timed out")
            drained.extend(self._drain_state(state, timeout=remaining))
        return tuple(drained)

    def _event_iterator(self, state: _ActiveTurn) -> Iterator[Mapping[str, object]]:
        while not state.terminal:
            with state.consumer_lock:
                event = self._next_event(state, timeout=None)
            yield event

    def _drain_state(
        self,
        state: _ActiveTurn,
        *,
        timeout: float,
    ) -> list[Mapping[str, object]]:
        deadline = time.monotonic() + timeout
        events: list[Mapping[str, object]] = []
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not state.consumer_lock.acquire(timeout=remaining):
            raise CodexAppServerTimeout("Codex App Server drain timed out waiting for event consumer")
        try:
            while not state.terminal:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexAppServerTimeout("Codex App Server drain timed out")
                events.append(self._next_event(state, timeout=remaining))
        finally:
            state.consumer_lock.release()
        return events

    def _next_event(
        self,
        state: _ActiveTurn,
        *,
        timeout: float | None,
    ) -> Mapping[str, object]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise CodexAppServerTimeout("Codex App Server event drain timed out")
            try:
                message = self.transport.receive(state.subscription, timeout=remaining)
            except CodexAppServerTimeout:
                raise
            except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                raise CodexAppServerExecutionError(
                    "App Server event stream failed after the turn was accepted",
                    execution_state=state.execution_state(),
                ) from exc
            params = message.get("params")
            if not isinstance(params, Mapping):
                continue
            if _notification_thread_id(params) != state.thread_id:
                continue
            event_turn_id = _notification_turn_id(params)
            if event_turn_id != state.turn_id:
                continue
            try:
                event = normalize_app_server_event(message)
            except (TypeError, ValueError) as exc:
                raise CodexAppServerExecutionError(
                    "App Server event normalization failed after the turn was accepted",
                    execution_state=state.execution_state(),
                ) from exc
            item = event.get("item")
            dynamic_names = (
                self._dynamic_surface.names if self._dynamic_surface is not None else frozenset()
            )
            if isinstance(item, Mapping) and app_server_item_starts_effect(
                item,
                read_only_dynamic_tools=dynamic_names,
            ):
                state.effect_started = True
            if event.get("type") == "turn.completed":
                state.terminal = True
                self._release_turn(state)
            return event

    def _release_turn(self, state: _ActiveTurn) -> None:
        self.transport.unsubscribe(state.subscription)
        with self._lock:
            if self._active_turns.get(state.turn_id) is state:
                self._active_turns.pop(state.turn_id, None)

    def _handle_dynamic_tool_request(
        self,
        message: Mapping[str, object],
    ) -> Mapping[str, object]:
        surface = self._dynamic_surface
        params = message.get("params")
        if surface is None or not isinstance(params, Mapping):
            return _dynamic_tool_failure("dynamic tool surface is unavailable")
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        call_id = params.get("callId")
        name = params.get("tool", params.get("name"))
        if not all(isinstance(value, str) and value for value in (thread_id, turn_id, call_id, name)):
            return _dynamic_tool_failure("dynamic tool call identity is incomplete")
        with self._lock:
            state = self._active_turns.get(str(turn_id))
            bound = bool(
                state is not None
                and not state.terminal
                and state.thread_id == thread_id
                and state.turn_id == turn_id
            )
        if not bound:
            return _dynamic_tool_failure("dynamic tool call does not bind the active thread and turn")
        return surface.dispatch(
            thread_id=str(thread_id),
            turn_id=str(turn_id),
            call_id=str(call_id),
            name=str(name),
            arguments=params.get("arguments", {}),
        )

    def close_application(self, provider_session_id: str) -> None:
        with self._lock:
            active_ids = tuple(
                state.turn_id for state in self._active_turns.values() if state.thread_id == provider_session_id
            )
        for turn_id in active_ids:
            self.cancel(turn_id)
            self.drain(turn_id, timeout=self.drain_timeout)
        self.transport.request(
            "thread/unsubscribe",
            {"threadId": provider_session_id},
        )
        with self._lock:
            actor_id = self._thread_applications.pop(provider_session_id, None)
            if actor_id is not None:
                self._application_threads.pop(actor_id, None)
                self._application_surface_digests.pop(actor_id, None)

    def shutdown(self) -> None:
        """Interrupt and drain owned turns, then stop only the owned process."""

        with self._lock:
            active_states = tuple(self._active_turns.values())
        deadline = time.monotonic() + self.drain_timeout
        try:
            for state in active_states:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    self._interrupt_state(state, timeout=remaining)
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self.drain(state.turn_id, timeout=remaining)
                except (KeyError, OSError, RuntimeError, TimeoutError, ValueError):
                    # Transport shutdown below is the final bounded containment step.
                    pass
        finally:
            self.transport.shutdown()

    def __enter__(self) -> Self:
        health = self.health()
        if health.status != "ready":
            raise CodexAppServerError("Codex App Server is unavailable")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()


__all__ = [
    "CodexAppServerAdapter",
    "CodexAppServerError",
    "CodexAppServerExecutionError",
    "CodexAppServerProtocolError",
    "CodexAppServerStdioTransport",
    "CodexAppServerTimeout",
    "CodexAppServerTransport",
    "DynamicToolSpec",
    "DynamicToolSurface",
    "app_server_item_starts_effect",
    "normalize_app_server_event",
]
