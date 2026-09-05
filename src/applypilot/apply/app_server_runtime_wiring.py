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
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from applypilot.apply.agent_runtime import (
    AgentRuntimeConfiguration,
    resolve_agent_runtime_configuration,
)
from applypilot.apply.application_plan import ApplicationPlan, render_application_plan_delta
from applypilot.apply.capabilities import CapabilityRegistry, capability_names_for_server
from applypilot.apply.codex_app_server import (
    CodexAppServerAdapter,
    CodexAppServerExecutionError,
    CodexAppServerTimeout,
    DynamicToolSpec,
    DynamicToolSurface,
    app_server_item_starts_effect,
)
from applypilot.apply.contracts import ToolSpec
from applypilot.apply.runtime_cell import (
    RuntimeCellExecutionState,
    RuntimeCellRequest,
    RuntimeCellTurn,
)

ConnectionProvider = Callable[[], sqlite3.Connection]
DatabasePathProvider = Callable[[], Path]
AdapterFactory = Callable[[], CodexAppServerAdapter]
AdapterFailureCallback = Callable[[CodexAppServerAdapter], None]

_READ_ONLY_PLAYWRIGHT_TOOLS = (
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_wait_for",
    "browser_console_messages",
    "browser_network_requests",
)
_LOOPBACK_PLAYWRIGHT_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def dynamic_tools_from_registry(
    registry: CapabilityRegistry,
    handlers: Mapping[str, Callable[[Mapping[str, object]], Mapping[str, object]]],
    *,
    declarations: Iterable[ToolSpec] = (),
) -> tuple[DynamicToolSpec, ...]:
    """Bind canonical read-only declarations to explicit in-process handlers.

    The registry remains the source of names, schemas, policy and timeouts.
    Handlers only provide implementation; an extra handler cannot create a
    capability that the compiled ToolBroker surface did not admit.
    """

    declared = {tool.name: tool for tool in registry.values()}
    descriptor_source = {tool.name: tool for tool in declarations}
    unknown = sorted(set(handlers).difference(declared))
    if unknown:
        raise ValueError(
            "dynamic tool handlers are not present in the compiled surface: "
            + ", ".join(unknown)
        )
    specs: list[DynamicToolSpec] = []
    for name in sorted(handlers):
        admitted_tool = declared[name]
        tool = descriptor_source.get(name, admitted_tool)
        if not isinstance(tool, ToolSpec) or not isinstance(admitted_tool, ToolSpec):
            raise TypeError("dynamic tool declarations must be ToolSpec instances")
        for field in ("effect_class", "authority", "sensitivity"):
            if getattr(tool, field) != getattr(admitted_tool, field):
                raise ValueError(f"dynamic tool {name} declaration does not match compiled policy")
        if str(tool.effect_class).casefold() != "read":
            raise ValueError(f"dynamic tool {name} is not read-only")
        if tool.authority.casefold() not in {"none", "read", "observation", "advisory"}:
            raise ValueError(f"dynamic tool {name} has unsupported authority")
        if tool.sensitivity.casefold() not in {"normal", "low", "public"}:
            raise ValueError(f"dynamic tool {name} has unsupported sensitivity")
        specs.append(
            DynamicToolSpec(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                handler=handlers[name],
                defer_loading=tool.defer_loading,
                timeout=float(tool.timeout_seconds or 2.0),
            )
        )
    return tuple(specs)


