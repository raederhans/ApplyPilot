"""Typed, fail-closed operator commands for parked application exceptions."""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

from applypilot.apply.contracts import ApplicationException, application_actor_id
from applypilot.storage import agent_control

OperatorAction = Literal["resolve", "resume", "reconcile"]
_ACTIONS = frozenset({"resolve", "resume", "reconcile"})
_SCOPES = frozenset({"application", "employer", "job_family", "jurisdiction", "global"})
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_RECOVERY_BUDGET = 2


class OperatorCommandError(RuntimeError):
    """Base class for rejected or indeterminate operator commands."""


class OperatorIdentityDrift(OperatorCommandError):
    """The parked exception no longer matches the command envelope."""


class OperatorUnknownReplay(OperatorCommandError):
    """A previously started executor call has no durable terminal result."""


class OperatorCircuitOpen(OperatorCommandError):
    """The durable retry budget or repeated-failure breaker is open."""


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    command_id: str
    exception_id: str
    action: OperatorAction
    run_id: str
    attempt_id: str
    actor_id: str
    turn_id: str
    expected_status: str = "open"
    input_ref: str | None = None
    input_sha256: str | None = None
    recovery_budget: int = 2
    browser_authority: bool = False
    page_write_authority: bool = False
    submit_authority: bool = False
    ledger_write_authority: bool = False
    schema_version: str = "1"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for name in ("command_id", "exception_id", "run_id", "attempt_id", "actor_id", "turn_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _REF.fullmatch(value):
                raise ValueError(f"{name} must be a bounded opaque reference")
        if self.action not in _ACTIONS:
            raise ValueError("unsupported operator action")
        if self.actor_id != application_actor_id(self.attempt_id) or self.turn_id != self.run_id:
            raise ValueError("operator command identity is not canonical")
        if self.expected_status != "open":
            raise ValueError("operator commands must expect an open exception")
        if (self.input_ref is None) != (self.input_sha256 is None):
            raise ValueError("input_ref and input_sha256 must be supplied together")
        if self.action == "resolve" and self.input_ref is not None:
            raise ValueError("resolve commands cannot carry operator input")
        if self.action in {"resume", "reconcile"} and self.input_ref is None:
            raise ValueError("resume/reconcile commands require an opaque input ref and digest")
        if self.input_ref is not None and not _REF.fullmatch(self.input_ref):
            raise ValueError("input_ref must be a bounded opaque reference")
        if self.input_sha256 is not None and not _SHA256.fullmatch(self.input_sha256):
            raise ValueError("input_sha256 must be a lowercase sha256 digest")
        if (
            isinstance(self.recovery_budget, bool)
            or not isinstance(self.recovery_budget, int)
            or self.recovery_budget < 0
            or self.recovery_budget > MAX_RECOVERY_BUDGET
        ):
            raise ValueError(
                f"recovery_budget must be an integer from 0 through {MAX_RECOVERY_BUDGET}"
            )
        if self.action in {"resume", "reconcile"} and self.recovery_budget == 0:
            raise ValueError("effectful operator commands require a positive recovery_budget")
        for name in (
            "browser_authority", "page_write_authority", "submit_authority", "ledger_write_authority"
        ):
            if getattr(self, name) is not False:
                raise ValueError("operator commands cannot claim browser, page, Submit, or ledger authority")
        if self.schema_version != "1":
            raise ValueError("unsupported OperatorCommand schema_version")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")

    def storage_values(self) -> tuple[object, ...]:
        return (
            self.command_id,
            self.exception_id,
            self.action,
            self.run_id,
            self.attempt_id,
            self.actor_id,
            self.turn_id,
            self.expected_status,
            self.input_ref,
            self.input_sha256,
            self.recovery_budget,
            0,
            0,
            0,
            0,
            self.schema_version,
            self.created_at.isoformat(),
        )


@dataclass(frozen=True, slots=True)
class OperatorExecution:
    verified: bool
    outcome: str
    terminal_status: str
    result_ref: str | None = None
    result_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.verified, bool) or not _REF.fullmatch(self.outcome):
            raise ValueError("executor outcome is invalid")
        if self.terminal_status not in {"completed", "failed", "blocked"}:
            raise ValueError("executor terminal_status is invalid")
        if self.verified != (self.terminal_status == "completed"):
            raise ValueError("only a completed executor result may be verified")
        if (self.result_ref is None) != (self.result_sha256 is None):
            raise ValueError("result_ref and result_sha256 must be supplied together")
        if self.verified and self.result_ref is None:
            raise ValueError("verified executor results require an opaque result ref and digest")
        if self.result_ref is not None and not _REF.fullmatch(self.result_ref):
            raise ValueError("result_ref must be a bounded opaque reference")
        if self.result_sha256 is not None and not _SHA256.fullmatch(self.result_sha256):
            raise ValueError("result_sha256 must be a lowercase sha256 digest")


