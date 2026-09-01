"""SQLite-backed subprocess lifecycle and restart reconciliation.

The durable row is the authority for turn identity.  The wrapped subprocess
runtime owns only local pipes and process handles; provider sessions and the
in-memory ``_runs`` map are never recovery authority.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from applypilot.apply.agent_runtime import (
    SubprocessAgentRuntime,
    SubprocessLaunchSpec,
    SubprocessParentIdentity,
)
from applypilot.storage import runtime_control

logger = logging.getLogger(__name__)

ConnectionProvider = Callable[[], sqlite3.Connection]
ProcessIdentityLookup = Callable[[int], tuple[int, int] | None]
ResumeAuthorizer = Callable[
    ["DurableLaunchIntent", runtime_control.AgentRuntimeTurn],
    bool,
]

RecoveryDisposition = Literal[
    "none",
    "live_owner",
    "recovery_required",
    "receipt_only",
    "blocked",
]

class DurableRuntimeError(RuntimeError):
    """Base failure for the durable subprocess facade."""


class DurableRuntimeBlocked(DurableRuntimeError):
    """A persisted precondition prevented a new process from being started."""


@dataclass(frozen=True, slots=True)
class DurableLaunchIntent:
    """Persistable launch identity; prompt and environment remain transient."""

    spec: SubprocessLaunchSpec
    runtime_backend: str
    resume_mode: Literal["root", "resume", "receipt_only"]
    tool_surface_hash: str
    prompt_contract_hash: str
    checkpoint_id: str | None = None
    model: str | None = None
    provider_session_id: str | None = None
    recovery_authorization_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for name in ("runtime_backend", "tool_surface_hash", "prompt_contract_hash"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        parent = self.spec.parent_run_id
        if parent is None:
            if self.resume_mode != "root" or self.checkpoint_id is not None:
                raise ValueError("root intent requires root mode and no checkpoint")
        elif (
            self.resume_mode == "root"
            or not self.checkpoint_id
            or not self.recovery_authorization_id
        ):
            raise ValueError(
                "resume intent requires checkpoint and recovery authorization"
            )
        if self.resume_mode == "receipt_only":
            raise ValueError(
                "receipt-only work must use the deterministic receipt observer, "
                "not an agent subprocess"
            )


@dataclass(slots=True)
class DurableRunHandle:
    intent: DurableLaunchIntent
    process: subprocess.Popen[str]
    token: runtime_control.RuntimeTurnToken


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryAdmission:
    disposition: RecoveryDisposition
    actor_id: str
    attempt_id: str
    parent_turn_id: str | None
    reason_code: str
    requires_fresh_observation: bool


def _checkpoint_matches(
    connection: sqlite3.Connection,
    intent: DurableLaunchIntent,
    parent: runtime_control.AgentRuntimeTurn,
) -> bool:
    """Validate latest checkpoint lineage without treating it as authorization."""
    from applypilot.storage import agent_control

    checkpoint = agent_control.latest_actor_checkpoint(connection, parent.actor_id)
    return bool(
        checkpoint is not None
        and checkpoint.schema_version == "2"
        and checkpoint.checkpoint_id == intent.checkpoint_id
        and checkpoint.actor_id == parent.actor_id
        and checkpoint.attempt_id == parent.attempt_id
        and checkpoint.turn_id == parent.turn_id
    )


class DurableAgentRuntime:
    """Bind a local subprocess lifecycle to exact durable runtime-turn CAS tokens."""

    def __init__(
        self,
        subprocess_runtime: SubprocessAgentRuntime,
        connection_provider: ConnectionProvider,
        *,
        process_identity: ProcessIdentityLookup,
        resume_authorizer: ResumeAuthorizer | None = None,
        close_connections: bool = False,
    ) -> None:
        self._runtime = subprocess_runtime
        self._connection_provider = connection_provider
        self._process_identity = process_identity
        self._resume_authorizer = resume_authorizer or (
            lambda _intent, _parent: False
        )
        self._close_connections = close_connections
        self._handles: dict[str, DurableRunHandle] = {}
        self._lock = threading.RLock()

    def _connection(self) -> sqlite3.Connection:
        connection = self._connection_provider()
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection_provider must return sqlite3.Connection")
        return connection

    def _release_connection(self, connection: sqlite3.Connection) -> None:
        if self._close_connections:
            connection.close()

    def _reserve(self, intent: DurableLaunchIntent) -> runtime_control.AgentRuntimeTurn:
        connection = self._connection()
        try:
            return runtime_control.start_runtime_turn(
                connection,
                turn_id=intent.spec.turn_id,
                actor_id=intent.spec.actor_id,
                attempt_id=intent.spec.attempt_id,
                parent_turn_id=intent.spec.parent_run_id,
                checkpoint_id=intent.checkpoint_id,
                runtime_id=intent.spec.runtime_id,
                profile_id=intent.spec.profile_id,
                runtime_backend=intent.runtime_backend,
                model=intent.model,
                provider_session_id=intent.provider_session_id,
                resume_mode=intent.resume_mode,
                submit_started=intent.spec.submit_started,
                tool_surface_hash=intent.tool_surface_hash,
                prompt_contract_hash=intent.prompt_contract_hash,
                idempotency_key=intent.idempotency_key or intent.spec.turn_id,
                require_new=True,
            )
        finally:
            self._release_connection(connection)

    def _attach(
        self,
        token: runtime_control.RuntimeTurnToken,
        process: subprocess.Popen[str],
    ) -> runtime_control.AgentRuntimeTurn:
        identity = self._process_identity(process.pid)
        if identity is None or identity[0] != process.pid or identity[1] < 1:
            raise DurableRuntimeBlocked("spawned process identity is unavailable")
        connection = self._connection()
        try:
            return runtime_control.attach_runtime_turn_process(
                connection,
                token=token,
                process_id=identity[0],
                process_birth_time=identity[1],
            )
        finally:
            self._release_connection(connection)

    def _terminal_token(
        self,
        token: runtime_control.RuntimeTurnToken,
        *,
        status: str,
        failure_code: str | None,
        exit_code: int | None,
    ) -> runtime_control.AgentRuntimeTurn:
        connection = self._connection()
        try:
            return runtime_control.mark_runtime_turn_terminal(
                connection,
                token=token,
                status=status,
                failure_code=failure_code,
                exit_code=exit_code,
            )
        finally:
            self._release_connection(connection)

    def _parent(self, parent_run_id: str) -> runtime_control.AgentRuntimeTurn:
        connection = self._connection()
        try:
            parent = runtime_control.get_runtime_turn(connection, parent_run_id)
        finally:
            self._release_connection(connection)
        if parent is None:
            raise DurableRuntimeBlocked("resume parent turn is unknown")
        return parent

    def _reserve_resume_atomic(
        self,
        intent: DurableLaunchIntent,
    ) -> tuple[
        runtime_control.AgentRuntimeTurn,
        runtime_control.AgentRuntimeTurn,
    ]:
        """Authorize and reserve a child under one SQLite writer transaction."""
        parent_run_id = intent.spec.parent_run_id
        if parent_run_id is None:
            raise ValueError("resume intent has no parent")
        connection = self._connection()
        try:
            from applypilot.storage import agent_control

            runtime_control.ensure_schema(connection)
            agent_control.ensure_schema(connection)
            if connection.in_transaction:
                raise DurableRuntimeBlocked(
                    "resume connection must not carry an existing transaction"
                )
            connection.execute("BEGIN IMMEDIATE")
            parent = runtime_control.get_runtime_turn(connection, parent_run_id)
            if parent is None:
                raise DurableRuntimeBlocked("resume parent turn is unknown")
            if parent.status == "running":
                raise DurableRuntimeBlocked("resume parent still has a live durable owner")
            if (parent.actor_id, parent.attempt_id) != (
                intent.spec.actor_id,
                intent.spec.attempt_id,
            ):
                raise DurableRuntimeBlocked("resume actor or attempt lineage changed")
            if not _checkpoint_matches(connection, intent, parent):
                raise DurableRuntimeBlocked("resume checkpoint is stale or mismatched")
            if parent.submit_started:
                raise DurableRuntimeBlocked(
                    "post-submit work must use deterministic receipt reconciliation"
                )
            # The authorizer deliberately receives no SQLite connection.  It is
            # a process-local, single-use capability check and therefore cannot
            # commit, roll back, or replace this IMMEDIATE transaction.
            authorized = self._resume_authorizer(intent, parent)
            if not authorized:
                raise DurableRuntimeBlocked("resume authorization is absent or stale")
            if not _checkpoint_matches(connection, intent, parent):
                raise DurableRuntimeBlocked(
                    "resume checkpoint changed during authorization"
                )
            reservation = runtime_control.start_runtime_turn(
                connection,
                turn_id=intent.spec.turn_id,
                actor_id=intent.spec.actor_id,
                attempt_id=intent.spec.attempt_id,
                parent_turn_id=intent.spec.parent_run_id,
                checkpoint_id=intent.checkpoint_id,
                runtime_id=intent.spec.runtime_id,
                profile_id=intent.spec.profile_id,
                runtime_backend=intent.runtime_backend,
                model=intent.model,
                provider_session_id=intent.provider_session_id,
                resume_mode=intent.resume_mode,
                submit_started=intent.spec.submit_started,
                tool_surface_hash=intent.tool_surface_hash,
                prompt_contract_hash=intent.prompt_contract_hash,
                idempotency_key=intent.idempotency_key or intent.spec.turn_id,
                require_new=True,
            )
            connection.commit()
            return parent, reservation
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            self._release_connection(connection)

    def _launch(
        self,
        intent: DurableLaunchIntent,
        *,
        parent: runtime_control.AgentRuntimeTurn | None,
        reservation: runtime_control.AgentRuntimeTurn,
        popen_factory: Callable[..., subprocess.Popen[str]] | None,
    ) -> DurableRunHandle:
        token = runtime_control.token_from_turn(reservation)
        spawned = False
        attached = False
        spawned_process: subprocess.Popen[str] | None = None

        def on_spawned(process: subprocess.Popen[str]) -> None:
            nonlocal spawned, attached, spawned_process, token
            spawned = True
            spawned_process = process
            row = self._attach(token, process)
            token = runtime_control.token_from_turn(row)
            attached = True

        try:
            if parent is None:
                process = self._runtime.start(
                    intent.spec,
                    popen_factory=popen_factory,
                    on_spawned=on_spawned,
                )
            else:
                process = self._runtime.resume(
                    parent.turn_id,
                    intent.spec,
                    popen_factory=popen_factory,
                    on_spawned=on_spawned,
                    persisted_parent=SubprocessParentIdentity(
                        run_id=parent.turn_id,
                        actor_id=parent.actor_id,
                        attempt_id=parent.attempt_id,
                        runtime_id=parent.runtime_id,
                        profile_id=parent.profile_id,
                        submit_started=bool(parent.submit_started),
                    ),
                )
        except BaseException:
            process_dead = spawned_process is None
            if spawned_process is not None:
                try:
                    process_dead = spawned_process.poll() is not None
                except BaseException as poll_error:
                    logger.debug(
                        "spawned subprocess poll failed during durable cleanup",
                        exc_info=poll_error,
                    )
                    process_dead = False
            if process_dead:
                status = "unknown" if spawned else "failed"
                failure_code = (
                    "PROMPT_DELIVERY_UNCERTAIN"
                    if attached
                    else "SPAWN_IDENTITY_UNBOUND"
                    if spawned
                    else "SPAWN_FAILED"
                )
                try:
                    self._terminal_token(
                        token,
                        status=status,
                        failure_code=failure_code,
                        exit_code=None,
                    )
                except (KeyError, RuntimeError, ValueError, sqlite3.Error):
                    # Preserve the original process failure. A conflicting durable
                    # owner must remain visible for reconciliation rather than being
                    # overwritten by this failed launcher.
                    pass
            elif spawned_process is not None:
                quarantined = DurableRunHandle(
                    intent=intent,
                    process=spawned_process,
                    token=token,
                )
                with self._lock:
                    self._handles[intent.spec.turn_id] = quarantined
            raise
        handle = DurableRunHandle(intent=intent, process=process, token=token)
        with self._lock:
            self._handles[intent.spec.turn_id] = handle
        return handle

    def start(
        self,
        intent: DurableLaunchIntent,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
    ) -> DurableRunHandle:
        if intent.spec.parent_run_id is not None:
            raise DurableRuntimeBlocked("root start cannot declare a parent")
        reservation = self._reserve(intent)
        return self._launch(
            intent,
            parent=None,
            reservation=reservation,
            popen_factory=popen_factory,
        )

    def resume(
        self,
        intent: DurableLaunchIntent,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
    ) -> DurableRunHandle:
        parent, reservation = self._reserve_resume_atomic(intent)
        return self._launch(
            intent,
            parent=parent,
            reservation=reservation,
            popen_factory=popen_factory,
        )

    def terminal(
        self,
        handle: DurableRunHandle,
        *,
        status: str,
        failure_code: str | None = None,
        exit_code: int | None = None,
    ) -> runtime_control.AgentRuntimeTurn:
        terminal = self._terminal_token(
            handle.token,
            status=status,
            failure_code=failure_code,
            exit_code=exit_code,
        )
        return terminal

    def reconcile_actor(
        self,
        actor_id: str,
        attempt_id: str,
    ) -> RuntimeRecoveryAdmission:
        connection = self._connection()
        try:
            turn = runtime_control.latest_runtime_turn_for_actor(connection, actor_id)
        finally:
            self._release_connection(connection)
        if turn is None:
            return RuntimeRecoveryAdmission(
                disposition="none",
                actor_id=actor_id,
                attempt_id=attempt_id,
                parent_turn_id=None,
                reason_code="NO_RUNNING_TURN",
                requires_fresh_observation=False,
            )
        if turn.attempt_id != attempt_id:
            return RuntimeRecoveryAdmission(
                disposition="blocked",
                actor_id=actor_id,
                attempt_id=attempt_id,
                parent_turn_id=turn.turn_id,
                reason_code="ATTEMPT_LINEAGE_MISMATCH",
                requires_fresh_observation=True,
            )
        if turn.status == "unknown":
            return RuntimeRecoveryAdmission(
                disposition="receipt_only" if turn.submit_started else "recovery_required",
                actor_id=actor_id,
                attempt_id=attempt_id,
                parent_turn_id=turn.turn_id,
                reason_code=turn.failure_code or "RUNTIME_OUTCOME_UNKNOWN",
                requires_fresh_observation=True,
            )
        if turn.status != "running":
            return RuntimeRecoveryAdmission(
                disposition="none",
                actor_id=actor_id,
                attempt_id=attempt_id,
                parent_turn_id=None,
                reason_code="NO_RUNNING_TURN",
                requires_fresh_observation=False,
            )
        token = runtime_control.token_from_turn(turn)
        if turn.process_id is None:
            reason = "SPAWN_IDENTITY_UNBOUND"
        else:
            try:
                identity = self._process_identity(turn.process_id)
            except (OSError, RuntimeError):
                return RuntimeRecoveryAdmission(
                    disposition="blocked",
                    actor_id=actor_id,
                    attempt_id=attempt_id,
                    parent_turn_id=turn.turn_id,
                    reason_code="PROCESS_IDENTITY_UNAVAILABLE",
                    requires_fresh_observation=True,
                )
            if identity == (turn.process_id, turn.process_birth_time):
                return RuntimeRecoveryAdmission(
                    disposition="live_owner",
                    actor_id=actor_id,
                    attempt_id=attempt_id,
                    parent_turn_id=turn.turn_id,
                    reason_code="PROCESS_STILL_RUNNING",
                    requires_fresh_observation=False,
                )
            reason = (
                "PROCESS_DISAPPEARED"
                if identity is None
                else "PROCESS_IDENTITY_REUSED"
            )
        self._terminal_token(
            token,
            status="unknown",
            failure_code=reason,
            exit_code=None,
        )
        return RuntimeRecoveryAdmission(
            disposition="receipt_only" if turn.submit_started else "recovery_required",
            actor_id=actor_id,
            attempt_id=attempt_id,
            parent_turn_id=turn.turn_id,
            reason_code=reason,
            requires_fresh_observation=True,
        )

    def local_process(self, turn_id: str) -> subprocess.Popen[str]:
        with self._lock:
            try:
                return self._handles[turn_id].process
            except KeyError as error:
                raise KeyError(f"durable runtime handle is unknown: {turn_id}") from error

    def close_local(self, turn_id: str | None = None) -> None:
        """Close only local handles; durable terminal outcome is explicit."""
        self._runtime.close(turn_id)