def dynamic_tool_surface_digest(tools: tuple[DynamicToolSpec, ...]) -> str | None:
    """Return the exact App Server descriptor digest without starting a process."""

    return DynamicToolSurface(tools).digest if tools else None


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
        database_path_provider: DatabasePathProvider | None = None,
    ) -> None:
        self._connection_provider = connection_provider
        self._close_connections = close_connections
        self._database_path_provider = database_path_provider

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

    def _prepare_write_connection(self) -> None:
        if self._database_path_provider is None:
            return
        database_path = Path(self._database_path_provider())
        database_path.parent.mkdir(parents=True, exist_ok=True)

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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_server_dynamic_surfaces (
                actor_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                surface_digest TEXT NOT NULL,
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
        self._prepare_write_connection()
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

        if self._database_path_provider is not None:
            database_path = Path(self._database_path_provider())
            if not database_path.exists():
                return None
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
    ) -> DurableAppServerState:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (actor_id, attempt_id, provider_session_id, provider_turn_id)
        ):
            raise ValueError("accepted App Server identity fields are required")
        self._prepare_write_connection()
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
            ):
                raise DurableAppServerStateError(
                    "App Server provider binding changed or is already receipt-only"
                )
            connection.execute(
                """
                INSERT INTO app_server_runtime_bindings(
                    actor_id, attempt_id, provider_session_id, provider_turn_id,
                    request_accepted, tool_or_effect_started, submit_started,
                    status, updated_at
                ) VALUES(?,?,?,?,1,?,0,?,?)
                ON CONFLICT(actor_id) DO UPDATE SET
                    provider_session_id=excluded.provider_session_id,
                    provider_turn_id=excluded.provider_turn_id,
                    request_accepted=1,
                    tool_or_effect_started=app_server_runtime_bindings.tool_or_effect_started,
                    submit_started=app_server_runtime_bindings.submit_started,
                    status=CASE
                        WHEN app_server_runtime_bindings.submit_started=1
                            THEN 'receipt_only'
                        ELSE excluded.status
                    END,
                    updated_at=excluded.updated_at
                """,
                (
                    actor_id,
                    attempt_id,
                    provider_session_id,
                    provider_turn_id,
                    int(current.tool_or_effect_started) if current is not None else 0,
                    "running",
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

    def dynamic_surface_digest(self, actor_id: str, attempt_id: str) -> str | None:
        self._prepare_write_connection()
        connection = self._connection()
        try:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT attempt_id, surface_digest FROM app_server_dynamic_surfaces WHERE actor_id=?",
                (actor_id,),
            ).fetchone()
        finally:
            self._release(connection)
        if row is None:
            return None
        if row["attempt_id"] != attempt_id:
            raise DurableAppServerStateError("dynamic tool surface belongs to a different attempt")
        return str(row["surface_digest"])

    def bind_dynamic_surface(
        self,
        *,
        actor_id: str,
        attempt_id: str,
        surface_digest: str,
    ) -> None:
        if not surface_digest.startswith("sha256:"):
            raise ValueError("dynamic tool surface digest is invalid")
        self._prepare_write_connection()
        connection = self._connection()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempt_id, surface_digest FROM app_server_dynamic_surfaces WHERE actor_id=?",
                (actor_id,),
            ).fetchone()
            if row is not None and (
                row["attempt_id"] != attempt_id or row["surface_digest"] != surface_digest
            ):
                raise DurableAppServerStateError(
                    "dynamic tool surface changed for the durable application thread"
                )
            connection.execute(
                """
                INSERT INTO app_server_dynamic_surfaces(
                    actor_id, attempt_id, surface_digest, updated_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(actor_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (actor_id, attempt_id, surface_digest, datetime.now(UTC).isoformat()),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            self._release(connection)

    def record_submit_started(
        self,
        *,
        actor_id: str,
        attempt_id: str,
        provider_turn_id: str,
    ) -> DurableAppServerState:
        """Persist only an explicit host-observed Submit effect, irreversibly."""

        self._prepare_write_connection()
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
                raise DurableAppServerStateError(
                    "Submit effect does not bind the accepted durable App Server turn"
                )
            connection.execute(
                """
                UPDATE app_server_runtime_bindings
                SET submit_started=1, status='receipt_only', updated_at=?
                WHERE actor_id=? AND attempt_id=? AND provider_turn_id=?
                """,
                (
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
        if execution_state.submit_started:
            raise ValueError("submit_started requires an explicit durable host observation")
        self._prepare_write_connection()
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
                ) VALUES(?,?,NULL,NULL,?,?,0,?,?)
                ON CONFLICT(actor_id) DO UPDATE SET
                    request_accepted=MAX(
                        app_server_runtime_bindings.request_accepted,
                        excluded.request_accepted
                    ),
                    tool_or_effect_started=MAX(
                        app_server_runtime_bindings.tool_or_effect_started,
                        excluded.tool_or_effect_started
                    ),
                    submit_started=app_server_runtime_bindings.submit_started,
                    status=CASE
                        WHEN app_server_runtime_bindings.submit_started=1
                            THEN 'receipt_only'
                        ELSE excluded.status
                    END,
                    updated_at=excluded.updated_at
                """,
                (
                    actor_id,
                    attempt_id,
                    int(execution_state.request_accepted),
                    int(execution_state.tool_or_effect_started),
                    "parked",
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
        read_only_dynamic_tools: frozenset[str] = frozenset(),
    ) -> DurableAppServerState:
        item = event.get("item")
        effect_started = isinstance(item, Mapping) and app_server_item_starts_effect(
            item,
            read_only_dynamic_tools=read_only_dynamic_tools,
        )
        terminal_status: str | None = None
        if event.get("type") == "turn.completed":
            raw_status = event.get("status")
            if raw_status not in {"completed", "failed", "interrupted"}:
                raise DurableAppServerStateError(
                    "terminal App Server event has no valid provider status"
                )
            terminal_status = str(raw_status)
        self._prepare_write_connection()
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
                    "receipt_only"
                    if current.submit_started
                    else terminal_status or "running",
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
        self._cleanup_threads: set[threading.Thread] = set()
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

    def adapter_for_worker_surface(
        self,
        worker_id: int,
        dynamic_surface_digest: str | None,
    ) -> CodexAppServerAdapter:
        """Reuse only adapters initialized for the same Dynamic Tools surface."""

        if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id < 0:
            raise ValueError("worker_id must be a non-negative integer")
        stale: CodexAppServerAdapter | None = None
        with self._lock:
            adapter = self._adapters.get(worker_id)
            if (
                adapter is not None
                and getattr(adapter, "dynamic_surface_digest", None)
                != dynamic_surface_digest
            ):
                stale = self._adapters.pop(worker_id)
                adapter = None
            if adapter is None:
                adapter = self._adapter_factory()
                self._adapters[worker_id] = adapter
        if stale is not None:
            self._schedule_shutdown(stale)
        return adapter

    def shutdown(self) -> None:
        with self._lock:
            adapters = tuple(self._adapters.values())
            self._adapters.clear()
            cleanup_threads = tuple(self._cleanup_threads)
        for adapter in adapters:
            self._shutdown_adapter(adapter)
        for cleanup_thread in cleanup_threads:
            cleanup_thread.join(timeout=5)

    @staticmethod
    def _shutdown_adapter(adapter: CodexAppServerAdapter) -> None:
        try:
            adapter.shutdown()
        except (OSError, RuntimeError, TimeoutError, ValueError):
            # Each adapter performs bounded transport containment itself.
            pass

    def _detach_worker(self, worker_id: int, adapter: CodexAppServerAdapter) -> bool:
        with self._lock:
            if self._adapters.get(worker_id) is not adapter:
                return False
            self._adapters.pop(worker_id, None)
            return True

    def evict_worker(self, worker_id: int, adapter: CodexAppServerAdapter) -> None:
        """Remove and close a worker transport after a non-terminal anomaly."""

        if self._detach_worker(worker_id, adapter):
            self._shutdown_adapter(adapter)

    def evict_worker_async(
        self,
        worker_id: int,
        adapter: CodexAppServerAdapter,
    ) -> threading.Thread | None:
        """Detach immediately; contain the old transport outside the caller path."""

        if not self._detach_worker(worker_id, adapter):
            return None

        return self._schedule_shutdown(adapter)

    def _schedule_shutdown(
        self,
        adapter: CodexAppServerAdapter,
    ) -> threading.Thread:
        def shutdown_detached_adapter() -> None:
            try:
                self._shutdown_adapter(adapter)
            finally:
                with self._lock:
                    self._cleanup_threads.discard(threading.current_thread())

        cleanup_thread = threading.Thread(
            target=shutdown_detached_adapter,
            name=f"applypilot-app-server-cleanup-{id(adapter):x}",
            daemon=True,
        )
        with self._lock:
            self._cleanup_threads.add(cleanup_thread)
        cleanup_thread.start()
        return cleanup_thread


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
    reasoning_effort: str = "high",
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
        "SHADOW_OBSERVATION_ONLY",
        f"phase={phase}",
        "Inputs are reference-only. Do not request browser handles, cookies, raw materials, paths, URLs, or SubmitAuthority.",
        "This turn is non-authoritative and read-only. Do not navigate, write page state, or claim an application outcome.",
        "Approval and elicitation are unavailable. All browser writes and Submit remain outside this request.",
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
        reasoning_effort=reasoning_effort,
        parent_provider_session_id=parent_provider_session_id,
    )


