"""Production wiring for the feature-gated Codex App Server runtime.

This module keeps the launcher hook deliberately small.  It owns the
process-per-worker adapter pool, a durable provider/thread/effect boundary,
and a ``Popen``-shaped event facade so the launcher's established output
reconciliation can consume App Server notifications without learning the
JSON-RPC protocol.

The durable row is fail-closed.  Once App Server acceptance is recorded, a
missing or unhealthy server may not silently switch the application actor to
the CLI.  Provider thread identity is retained after a completed turn so a
repair turn resumes the same thread.  This state is not submission authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from applypilot.apply import agent_runtime as agent_runtime_mod
from applypilot.apply.application_plan import ApplicationPlan, render_application_plan_delta
from applypilot.apply.capabilities import CapabilityRegistry, capability_names_for_server
from applypilot.apply.codex_app_server import (
    CodexAppServerAdapter,
    CodexAppServerExecutionError,
    CodexAppServerTimeout,
)
from applypilot.apply.runtime_cell import (
    RuntimeCellExecutionState,
    RuntimeCellRequest,
    RuntimeCellTurn,
)

ConnectionProvider = Callable[[], sqlite3.Connection]
AdapterFactory = Callable[[], CodexAppServerAdapter]

_EFFECT_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "dynamic_tool_call",
        "file_change",
        "mcp_tool_call",
        "tool_call",
    }
)


class DurableAppServerStateError(RuntimeError):
    """A durable App Server binding is stale, incomplete, or conflicting."""


@dataclass(frozen=True, slots=True)
class DurableAppServerState:
    actor_id: str
    attempt_id: str
    provider_session_id: str | None
    provider_turn_id: str | None
    request_accepted: bool
    tool_or_effect_started: bool
    submit_started: bool
    status: str

    @property
    def execution_state(self) -> RuntimeCellExecutionState:
        bound = (
            "codex-app-server"
            if (self.request_accepted or self.tool_or_effect_started or self.submit_started)
            else None
        )
        return RuntimeCellExecutionState(
            request_accepted=self.request_accepted,
            tool_or_effect_started=self.tool_or_effect_started,
            submit_started=self.submit_started,
            bound_backend=bound,
        )


class DurableAppServerStateStore:
    """SQLite-backed provider identity and effect boundary per application."""

    def __init__(
        self,
        connection_provider: ConnectionProvider,
        *,
        close_connections: bool = False,
    ) -> None:
        self._connection_provider = connection_provider
        self._close_connections = close_connections

    def _connection(self) -> sqlite3.Connection:
        connection = self._connection_provider()
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection_provider must return sqlite3.Connection")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _release(self, connection: sqlite3.Connection) -> None:
        if self._close_connections:
            connection.close()

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_server_runtime_bindings (
                actor_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                provider_session_id TEXT,
                provider_turn_id TEXT,
                request_accepted INTEGER NOT NULL CHECK(request_accepted IN (0,1)),
                tool_or_effect_started INTEGER NOT NULL
                    CHECK(tool_or_effect_started IN (0,1)),
                submit_started INTEGER NOT NULL CHECK(submit_started IN (0,1)),
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> DurableAppServerState | None:
        if row is None:
            return None
        return DurableAppServerState(
            actor_id=str(row["actor_id"]),
            attempt_id=str(row["attempt_id"]),
            provider_session_id=row["provider_session_id"],
            provider_turn_id=row["provider_turn_id"],
            request_accepted=bool(row["request_accepted"]),
            tool_or_effect_started=bool(row["tool_or_effect_started"]),
            submit_started=bool(row["submit_started"]),
            status=str(row["status"]),
        )

    def load(self, actor_id: str, attempt_id: str) -> DurableAppServerState | None:
        connection = self._connection()
        try:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT * FROM app_server_runtime_bindings WHERE actor_id=?",
                (actor_id,),
            ).fetchone()
        finally:
            self._release(connection)
        state = self._from_row(row)
        if state is not None and state.attempt_id != attempt_id:
            raise DurableAppServerStateError("App Server actor binding belongs to a different attempt")
        return state

    def peek(self, actor_id: str, attempt_id: str) -> DurableAppServerState | None:
        """Read an existing binding without creating schema while feature-off."""

        connection = self._connection()
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_server_runtime_bindings'"
            ).fetchone()
            row = (
                connection.execute(
                    "SELECT * FROM app_server_runtime_bindings WHERE actor_id=?",
                    (actor_id,),
                ).fetchone()
                if exists is not None
                else None
            )
        finally:
            self._release(connection)
        state = self._from_row(row)
        if state is not None and state.attempt_id != attempt_id:
            raise DurableAppServerStateError("App Server actor binding belongs to a different attempt")
        return state

    def execution_state(self, actor_id: str, attempt_id: str) -> RuntimeCellExecutionState:
        state = self.load(actor_id, attempt_id)
        if state is None:
            return RuntimeCellExecutionState(
                request_accepted=False,
                tool_or_effect_started=False,
                submit_started=False,
                bound_backend=None,
            )
        return state.execution_state

    def record_accepted(
        self,
        *,
        actor_id: str,
        attempt_id: str,
        provider_session_id: str,
        provider_turn_id: str,
        submit_started: bool,
    ) -> DurableAppServerState:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (actor_id, attempt_id, provider_session_id, provider_turn_id)
        ):
            raise ValueError("accepted App Server identity fields are required")
        connection = self._connection()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = self._from_row(
                connection.execute(
                    "SELECT * FROM app_server_runtime_bindings WHERE actor_id=?",
                    (actor_id,),
                ).fetchone()
            )
            if current is not None and (
                current.attempt_id != attempt_id
                or (current.provider_session_id is not None and current.provider_session_id != provider_session_id)
                or current.submit_started
                and not submit_started
            ):
                raise DurableAppServerStateError("App Server provider binding or submit state changed")
            connection.execute(
                """
                INSERT INTO app_server_runtime_bindings(
                    actor_id, attempt_id, provider_session_id, provider_turn_id,
                    request_accepted, tool_or_effect_started, submit_started,
                    status, updated_at
                ) VALUES(?,?,?,?,1,?,?,?,?)
                ON CONFLICT(actor_id) DO UPDATE SET
                    provider_session_id=excluded.provider_session_id,
                    provider_turn_id=excluded.provider_turn_id,
                    request_accepted=1,
                    tool_or_effect_started=app_server_runtime_bindings.tool_or_effect_started,
                    submit_started=MAX(
                        app_server_runtime_bindings.submit_started,
                        excluded.submit_started
                    ),
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    actor_id,
                    attempt_id,
                    provider_session_id,
                    provider_turn_id,
                    int(current.tool_or_effect_started) if current is not None else 0,
                    int(submit_started),
                    "receipt_only" if submit_started else "running",
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
            state = self._from_row(
                connection.execute(
                    "SELECT * FROM app_server_runtime_bindings WHERE actor_id=?",
                    (actor_id,),
                ).fetchone()
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            self._release(connection)
        assert state is not None
        return state

    def record_uncertain_acceptance(
        self,
        *,
        actor_id: str,
        attempt_id: str,
        execution_state: RuntimeCellExecutionState,
    ) -> DurableAppServerState:
        """Persist an accepted failure even when no provider id was returned."""

        if execution_state.bound_backend != "codex-app-server":
            raise ValueError("uncertain App Server acceptance must stay bound")
        connection = self._connection()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = self._from_row(
                connection.execute(
                    "SELECT * FROM app_server_runtime_bindings WHERE actor_id=?",
                    (actor_id,),
                ).fetchone()
            )
            if current is not None and current.attempt_id != attempt_id:
                raise DurableAppServerStateError("App Server actor binding belongs to a different attempt")
            connection.execute(
                """
                INSERT INTO app_server_runtime_bindings(
                    actor_id, attempt_id, provider_session_id, provider_turn_id,
                    request_accepted, tool_or_effect_started, submit_started,
                    status, updated_at
                ) VALUES(?,?,NULL,NULL,?,?,?,?,?)
                ON CONFLICT(actor_id) DO UPDATE SET
                    request_accepted=MAX(
                        app_server_runtime_bindings.request_accepted,
                        excluded.request_accepted
                    ),
                    tool_or_effect_started=MAX(
                        app_server_runtime_bindings.tool_or_effect_started,
                        excluded.tool_or_effect_started
                    ),
                    submit_started=MAX(
                        app_server_runtime_bindings.submit_started,
                        excluded.submit_started
                    ),
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    actor_id,
                    attempt_id,
                    int(execution_state.request_accepted),
                    int(execution_state.tool_or_effect_started),
                    int(execution_state.submit_started),
                    "receipt_only" if execution_state.submit_started else "parked",
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
            state = self._from_row(
                connection.execute(
                    "SELECT * FROM app_server_runtime_bindings WHERE actor_id=?",
                    (actor_id,),
                ).fetchone()
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            self._release(connection)
        assert state is not None
        return state

    def record_event(
        self,
        *,
        actor_id: str,
        attempt_id: str,
        provider_turn_id: str,
        event: Mapping[str, object],
    ) -> DurableAppServerState:
        item = event.get("item")
        effect_started = isinstance(item, Mapping) and item.get("type") in _EFFECT_ITEM_TYPES
        terminal = event.get("type") == "turn.completed"
        connection = self._connection()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = self._from_row(
                connection.execute(
                    "SELECT * FROM app_server_runtime_bindings WHERE actor_id=?",
                    (actor_id,),
                ).fetchone()
            )
            if (
                current is None
                or current.attempt_id != attempt_id
                or current.provider_turn_id != provider_turn_id
                or not current.request_accepted
            ):
                raise DurableAppServerStateError("App Server event does not bind the active durable turn")
            connection.execute(
                """
                UPDATE app_server_runtime_bindings
                SET tool_or_effect_started=MAX(tool_or_effect_started, ?),
                    status=?, updated_at=?
                WHERE actor_id=? AND attempt_id=? AND provider_turn_id=?
                """,
                (
                    int(effect_started),
                    ("receipt_only" if current.submit_started else "completed" if terminal else "running"),
                    datetime.now(UTC).isoformat(),
                    actor_id,
                    attempt_id,
                    provider_turn_id,
                ),
            )
            connection.commit()
            state = self._from_row(
                connection.execute(
                    "SELECT * FROM app_server_runtime_bindings WHERE actor_id=?",
                    (actor_id,),
                ).fetchone()
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            self._release(connection)
        assert state is not None
        return state


class AppServerRuntimePool:
    """Lazily keep one warm App Server process for each launcher worker."""

    def __init__(self, adapter_factory: AdapterFactory = CodexAppServerAdapter) -> None:
        self._adapter_factory = adapter_factory
        self._adapters: dict[int, CodexAppServerAdapter] = {}
        self._lock = threading.RLock()

    def adapter_for_worker(self, worker_id: int) -> CodexAppServerAdapter:
        if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id < 0:
            raise ValueError("worker_id must be a non-negative integer")
        with self._lock:
            adapter = self._adapters.get(worker_id)
            if adapter is None:
                adapter = self._adapter_factory()
                self._adapters[worker_id] = adapter
            return adapter

    def shutdown(self) -> None:
        with self._lock:
            adapters = tuple(self._adapters.values())
            self._adapters.clear()
        for adapter in adapters:
            try:
                adapter.shutdown()
            except (OSError, RuntimeError, TimeoutError, ValueError):
                # Each adapter performs bounded transport containment itself.
                pass


def _content_ref(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_ref_only_request(
    *,
    run_id: str,
    actor_id: str,
    attempt_id: str,
    phase: str,
    cwd: Path,
    model: str,
    prompt_contract: object,
    ats_context: object,
    application_context: object | None = None,
    plan: ApplicationPlan | None = None,
    previous_plan: ApplicationPlan | None = None,
    plan_shadow_enabled: bool = False,
    parent_provider_session_id: str | None = None,
) -> RuntimeCellRequest:
    """Build an App Server request containing only identities and digest refs."""

    context_refs = {
        "prompt_contract": _content_ref(prompt_contract),
        "ats_context": _content_ref(ats_context),
    }
    if application_context is not None:
        context_refs["application_context"] = _content_ref(application_context)
    prompt_parts = [
        "APPLYPILOT_RUNTIME_CELL_V1",
        f"phase={phase}",
        "Inputs are reference-only. Do not request browser handles, cookies, raw materials, paths, URLs, or SubmitAuthority.",
        "Approval and elicitation are unavailable. Host browser writes and Submit remain outside this request.",
        json.dumps(context_refs, sort_keys=True, separators=(",", ":")),
    ]
    if plan_shadow_enabled and plan is not None:
        if plan.attempt_id != attempt_id:
            raise ValueError("ApplicationPlan attempt does not match the runtime request")
        prompt_parts.append(render_application_plan_delta(plan, previous=previous_plan))
    return RuntimeCellRequest(
        run_id=run_id,
        actor_id=actor_id,
        attempt_id=attempt_id,
        phase=phase,
        prompt="\n".join(prompt_parts),
        cwd=cwd,
        model=model,
        context_refs=context_refs,
        parent_provider_session_id=parent_provider_session_id,
    )


def build_thread_config(
    mcp_config: Mapping[str, object],
    *,
    process_env: Mapping[str, str],
    server_env_names: Mapping[str, tuple[str, ...]] | None = None,
    enabled_tools: Mapping[str, tuple[str, ...]] | None = None,
    playwright_url: str | None = None,
) -> dict[str, object]:
    """Convert the existing MCP config into App Server thread overrides."""

    raw_servers = mcp_config.get("mcpServers")
    if not isinstance(raw_servers, Mapping):
        raise TypeError("MCP config does not contain mcpServers")
    env_names = server_env_names or {}
    tool_names = enabled_tools or {}
    servers: dict[str, object] = {}
    for raw_name, raw_config in raw_servers.items():
        name = str(raw_name)
        if not isinstance(raw_config, Mapping):
            raise TypeError(f"MCP server config is invalid: {name}")
        config = dict(raw_config)
        if name == "playwright" and playwright_url is not None:
            config = {"url": playwright_url}
        elif config.get("command") == "npx":
            args = list(config.get("args", ()))
            if platform.system() == "Windows":
                config["command"] = process_env.get(
                    "COMSPEC", os.environ.get("COMSPEC", "cmd.exe")
                )
                config["args"] = ["/d", "/s", "/c", "npx", *args]
            else:
                config["command"] = shutil.which("npx") or "npx"
                config["args"] = args
        selected_env = {key: process_env[key] for key in env_names.get(name, ()) if key in process_env}
        if selected_env:
            config["env"] = selected_env
        if name in tool_names:
            config["enabled_tools"] = list(tool_names[name])
        config["default_tools_approval_mode"] = "approve"
        servers[name] = config
    return {
        "features": {
            "shell_tool": False,
            "skill_mcp_dependency_install": False,
        },
        "web_search": "disabled",
        "mcp_servers": servers,
    }


class AppServerTurnProcess:
    """A narrow process facade over one App Server turn event stream."""

    def __init__(
        self,
        *,
        adapter: CodexAppServerAdapter,
        turn: RuntimeCellTurn,
        state_store: DurableAppServerStateStore,
        actor_id: str,
        attempt_id: str,
    ) -> None:
        self.adapter = adapter
        self.turn = turn
        self.state_store = state_store
        self.actor_id = actor_id
        self.attempt_id = attempt_id
        process = adapter.transport.process
        self.pid = int(process.pid) if process is not None else 0
        self.returncode: int | None = None
        self._stream: queue.Queue[object] = queue.Queue()
        self._stream_end = object()
        self._cancel_requested = threading.Event()
        self._consumer = threading.Thread(
            target=self._consume_events,
            name=f"applypilot-app-server-turn-{turn.provider_turn_id}",
            daemon=True,
        )
        self._consumer.start()
        self.stdout = self._lines()

    def _consume_events(self) -> None:
        try:
            for event in self.turn.events:
                self.state_store.record_event(
                    actor_id=self.actor_id,
                    attempt_id=self.attempt_id,
                    provider_turn_id=self.turn.provider_turn_id,
                    event=event,
                )
                self._stream.put(event)
        # Preserve every producer failure for the owning launcher thread;
        # otherwise a daemon-thread exception could be mistaken for clean EOF.
        except BaseException as exc:  # noqa: BLE001
            self._stream.put(exc)
        finally:
            self._stream.put(self._stream_end)

    def _lines(self) -> Iterator[str]:
        terminal = False
        cancel_deadline: float | None = None
        try:
            while True:
                if self._cancel_requested.is_set() and cancel_deadline is None:
                    cancel_deadline = time.monotonic() + self.adapter.drain_timeout
                wait_seconds = 0.25
                if cancel_deadline is not None:
                    remaining = cancel_deadline - time.monotonic()
                    if remaining <= 0:
                        raise CodexAppServerTimeout(
                            "App Server turn did not terminate after cancellation"
                        )
                    wait_seconds = min(wait_seconds, remaining)
                try:
                    item = self._stream.get(timeout=wait_seconds)
                except queue.Empty:
                    continue
                if item is self._stream_end:
                    break
                if isinstance(item, BaseException):
                    raise item
                if not isinstance(item, Mapping):
                    raise TypeError("App Server event stream yielded a non-mapping")
                event = item
                terminal = event.get("type") == "turn.completed"
                yield json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        except CodexAppServerExecutionError as exc:
            self.state_store.record_uncertain_acceptance(
                actor_id=self.actor_id,
                attempt_id=self.attempt_id,
                execution_state=exc.execution_state,
            )
            raise
        finally:
            self.returncode = 0 if terminal else -1

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise TimeoutError("App Server turn event stream has not terminated")
        return self.returncode

    def cancel(self) -> None:
        self._cancel_requested.set()
        self.adapter.cancel(self.turn.provider_turn_id)

    def drain(self, *, timeout: float) -> None:
        self.adapter.drain(self.turn.provider_turn_id, timeout=timeout)


def open_app_server_turn(
    *,
    adapter: CodexAppServerAdapter,
    state_store: DurableAppServerStateStore,
    request: RuntimeCellRequest,
    submit_started: bool,
) -> AppServerTurnProcess:
    """Start or resume one durable application thread and persist acceptance."""

    existing = state_store.load(request.actor_id, request.attempt_id)
    if existing is not None and existing.request_accepted:
        if not existing.provider_session_id:
            raise DurableAppServerStateError("accepted App Server request has no resumable provider session")
        if request.parent_provider_session_id != existing.provider_session_id:
            raise DurableAppServerStateError("App Server repair did not preserve the durable provider session")
    else:
        # Persist the ambiguous dispatch boundary before the first JSON-RPC
        # write.  If the launcher disappears after the provider accepts but
        # before its response is committed locally, recovery must park rather
        # than treating the request as pristine and starting the CLI.
        state_store.record_uncertain_acceptance(
            actor_id=request.actor_id,
            attempt_id=request.attempt_id,
            execution_state=RuntimeCellExecutionState(
                request_accepted=True,
                tool_or_effect_started=False,
                submit_started=submit_started,
                bound_backend="codex-app-server",
            ),
        )
    try:
        turn = adapter.resume(request) if request.parent_provider_session_id else adapter.start(request)
    except CodexAppServerExecutionError as exc:
        state_store.record_uncertain_acceptance(
            actor_id=request.actor_id,
            attempt_id=request.attempt_id,
            execution_state=exc.execution_state,
        )
        raise
    state_store.record_accepted(
        actor_id=request.actor_id,
        attempt_id=request.attempt_id,
        provider_session_id=turn.provider_session_id,
        provider_turn_id=turn.provider_turn_id,
        submit_started=submit_started,
    )
    if submit_started:
        adapter.mark_submit_started(turn.provider_turn_id)
    return AppServerTurnProcess(
        adapter=adapter,
        turn=turn,
        state_store=state_store,
        actor_id=request.actor_id,
        attempt_id=request.attempt_id,
    )


def open_configured_app_server_turn(
    *,
    adapter: CodexAppServerAdapter,
    state_store: DurableAppServerStateStore,
    run_id: str,
    actor_id: str,
    attempt_id: str,
    phase: str,
    cwd: Path,
    model: str,
    mcp_config: Mapping[str, object],
    process_env: Mapping[str, str],
    runtime_capabilities: CapabilityRegistry,
    playwright_env: Mapping[str, str],
    mailbox_server_name: str | None,
    mailbox_env: Mapping[str, str],
    credential_relay_authorized: bool,
    identity_relay_authorized: bool,
    playwright_url: str | None,
    prompt_contract: object,
    ats_context: object,
    application_context: object | None,
    plan: ApplicationPlan | None,
    previous_plan: ApplicationPlan | None,
    plan_shadow_enabled: bool,
    submit_started: bool,
) -> AppServerTurnProcess:
    """Configure one worker adapter and open its durable application turn."""

    server_env_names: dict[str, tuple[str, ...]] = {
        "playwright": tuple(playwright_env),
        "applypilot_control": tuple(agent_runtime_mod.CONTROL_REPORT_ENV_VARS),
        "applypilot_ats": tuple(agent_runtime_mod.APPLICATION_TOOL_ENV_VARS),
        "credential_relay": tuple(agent_runtime_mod.CREDENTIAL_RELAY_ENV_VARS),
    }
    if mailbox_server_name:
        server_env_names[mailbox_server_name] = tuple(mailbox_env)
    enabled_tools: dict[str, tuple[str, ...]] = {
        server: tuple(capability_names_for_server(runtime_capabilities, server))
        for server in ("playwright", "applypilot_control", "applypilot_ats")
    }
    if credential_relay_authorized or identity_relay_authorized:
        enabled_tools["credential_relay"] = tuple(
            tool
            for tool, enabled in (
                ("fill_ats_credentials", credential_relay_authorized),
                ("fill_protected_identifier", identity_relay_authorized),
            )
            if enabled
        )
    adapter.configure_thread(
        build_thread_config(
            mcp_config,
            process_env=process_env,
            server_env_names=server_env_names,
            enabled_tools=enabled_tools,
            playwright_url=playwright_url,
        )
    )
    existing = state_store.load(actor_id, attempt_id)
    request = build_ref_only_request(
        run_id=run_id,
        actor_id=actor_id,
        attempt_id=attempt_id,
        phase=phase,
        cwd=cwd,
        model=model,
        prompt_contract=prompt_contract,
        ats_context=ats_context,
        application_context=application_context,
        plan=plan,
        previous_plan=previous_plan,
        plan_shadow_enabled=plan_shadow_enabled,
        parent_provider_session_id=(existing.provider_session_id if existing is not None else None),
    )
    return open_app_server_turn(
        adapter=adapter,
        state_store=state_store,
        request=request,
        submit_started=submit_started,
    )


__all__ = [
    "AppServerRuntimePool",
    "AppServerTurnProcess",
    "DurableAppServerState",
    "DurableAppServerStateError",
    "DurableAppServerStateStore",
    "build_ref_only_request",
    "build_thread_config",
    "open_app_server_turn",
    "open_configured_app_server_turn",
]