@dataclass(frozen=True, slots=True)
class OperatorCommandResult:
    command_id: str
    action: OperatorAction
    status: str
    resolved: bool
    replayed: bool = False
    result_ref: str | None = None
    result_sha256: str | None = None
    continuation_authorized: bool = field(default=False, init=False)


class OperatorExecutor(Protocol):
    def __call__(self, command: OperatorCommand) -> OperatorExecution: ...


class OperatorVerifier(Protocol):
    def __call__(self, command: OperatorCommand, execution: OperatorExecution) -> bool: ...


class OperatorScopeResolver(Protocol):
    def __call__(self, exception: ApplicationException) -> tuple[str, str] | None: ...


@contextmanager
def _savepoint(connection: sqlite3.Connection, prefix: str):
    owns_transaction = not connection.in_transaction
    name = f"{prefix}_{uuid.uuid4().hex}"
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
        connection.execute(f"RELEASE SAVEPOINT {name}")
        if owns_transaction:
            connection.commit()
    except Exception:
        connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
        connection.execute(f"RELEASE SAVEPOINT {name}")
        if owns_transaction:
            connection.rollback()
        raise


def _result_values(
    command: OperatorCommand,
    *,
    stage: str,
    outcome: str,
    terminal_status: str | None,
    occurred_at: datetime,
    result_ref: str | None = None,
    result_sha256: str | None = None,
) -> tuple[object, ...]:
    return (
        f"operator-result:{command.command_id}:{stage}",
        command.command_id,
        stage,
        outcome,
        terminal_status,
        result_ref,
        result_sha256,
        "1",
        occurred_at.isoformat(),
    )


def _exception(connection: sqlite3.Connection, command: OperatorCommand) -> ApplicationException:
    item = agent_control.get_exception(connection, command.exception_id)
    if item is None:
        raise OperatorIdentityDrift("operator exception is missing or ambiguous")
    if (
        item.run_id,
        item.attempt_id,
        item.actor_id,
        item.turn_id,
        item.status,
    ) != (
        command.run_id,
        command.attempt_id,
        command.actor_id,
        command.turn_id,
        command.expected_status,
    ):
        raise OperatorIdentityDrift("operator exception identity or status drifted")
    allowed_lanes = {
        "resolve": {"parked", "capability", "receipt_reconciliation", "human_only", "recovery_execution"},
        "resume": {"human_only", "recovery_execution"},
        "reconcile": {"receipt_reconciliation"},
    }
    if item.queue_kind not in allowed_lanes[command.action]:
        raise OperatorIdentityDrift("operator action does not match the exception queue lane")
    if (
        command.action == "resume"
        and item.queue_kind == "recovery_execution"
        and item.context.get("effect_scope") not in {"same_application", "new_session"}
    ):
        raise OperatorIdentityDrift("resume is restricted to pre-submit recovery effects")
    return item