def _validated_loopback_playwright_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("App Server Playwright endpoint has an invalid port") from exc
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "http"
        or host not in _LOOPBACK_PLAYWRIGHT_HOSTS
        or port is None
        or parsed.path != "/mcp"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "App Server Playwright endpoint must be http://<loopback>:<port>/mcp"
        )
    rendered_host = f"[{host}]" if host == "::1" else host
    return f"http://{rendered_host}:{port}/mcp"


def build_thread_config(
    mcp_config: Mapping[str, object],
    *,
    enabled_tools: Mapping[str, tuple[str, ...]] | None = None,
    playwright_url: str | None = None,
) -> dict[str, object]:
    """Build a Playwright-only App Server config without per-turn env values."""

    raw_servers = mcp_config.get("mcpServers")
    if not isinstance(raw_servers, Mapping):
        raise TypeError("MCP config does not contain mcpServers")
    tool_names = enabled_tools or {}
    servers: dict[str, object] = {}
    # The existing CLI config may contain user-controlled launcher/extra args,
    # storage-state paths, signed URLs, or secrets.  Never copy any part of it;
    # its presence is checked only to preserve the input schema contract.
    if "playwright" in raw_servers and not isinstance(raw_servers["playwright"], Mapping):
        raise TypeError("MCP server config is invalid: playwright")
    if playwright_url is not None:
        requested = frozenset(tool_names.get("playwright", ()))
        read_only_tools = [name for name in _READ_ONLY_PLAYWRIGHT_TOOLS if name in requested]
        if read_only_tools:
            servers["playwright"] = {
                "url": _validated_loopback_playwright_url(playwright_url),
                "enabled_tools": read_only_tools,
                "default_tools_approval_mode": "approve",
            }
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
        read_only_dynamic_tools: frozenset[str] = frozenset(),
        on_transport_failure: AdapterFailureCallback | None = None,
    ) -> None:
        self.adapter = adapter
        self.turn = turn
        self.state_store = state_store
        self.actor_id = actor_id
        self.attempt_id = attempt_id
        self.read_only_dynamic_tools = read_only_dynamic_tools
        self.on_transport_failure = on_transport_failure
        process = adapter.transport.process
        self.pid = int(process.pid) if process is not None else 0
        self.returncode: int | None = None
        self._stream: queue.Queue[object] = queue.Queue()
        self._stream_end = object()
        self._cancel_requested = threading.Event()
        self._failure_contained = threading.Event()
        self.terminal_status: str | None = None
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
                    read_only_dynamic_tools=self.read_only_dynamic_tools,
                )
                self._stream.put(event)
        # Preserve every producer failure for the owning launcher thread;
        # otherwise a daemon-thread exception could be mistaken for clean EOF.
        except BaseException as exc:  # noqa: BLE001
            self._stream.put(exc)
        finally:
            self._stream.put(self._stream_end)

    def _lines(self) -> Iterator[str]:
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
                if event.get("type") == "turn.completed":
                    status = event.get("status")
                    if status not in {"completed", "failed", "interrupted"}:
                        raise CodexAppServerExecutionError(
                            "App Server terminal event has no valid provider status",
                            execution_state=self.state_store.execution_state(
                                self.actor_id, self.attempt_id
                            ),
                        )
                    self.terminal_status = str(status)
                yield json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            if self.terminal_status is None:
                raise CodexAppServerExecutionError(
                    "App Server event stream ended before provider turn terminal",
                    execution_state=self.state_store.execution_state(
                        self.actor_id, self.attempt_id
                    ),
                )
        except CodexAppServerExecutionError as exc:
            try:
                self.state_store.record_uncertain_acceptance(
                    actor_id=self.actor_id,
                    attempt_id=self.attempt_id,
                    execution_state=exc.execution_state,
                )
            finally:
                if self.terminal_status is None:
                    self._contain_nonterminal_failure()
            raise
        except BaseException:
            if self.terminal_status is None:
                self._contain_nonterminal_failure()
            raise
        finally:
            if self.terminal_status == "completed":
                self.returncode = 0
            elif self.terminal_status == "interrupted":
                self.returncode = -1
            else:
                self.returncode = 1

    def _contain_nonterminal_failure(self) -> None:
        if self._failure_contained.is_set():
            return
        self._failure_contained.set()
        try:
            self.adapter.cancel(self.turn.provider_turn_id)
        except (KeyError, OSError, RuntimeError, TimeoutError, ValueError):
            pass
        try:
            self.adapter.drain(
                self.turn.provider_turn_id,
                timeout=self.adapter.drain_timeout,
            )
        except (KeyError, OSError, RuntimeError, TimeoutError, ValueError):
            pass
        if self.on_transport_failure is not None:
            self.on_transport_failure(self.adapter)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise TimeoutError("App Server turn event stream has not terminated")
        return self.returncode

    def cancel(self) -> None:
        self._cancel_requested.set()
        self.adapter.cancel(self.turn.provider_turn_id)

    def steer(self, prompt: str, *, expected_turn_id: str) -> None:
        """Steer this exact provider turn; stale controller state fails closed."""

        self.adapter.steer(
            self.turn.provider_turn_id,
            expected_turn_id=expected_turn_id,
            prompt=prompt,
        )

    def drain(self, *, timeout: float) -> None:
        self.adapter.drain(self.turn.provider_turn_id, timeout=timeout)


