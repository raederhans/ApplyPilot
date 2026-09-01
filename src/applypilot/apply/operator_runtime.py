"""Conservative production adapters for typed operator commands.

This module owns no browser, page, Submit, or ledger authority.  It validates
durable identities before delegating to the existing command and receipt
contracts, and treats an injected in-process owner as the only legal resume
executor.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from applypilot import database
from applypilot.apply.contracts import ApplicationException
from applypilot.apply.human_handoff import fresh_resume_context, load_human_response
from applypilot.apply.operator_commands import (
    OperatorCommand,
    OperatorCommandError,
    OperatorCommandResult,
    OperatorCommandService,
    OperatorExecution,
    load_requested_resume_command,
    requested_resume_commands,
    semantic_exception_groups,
)
from applypilot.storage import agent_control, runtime_control

_RECEIPT_KEYS = frozenset(
    {
        "source",
        "receipt_id",
        "job_url",
        "company_name",
        "job_title",
        "confirmation_text",
        "portal_status",
        "observed_at",
        "gate_id",
        "batch_id",
        "attempt_id",
    }
)
_RECEIPT_REQUIRED = frozenset(
    {"source", "receipt_id", "job_url", "company_name", "job_title", "gate_id", "batch_id", "attempt_id"}
)
_RECEIPT_SOURCES = frozenset({"confirmation_email", "candidate_portal", "browser_receipt"})
_TERMINAL_PARENT = frozenset({"blocked", "cancelled", "closed", "completed", "failed", "timed_out"})


class ResumeOwner(Protocol):
    """Same-process owner that alone may create the fresh child runtime turn."""

    def __call__(
        self,
        command: OperatorCommand,
        resume_context: Mapping[str, object],
    ) -> OperatorExecution: ...


@dataclass(frozen=True, slots=True)
class RuntimeInspection:
    actor_id: str
    latest_turn_id: str | None
    latest_status: str | None
    attempt_id: str | None
    parent_turn_id: str | None
    checkpoint_id: str | None
    submit_started: bool | None
    open_exception_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequestedResumeExpiry:
    exception_id: str
    request_id: str
    command_id: str | None
    status: str = "expired"
    replayed: bool = False


@contextmanager
def _atomic(connection: sqlite3.Connection, prefix: str):
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


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OperatorCommandError(f"{key} is required")
    return value.strip()


def verified_child_execution(
    connection: sqlite3.Connection,
    *,
    actor_id: str,
    attempt_id: str,
    parent_turn_id: str,
    child_turn_id: str,
) -> OperatorExecution:
    """Build verified resume evidence from one exact durable completed child."""
    child = runtime_control.get_runtime_turn(connection, child_turn_id)
    parent_checkpoint = agent_control.latest_checkpoint(connection, parent_turn_id)
    latest = agent_control.latest_actor_checkpoint(connection, actor_id)
    if (
        child is None
        or child.actor_id != actor_id
        or child.attempt_id != attempt_id
        or child.parent_turn_id != parent_turn_id
        or child.resume_mode != "resume"
        or child.status != "completed"
        or child.submit_started != 0
        or parent_checkpoint is None
        or parent_checkpoint.schema_version != "2"
        or child.checkpoint_id != parent_checkpoint.checkpoint_id
        or latest is None
        or latest.schema_version != "2"
        or latest.actor_id != actor_id
        or latest.attempt_id != attempt_id
        or latest.turn_id != child_turn_id
        or latest.sequence <= parent_checkpoint.sequence
    ):
        raise OperatorCommandError("durable child resume evidence is not exact and completed")
    return OperatorExecution(
        True,
        "verified_child_resume",
        "completed",
        result_ref=f"runtime:{child_turn_id}",
        result_sha256=_canonical_digest(
            [child_turn_id, latest.checkpoint_id, latest.sequence]
        ),
    )


class OperatorRuntime:
    """Typed read/command facade over one explicitly supplied SQLite connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        resume_owner: ResumeOwner | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be an explicit sqlite3 connection")
        self.connection = connection
        self.resume_owner = resume_owner

    def list_exceptions(
        self,
        *,
        status: str | None = "open",
        limit: int = 100,
    ) -> tuple[ApplicationException, ...]:
        return tuple(agent_control.list_exceptions(self.connection, status=status, limit=limit))

    def show_exception(self, exception_id: str) -> ApplicationException | None:
        return agent_control.get_exception(self.connection, exception_id)

    def group_exceptions(self) -> Mapping[str, tuple[str, ...]]:
        return semantic_exception_groups(self.connection)

    def inspect_run(self, actor_id: str) -> RuntimeInspection:
        turn = runtime_control.latest_runtime_turn_for_actor(self.connection, actor_id)
        exceptions = agent_control.list_exceptions(self.connection, status="open", limit=500)
        return RuntimeInspection(
            actor_id=actor_id,
            latest_turn_id=None if turn is None else turn.turn_id,
            latest_status=None if turn is None else turn.status,
            attempt_id=None if turn is None else turn.attempt_id,
            parent_turn_id=None if turn is None else turn.parent_turn_id,
            checkpoint_id=None if turn is None else turn.checkpoint_id,
            submit_started=None if turn is None else bool(turn.submit_started),
            open_exception_ids=tuple(item.exception_id for item in exceptions if item.actor_id == actor_id),
        )

    def resolve(self, command: OperatorCommand) -> OperatorCommandResult:
        if command.action != "resolve":
            raise OperatorCommandError("resolve requires a resolve command")
        return OperatorCommandService(self.connection).execute(command)

    def reconcile(
        self,
        command: OperatorCommand,
        *,
        evidence_ref: str,
        evidence_bytes: bytes,
    ) -> OperatorCommandResult:
        if command.action != "reconcile":
            raise OperatorCommandError("reconcile requires a reconcile command")
        if command.input_ref != evidence_ref or command.input_sha256 != _sha256(evidence_bytes):
            raise OperatorCommandError("receipt input reference or sha256 mismatch")
        replay = self._replay_if_present(command)
        if replay is not None:
            return replay
        evidence = self._parse_receipt(evidence_bytes)
        self._validate_receipt_admission(command, evidence)

        def executor(_: OperatorCommand) -> OperatorExecution:
            result = database.reconcile_submission_receipt(dict(evidence), conn=self.connection)
            if result.get("status") != "applied":
                return OperatorExecution(False, "receipt_not_applied", "blocked")
            snapshot = self._receipt_snapshot(evidence)
            if snapshot is None:
                return OperatorExecution(False, "receipt_binding_missing", "blocked")
            return OperatorExecution(
                True,
                "receipt_reconciled",
                "completed",
                result_ref=f"receipt:{_canonical_digest([evidence['source'], evidence['receipt_id']])}",
                result_sha256=_canonical_digest(snapshot),
            )

        def verifier(_: OperatorCommand, execution: OperatorExecution) -> bool:
            snapshot = self._receipt_snapshot(evidence)
            expected_ref = f"receipt:{_canonical_digest([evidence['source'], evidence['receipt_id']])}"
            return bool(
                snapshot is not None
                and execution.result_ref == expected_ref
                and execution.result_sha256 == _canonical_digest(snapshot)
            )

        return OperatorCommandService(
            self.connection,
            executor=executor,
            verifier=verifier,
        ).execute(command)

    def resume(self, command: OperatorCommand, *, request_id: str) -> OperatorCommandResult:
        if command.action != "resume":
            raise OperatorCommandError("resume requires a resume command")
        replay = self._replay_if_present(command)
        if replay is not None and replay.status != "requested":
            if replay.resolved:
                self._consume_human_request(request_id)
            return replay
        prepared = self._prepare_resume(command, request_id)
        if self.resume_owner is None:
            return OperatorCommandService(self.connection).request_resume(command)
        if replay is not None:
            persisted = self.load_requested_resume(command.exception_id)
            if persisted is None or persisted.command_id != command.command_id:
                raise OperatorCommandError("requested resume command is missing or ambiguous")
            command = persisted

        before_turns = self._child_turn_ids(command.run_id)
        if before_turns:
            raise OperatorCommandError("resume parent already has a durable child turn")
        resume_context = prepared["resume_context"]

        def executor(supplied: OperatorCommand) -> OperatorExecution:
            return self.resume_owner(supplied, resume_context)  # type: ignore[arg-type]

        def verifier(_: OperatorCommand, execution: OperatorExecution) -> bool:
            new_children = self._child_turn_ids(command.run_id) - before_turns
            if len(new_children) != 1:
                return False
            child_id = next(iter(new_children))
            try:
                expected = verified_child_execution(
                    self.connection,
                    actor_id=command.actor_id,
                    attempt_id=command.attempt_id,
                    parent_turn_id=command.run_id,
                    child_turn_id=child_id,
                )
            except OperatorCommandError:
                return False
            return bool(
                execution.result_ref == expected.result_ref
                and execution.result_sha256 == expected.result_sha256
            )

        result = OperatorCommandService(
            self.connection,
            executor=executor,
            verifier=verifier,
        ).execute(command)
        if result.resolved:
            self._consume_human_request(request_id)
        return result

    def load_requested_resume(self, exception_id: str) -> OperatorCommand | None:
        """Load one current dispatcher request with its original accepted timestamp."""
        return load_requested_resume_command(self.connection, exception_id)

    def expire_resume_request(
        self,
        exception_id: str,
        *,
        request_id: str,
        expired_at: datetime | None = None,
    ) -> RequestedResumeExpiry:
        """Expire an exact request/exception pair without consuming its response."""
        when = expired_at or datetime.now(UTC)
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("expired_at must be timezone-aware")
        item = agent_control.get_exception(self.connection, exception_id)
        request = agent_control.get_human_request(self.connection, request_id)
        if item is None or request is None:
            raise OperatorCommandError("resume exception or human request is missing")
        if (
            item.queue_kind != "human_only"
            or item.context.get("request_id") != request_id
            or request.run_id != item.run_id
            or request.attempt_id != item.attempt_id
            or request.context.get("actor_id") != item.actor_id
            or request.context.get("turn_id") != item.turn_id
        ):
            raise OperatorCommandError("resume expiry lineage drifted")
        commands = requested_resume_commands(self.connection, exception_id)
        if len(commands) > 1:
            raise OperatorCommandError("multiple requested resume commands exist for one exception")
        command = commands[0] if commands else None
        if command is not None and (
            command.run_id,
            command.attempt_id,
            command.actor_id,
            command.turn_id,
        ) != (item.run_id, item.attempt_id, item.actor_id, item.turn_id):
            raise OperatorCommandError("requested resume command lineage drifted")
        if item.status == "expired" and request.status == "expired":
            if command is not None:
                terminal = OperatorCommandService(self.connection).replay(command)
                if terminal is None or terminal.status != "blocked":
                    raise OperatorCommandError("expired resume command lifecycle drifted")
            return RequestedResumeExpiry(
                exception_id,
                request_id,
                None if command is None else command.command_id,
                replayed=True,
            )
        if item.status != "open" or request.status != "open":
            raise OperatorCommandError("resume expiry lifecycle drifted")
        with _atomic(self.connection, "operator_resume_expiry"):
            if command is not None:
                OperatorCommandService(self.connection).expire_requested_resume(
                    command,
                    expired_at=when,
                )
            if not agent_control.expire_exception_cas(
                self.connection,
                exception_id=item.exception_id,
                run_id=item.run_id,
                attempt_id=item.attempt_id,
                actor_id=item.actor_id,
                turn_id=item.turn_id,
                expired_at=when.isoformat(),
            ):
                raise OperatorCommandError("resume exception expiry CAS failed")
            if not agent_control.expire_human_request_cas(
                self.connection,
                request_id=request.request_id,
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                expired_at=when.isoformat(),
            ):
                raise OperatorCommandError("human request expiry CAS failed")
        return RequestedResumeExpiry(
            exception_id,
            request_id,
            None if command is None else command.command_id,
        )

    def _parse_receipt(self, raw: bytes) -> Mapping[str, object]:
        if not isinstance(raw, bytes) or len(raw) > 16_384:
            raise OperatorCommandError("receipt evidence must be compact JSON bytes")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OperatorCommandError("receipt evidence is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict) or set(value) - _RECEIPT_KEYS:
            raise OperatorCommandError("receipt evidence contains non-allowlisted fields")
        if _RECEIPT_REQUIRED - set(value):
            raise OperatorCommandError("receipt evidence is missing exact binding fields")
        for key, item in value.items():
            if not isinstance(item, str) or not item.strip():
                raise OperatorCommandError(f"receipt {key} must be a non-empty string")
        normalized = {key: item.strip() for key, item in value.items()}
        normalized["source"] = normalized["source"].casefold()
        if normalized["source"] not in _RECEIPT_SOURCES:
            raise OperatorCommandError("receipt source is not allowlisted")
        return normalized

    def _validate_receipt_admission(
        self,
        command: OperatorCommand,
        evidence: Mapping[str, object],
    ) -> None:
        item = agent_control.get_exception(self.connection, command.exception_id)
        if item is None or item.queue_kind != "receipt_reconciliation" or item.status != "open":
            raise OperatorCommandError("receipt command requires its exact open reconciliation lane")
        if _text(evidence, "attempt_id") != command.attempt_id:
            raise OperatorCommandError("receipt attempt identity mismatch")
        attempt = self.connection.execute(
            "SELECT job_url,submit_started,status FROM application_attempts WHERE attempt_id=?",
            (command.attempt_id,),
        ).fetchone()
        gate = self.connection.execute(
            "SELECT gate_id,batch_id,job_url,attempt_id,state FROM application_submission_gates WHERE attempt_id=?",
            (command.attempt_id,),
        ).fetchone()
        job = self.connection.execute(
            "SELECT apply_status FROM jobs WHERE url=?",
            (_text(evidence, "job_url"),),
        ).fetchone()
        expected = (
            _text(evidence, "gate_id"),
            _text(evidence, "batch_id"),
            _text(evidence, "job_url"),
            command.attempt_id,
            "submission_uncertain",
        )
        if (
            attempt is None
            or gate is None
            or job is None
            or tuple(str(value) for value in gate) != expected
            or str(attempt[0]) != expected[2]
            or int(attempt[1]) != 1
            or str(job[0]) != "submission_uncertain"
        ):
            raise OperatorCommandError("receipt job, attempt, or submission gate identity drifted")

    def _receipt_snapshot(self, evidence: Mapping[str, object]) -> Sequence[object] | None:
        row = self.connection.execute(
            "SELECT r.receipt_digest,r.job_url,b.gate_id,b.batch_id,b.attempt_id,g.state,"
            "c.status,j.apply_status FROM application_receipts r "
            "JOIN application_receipt_gate_bindings b USING(receipt_source,receipt_id) "
            "JOIN application_submission_gates g ON g.gate_id=b.gate_id "
            "JOIN application_batch_consumptions c ON c.batch_id=b.batch_id AND c.job_url=b.job_url "
            "JOIN jobs j ON j.url=b.job_url WHERE r.receipt_source=? AND r.receipt_id=? "
            "AND b.gate_id=? AND b.batch_id=? AND b.job_url=? AND b.attempt_id=?",
            tuple(
                _text(evidence, key) for key in ("source", "receipt_id", "gate_id", "batch_id", "job_url", "attempt_id")
            ),
        ).fetchone()
        if (
            row is None
            or str(row[0]) != self._expected_receipt_digest(evidence)
            or tuple(str(value) for value in row[5:]) != ("applied", "applied", "applied")
        ):
            return None
        return tuple(str(value) for value in row)

    @staticmethod
    def _expected_receipt_digest(evidence: Mapping[str, object]) -> str:
        cleaned = {
            "source": _text(evidence, "source").casefold(),
            "receipt_id": _text(evidence, "receipt_id")[:500],
            "job_url": _text(evidence, "job_url"),
            "company_name": _text(evidence, "company_name")[:300],
            "job_title": _text(evidence, "job_title")[:300],
            "confirmation_text": str(evidence.get("confirmation_text") or "").strip()[:1000],
            "portal_status": " ".join(str(evidence.get("portal_status") or "").casefold().split())[:200],
        }
        return _canonical_digest(cleaned)

    def _prepare_resume(self, command: OperatorCommand, request_id: str) -> Mapping[str, object]:
        item = agent_control.get_exception(self.connection, command.exception_id)
        if (
            item is None
            or item.status != "open"
            or item.queue_kind != "human_only"
        ):
            raise OperatorCommandError("resume requires its exact open pre-submit queue lane")
        context = item.context
        required_context = {
            "request_id",
            "checkpoint_id",
            "job_url",
            "profile_id",
            "browser_lease_id",
            "browser_lease_epoch",
            "page_target_id",
            "page_epoch",
        }
        if required_context - set(context):
            raise OperatorCommandError("resume exception lacks exact request/checkpoint/lease binding")
        if _text(context, "request_id") != request_id:
            raise OperatorCommandError("human request identity drifted")
        response = load_human_response(self.connection, request_id)
        if response is None:
            raise OperatorCommandError("durable human response is missing")
        request_input_ref = f"human-response:{_sha256(request_id.encode('utf-8'))}"
        if command.input_ref != request_input_ref or command.input_sha256 != response.response_digest:
            raise OperatorCommandError("human response ref or digest mismatch")
        requests = [
            request
            for request in agent_control.list_open_human_requests(self.connection, attempt_id=command.attempt_id)
            if request.request_id == request_id
        ]
        if len(requests) != 1:
            raise OperatorCommandError("human request is not uniquely open")
        request = requests[0]
        if (
            request.run_id != command.run_id
            or request.attempt_id != command.attempt_id
            or request.context.get("actor_id") != command.actor_id
            or request.context.get("turn_id") != command.turn_id
            or response.response_type != request.request_type
            or response.resolved_at < request.created_at
        ):
            raise OperatorCommandError("human request/response identity or time drifted")
        parent = runtime_control.get_runtime_turn(self.connection, command.run_id)
        checkpoint = agent_control.latest_actor_checkpoint(self.connection, command.actor_id)
        checkpoint_id = _text(context, "checkpoint_id")
        if (
            parent is None
            or parent.actor_id != command.actor_id
            or parent.attempt_id != command.attempt_id
            or parent.profile_id != _text(context, "profile_id")
            or parent.status not in _TERMINAL_PARENT
            or parent.submit_started != 0
            or checkpoint is None
            or checkpoint.schema_version != "2"
            or checkpoint.checkpoint_id != checkpoint_id
            or checkpoint.actor_id != command.actor_id
            or checkpoint.attempt_id != command.attempt_id
            or checkpoint.turn_id != command.run_id
        ):
            raise OperatorCommandError("resume parent or latest checkpoint drifted")
        attempt = self.connection.execute(
            "SELECT job_url,lease_expires_at,submit_started,status FROM application_attempts WHERE attempt_id=?",
            (command.attempt_id,),
        ).fetchone()
        lease = self.connection.execute(
            "SELECT profile_id,actor_id,attempt_id,runtime_id,lease_epoch,page_target_id,"
            "page_epoch,expires_at,status "
            "FROM browser_resource_leases WHERE lease_id=?",
            (_text(context, "browser_lease_id"),),
        ).fetchone()
        now = datetime.now(UTC)
        if (
            attempt is None
            or str(attempt[0]) != _text(context, "job_url")
            or int(attempt[2]) != 0
            or str(attempt[3]) != "in_progress"
            or datetime.fromisoformat(str(attempt[1])) <= now
            or lease is None
            or tuple(str(value) for value in lease[:4])
            != (parent.profile_id, command.actor_id, command.attempt_id, parent.runtime_id)
            or int(lease[4]) != int(context["browser_lease_epoch"])
            or str(lease[5]) != _text(context, "page_target_id")
            or int(lease[6]) != int(context["page_epoch"])
            or datetime.fromisoformat(str(lease[7])) <= now
            or str(lease[8]) != "active"
        ):
            raise OperatorCommandError("resume attempt, job, or browser lease drifted")
        resume_context = fresh_resume_context(
            self.connection,
            parent_run_id=command.run_id,
            checkpoint_ref=checkpoint_id,
            request_id=request_id,
        )
        return {
            "checkpoint_id": checkpoint_id,
            "checkpoint_sequence": checkpoint.sequence,
            "resume_context": resume_context,
        }

    def _consume_human_request(self, request_id: str) -> None:
        if agent_control.resolve_human_request(
            self.connection,
            request_id,
            status="consumed",
        ):
            self.connection.commit()
            return
        row = self.connection.execute(
            "SELECT status FROM agent_human_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None or str(row[0]) != "consumed":
            raise OperatorCommandError("verified resume could not consume its human request")

    def _child_turn_ids(self, parent_turn_id: str) -> set[str]:
        runtime_control.ensure_schema(self.connection)
        return {
            str(row[0])
            for row in self.connection.execute(
                "SELECT turn_id FROM agent_runtime_turns WHERE parent_turn_id=?",
                (parent_turn_id,),
            ).fetchall()
        }

    def _replay_if_present(self, command: OperatorCommand) -> OperatorCommandResult | None:
        if agent_control.get_operator_command(self.connection, command.command_id) is None:
            return None

        return OperatorCommandService(self.connection).replay(command)