def _existing_terminal(connection: sqlite3.Connection, command: OperatorCommand) -> OperatorCommandResult | None:
    row = agent_control.get_operator_command(connection, command.command_id)
    if row is None:
        return None
    # ``created_at`` records when the host first accepted this command. A new
    # process reconstructing the same semantic envelope cannot know that
    # timestamp, so it is deliberately excluded from replay identity. Every
    # authority, lineage, input digest, and budget field remains exact-match.
    if tuple(row)[:-1] != command.storage_values()[:-1]:
        raise ValueError(f"operator command collision: {command.command_id}")
    results = agent_control.list_operator_result_rows(connection, command_id=command.command_id)
    terminal = [row for row in results if str(row[2]) in {"verified", "failed"}]
    if terminal:
        last = terminal[-1]
        return OperatorCommandResult(
            command_id=command.command_id,
            action=command.action,
            status=str(last[4] or last[3]),
            resolved=str(last[2]) == "verified",
            replayed=True,
            result_ref=None if last[5] is None else str(last[5]),
            result_sha256=None if last[6] is None else str(last[6]),
        )
    if any(str(row[2]) == "started" for row in results):
        raise OperatorUnknownReplay("started operator command has no terminal result")
    if any(str(row[2]) == "requested" for row in results):
        return OperatorCommandResult(
            command_id=command.command_id,
            action=command.action,
            status="requested",
            resolved=False,
            replayed=True,
        )
    raise OperatorUnknownReplay("persisted operator command has no lifecycle result")


def _command_from_storage_row(row: sqlite3.Row | tuple) -> OperatorCommand:
    return OperatorCommand(
        command_id=str(row[0]),
        exception_id=str(row[1]),
        action=str(row[2]),  # type: ignore[arg-type]
        run_id=str(row[3]),
        attempt_id=str(row[4]),
        actor_id=str(row[5]),
        turn_id=str(row[6]),
        expected_status=str(row[7]),
        input_ref=None if row[8] is None else str(row[8]),
        input_sha256=None if row[9] is None else str(row[9]),
        recovery_budget=int(row[10]),
        browser_authority=bool(row[11]),
        page_write_authority=bool(row[12]),
        submit_authority=bool(row[13]),
        ledger_write_authority=bool(row[14]),
        schema_version=str(row[15]),
        created_at=datetime.fromisoformat(str(row[16])),
    )


def requested_resume_commands(
    connection: sqlite3.Connection,
    exception_id: str,
) -> tuple[OperatorCommand, ...]:
    """Rebuild every requested resume envelope with its accepted timestamp."""
    return tuple(
        _command_from_storage_row(row)
        for row in agent_control.list_requested_resume_rows(
            connection,
            exception_id=exception_id,
        )
    )


def load_requested_resume_command(
    connection: sqlite3.Connection,
    exception_id: str,
) -> OperatorCommand | None:
    """Load the unique current requested resume command, failing closed on drift."""
    commands = requested_resume_commands(connection, exception_id)
    if not commands:
        return None
    if len(commands) != 1:
        raise OperatorCommandError("multiple requested resume commands exist for one exception")
    command = commands[0]
    results = agent_control.list_operator_result_rows(connection, command_id=command.command_id)
    if any(str(row[2]) in {"started", "verified", "failed"} for row in results):
        return None
    _exception(connection, command)
    return command


def _enforce_breaker(connection: sqlite3.Connection, command: OperatorCommand) -> None:
    rows = agent_control.list_operator_result_rows(connection, exception_id=command.exception_id)
    started = {str(row[1]) for row in rows if str(row[2]) == "started"}
    terminal = {str(row[1]) for row in rows if str(row[2]) in {"verified", "failed"}}
    unknown = started - terminal
    failed = {str(row[1]) for row in rows if str(row[2]) == "failed"}
    durable_budget_row = connection.execute(
        "SELECT MIN(recovery_budget) FROM operator_command_envelopes "
        "WHERE exception_id=? AND action IN ('resume', 'reconcile')",
        (command.exception_id,),
    ).fetchone()
    durable_budget = (
        command.recovery_budget
        if durable_budget_row is None or durable_budget_row[0] is None
        else min(command.recovery_budget, int(durable_budget_row[0]))
    )
    if unknown:
        raise OperatorCircuitOpen("an operator executor outcome is unknown")
    attempts_used = len(started)
    if attempts_used >= durable_budget:
        raise OperatorCircuitOpen("operator recovery budget exhausted")
    if len(unknown | failed) >= 2:
        raise OperatorCircuitOpen("operator recovery circuit is open")