def open_app_server_turn(
    *,
    adapter: CodexAppServerAdapter,
    state_store: DurableAppServerStateStore,
    request: RuntimeCellRequest,
    read_only_dynamic_tools: frozenset[str] = frozenset(),
    on_transport_failure: AdapterFailureCallback | None = None,
) -> AppServerTurnProcess:
    """Start or resume one durable application thread and persist acceptance."""

    if request.phase == "submit":
        raise ValueError("submit phase is not supported by the App Server runtime")

    # This model/list preflight is deliberately before the durable ambiguous
    # dispatch boundary. Unsupported App Server/model/effort combinations can
    # therefore degrade without pretending that a provider turn was accepted.
    adapter.validate_configuration(request)

    existing = state_store.load(request.actor_id, request.attempt_id)
    if existing is not None and existing.request_accepted:
        if existing.submit_started:
            raise DurableAppServerStateError(
                "App Server application is receipt-only after Submit started"
            )
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
                submit_started=False,
                bound_backend="codex-app-server",
            ),
        )
    try:
        turn = adapter.resume(request) if request.parent_provider_session_id else adapter.start(request)
    except CodexAppServerExecutionError as exc:
        try:
            state_store.record_uncertain_acceptance(
                actor_id=request.actor_id,
                attempt_id=request.attempt_id,
                execution_state=exc.execution_state,
            )
        finally:
            if on_transport_failure is not None:
                on_transport_failure(adapter)
        raise
    state_store.record_accepted(
        actor_id=request.actor_id,
        attempt_id=request.attempt_id,
        provider_session_id=turn.provider_session_id,
        provider_turn_id=turn.provider_turn_id,
    )
    return AppServerTurnProcess(
        adapter=adapter,
        turn=turn,
        state_store=state_store,
        actor_id=request.actor_id,
        attempt_id=request.attempt_id,
        read_only_dynamic_tools=read_only_dynamic_tools,
        on_transport_failure=on_transport_failure,
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
    runtime_capabilities: CapabilityRegistry,
    playwright_url: str | None,
    prompt_contract: object,
    ats_context: object,
    application_context: object | None,
    plan: ApplicationPlan | None,
    previous_plan: ApplicationPlan | None,
    plan_shadow_enabled: bool,
    dynamic_tools: tuple[DynamicToolSpec, ...] = (),
    on_transport_failure: AdapterFailureCallback | None = None,
    workload_class: str | None = None,
    reasoning_efforts: Mapping[str, str] | None = None,
    resolved_configuration: AgentRuntimeConfiguration | None = None,
    runtime_metadata: dict[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppServerTurnProcess:
    """Configure one worker adapter and open its durable application turn."""

    if phase == "submit":
        raise ValueError("submit phase is not supported by the App Server runtime")

    runtime_configuration = resolved_configuration or resolve_agent_runtime_configuration(
        "codex",
        model,
        workload_class=workload_class or phase,
        reasoning_efforts=reasoning_efforts,
        environ=environ,
    )
    if runtime_configuration.backend != "codex":
        raise ValueError("App Server requires a resolved Codex runtime configuration")
    if runtime_configuration.model != model:
        raise ValueError("resolved App Server model does not match the requested model")

    dynamic_enabled = os.getenv("APPLYPILOT_CODEX_DYNAMIC_TOOLS_ENABLED", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    surface = DynamicToolSurface(dynamic_tools) if dynamic_enabled and dynamic_tools else None
    configure_dynamic_tools = getattr(adapter, "configure_dynamic_tools", None)
    if callable(configure_dynamic_tools):
        configure_dynamic_tools(surface)
    elif surface is not None:
        raise DurableAppServerStateError(
            "configured App Server adapter does not support dynamic tools"
        )

    enabled_tools: dict[str, tuple[str, ...]] = {
        "playwright": tuple(capability_names_for_server(runtime_capabilities, "playwright"))
    }
    adapter.configure_thread(
        build_thread_config(
            mcp_config,
            enabled_tools=enabled_tools,
            playwright_url=playwright_url,
        )
    )
    existing = state_store.load(actor_id, attempt_id)
    if existing is not None and existing.provider_session_id is not None:
        stored_digest = state_store.dynamic_surface_digest(actor_id, attempt_id)
        expected_digest = surface.digest if surface is not None else None
        if stored_digest != expected_digest:
            raise DurableAppServerStateError(
                "configured dynamic tool surface does not match the durable thread"
            )
        if stored_digest is not None:
            adapter.bind_dynamic_surface_digest(actor_id, stored_digest)
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
        reasoning_effort=runtime_configuration.reasoning.value,
    )
    configuration_validation = adapter.validate_configuration(request)
    if runtime_metadata is not None:
        runtime_metadata["runtime_configuration"] = runtime_configuration.as_dict()
        runtime_metadata["app_server_configuration_validation"] = configuration_validation
    process = open_app_server_turn(
        adapter=adapter,
        state_store=state_store,
        request=request,
        read_only_dynamic_tools=(surface.names if surface is not None else frozenset()),
        on_transport_failure=on_transport_failure,
    )
    if surface is not None:
        state_store.bind_dynamic_surface(
            actor_id=actor_id,
            attempt_id=attempt_id,
            surface_digest=surface.digest,
        )
    return process


__all__ = [
    "AppServerRuntimePool",
    "AppServerTurnProcess",
    "DurableAppServerState",
    "DurableAppServerStateError",
    "DurableAppServerStateStore",
    "build_ref_only_request",
    "build_thread_config",
    "dynamic_tool_surface_digest",
    "dynamic_tools_from_registry",
    "open_app_server_turn",
    "open_configured_app_server_turn",
]