class OperatorCommandService:
    """Execute one command against one exact parked exception."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        executor: OperatorExecutor | None = None,
        verifier: OperatorVerifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.connection = connection
        self.executor = executor
        self.verifier = verifier
        self.clock = clock

    def execute(self, command: OperatorCommand) -> OperatorCommandResult:
        agent_control.ensure_schema(self.connection)
        replay = _existing_terminal(self.connection, command)
        if replay is not None and replay.status != "requested":
            return replay
        _exception(self.connection, command)
        if command.action == "resolve":
            return self._resolve(command)
        if self.executor is None:
            raise OperatorCommandError("resume/reconcile requires an injected executor")
        if self.connection.in_transaction:
            raise OperatorCommandError(
                "effectful operator commands require a connection without an outer transaction"
            )
        _enforce_breaker(self.connection, command)
        return self._execute_recovery(command)

    def replay(self, command: OperatorCommand) -> OperatorCommandResult | None:
        """Inspect an exact persisted lifecycle without executing an effect."""
        agent_control.ensure_schema(self.connection)
        return _existing_terminal(self.connection, command)

    def request_resume(self, command: OperatorCommand) -> OperatorCommandResult:
        """Durably request a validated resume without starting its executor."""
        if command.action != "resume":
            raise OperatorCommandError("request_resume requires a resume command")
        agent_control.ensure_schema(self.connection)
        replay = _existing_terminal(self.connection, command)
        if replay is not None:
            return replay
        _exception(self.connection, command)
        if self.connection.in_transaction:
            raise OperatorCommandError(
                "resume requests require a connection without an outer transaction"
            )
        requested_at = self.clock()
        with _savepoint(self.connection, "operator_requested"):
            agent_control.append_operator_command(self.connection, command.storage_values())
            agent_control.append_operator_result(
                self.connection,
                _result_values(
                    command,
                    stage="requested",
                    outcome="resume_requested",
                    terminal_status=None,
                    occurred_at=requested_at,
                ),
            )
        return OperatorCommandResult(command.command_id, command.action, "requested", False)

    def expire_requested_resume(
        self,
        command: OperatorCommand,
        *,
        expired_at: datetime | None = None,
    ) -> OperatorCommandResult:
        """Terminally block a requested command without invoking an executor."""
        if command.action != "resume":
            raise OperatorCommandError("expire_requested_resume requires a resume command")
        agent_control.ensure_schema(self.connection)
        replay = _existing_terminal(self.connection, command)
        if replay is None or replay.status != "requested":
            if replay is not None and replay.status == "blocked":
                return replay
            raise OperatorCommandError("resume command is not uniquely requested")
        when = expired_at or self.clock()
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("expired_at must be timezone-aware")
        with _savepoint(self.connection, "operator_request_expired"):
            agent_control.append_operator_result(
                self.connection,
                _result_values(
                    command,
                    stage="failed",
                    outcome="resume_request_expired",
                    terminal_status="blocked",
                    occurred_at=when,
                ),
            )
        return OperatorCommandResult(command.command_id, command.action, "blocked", False)

    def _resolve(self, command: OperatorCommand) -> OperatorCommandResult:
        now = self.clock()
        with _savepoint(self.connection, "operator_resolve"):
            agent_control.append_operator_command(self.connection, command.storage_values())
            if not agent_control.resolve_exception_cas(
                self.connection,
                exception_id=command.exception_id,
                run_id=command.run_id,
                attempt_id=command.attempt_id,
                actor_id=command.actor_id,
                turn_id=command.turn_id,
                expected_status=command.expected_status,
                resolved_at=now.isoformat(),
            ):
                raise OperatorIdentityDrift("resolve CAS failed")
            agent_control.append_operator_result(
                self.connection,
                _result_values(
                    command,
                    stage="verified",
                    outcome="operator_resolved",
                    terminal_status="dismissed",
                    occurred_at=now,
                ),
            )
        return OperatorCommandResult(command.command_id, command.action, "dismissed", True)

    def _execute_recovery(self, command: OperatorCommand) -> OperatorCommandResult:
        started_at = self.clock()
        with _savepoint(self.connection, "operator_started"):
            agent_control.append_operator_command(self.connection, command.storage_values())
            agent_control.append_operator_result(
                self.connection,
                _result_values(
                    command,
                    stage="started",
                    outcome="executor_started",
                    terminal_status=None,
                    occurred_at=started_at,
                ),
            )
        try:
            execution = self.executor(command)  # type: ignore[misc]
        except Exception as exc:
            raise OperatorCommandError(
                "executor outcome is unknown; command remains started"
            ) from exc
        if not isinstance(execution, OperatorExecution):
            raise OperatorCommandError(
                "executor returned an invalid unknown result; command remains started"
            )
        finished_at = self.clock()
        if not execution.verified:
            with _savepoint(self.connection, "operator_failed"):
                agent_control.append_operator_result(
                    self.connection,
                    _result_values(
                        command,
                        stage="failed",
                        outcome=execution.outcome,
                        terminal_status=execution.terminal_status,
                        result_ref=execution.result_ref,
                        result_sha256=execution.result_sha256,
                        occurred_at=finished_at,
                    ),
                )
            return OperatorCommandResult(
                command.command_id,
                command.action,
                execution.terminal_status,
                False,
                result_ref=execution.result_ref,
                result_sha256=execution.result_sha256,
            )
        try:
            trusted = self.verifier is not None and self.verifier(command, execution) is True
        except Exception as exc:
            raise OperatorCommandError(
                "trusted verification outcome is unknown; command remains started"
            ) from exc
        if not trusted:
            with _savepoint(self.connection, "operator_unverified"):
                agent_control.append_operator_result(
                    self.connection,
                    _result_values(
                        command,
                        stage="failed",
                        outcome="trusted_verification_rejected",
                        terminal_status="blocked",
                        result_ref=execution.result_ref,
                        result_sha256=execution.result_sha256,
                        occurred_at=finished_at,
                    ),
                )
            return OperatorCommandResult(
                command.command_id,
                command.action,
                "blocked",
                False,
                result_ref=execution.result_ref,
                result_sha256=execution.result_sha256,
            )
        with _savepoint(self.connection, "operator_verified"):
            if not agent_control.resolve_exception_cas(
                self.connection,
                exception_id=command.exception_id,
                run_id=command.run_id,
                attempt_id=command.attempt_id,
                actor_id=command.actor_id,
                turn_id=command.turn_id,
                expected_status=command.expected_status,
                resolved_at=finished_at.isoformat(),
            ):
                raise OperatorIdentityDrift("verified executor result lost exception CAS")
            agent_control.append_operator_result(
                self.connection,
                _result_values(
                    command,
                    stage="verified",
                    outcome=execution.outcome,
                    terminal_status="completed",
                    result_ref=execution.result_ref,
                    result_sha256=execution.result_sha256,
                    occurred_at=finished_at,
                ),
            )
        return OperatorCommandResult(
            command.command_id,
            command.action,
            "completed",
            True,
            result_ref=execution.result_ref,
            result_sha256=execution.result_sha256,
        )


def semantic_exception_groups(
    connection: sqlite3.Connection,
    *,
    scope_resolver: OperatorScopeResolver | None = None,
) -> Mapping[str, tuple[str, ...]]:
    """Group open exceptions for read-only triage; execution remains exception-bound."""
    groups: dict[str, list[str]] = {}
    for item in agent_control.list_exceptions(connection, status="open", limit=500):
        context = item.context
        resolved_scope = scope_resolver(item) if scope_resolver is not None else None
        requested, reference = (
            resolved_scope if resolved_scope is not None else ("application", item.exception_id)
        )
        sensitive = context.get("sensitive") is True or context.get("human_specific") is True
        employer_specific = context.get("employer_specific") is True
        if requested not in _SCOPES or sensitive or employer_specific:
            requested = "application"
            reference = item.exception_id
        if not _REF.fullmatch(reference):
            requested = "application"
            reference = item.exception_id
        groups.setdefault(f"{requested}:{reference}", []).append(item.exception_id)
    return {key: tuple(sorted(values)) for key, values in sorted(groups.items())}
