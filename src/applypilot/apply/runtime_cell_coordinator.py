"""Admission, scheduling, and teardown for the gated two-Cell runtime.

The coordinator allocates execution hosts only.  It intentionally exposes no
page-write, SubmissionGate, reservation, receipt, ledger, or Submit capability.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from applypilot.apply.browser_context_runtime import (
    ApplicationContextLease,
    BrowserContextRuntimeError,
    BrowserStateScope,
    HotBrowserContextRuntime,
    ScopedBrowserState,
)
from applypilot.apply.runtime_cell import RuntimeCellExecutionState
from applypilot.storage import runtime_cells as storage

RUNTIME_CELL_ADMISSION_SCHEMA = "applypilot-runtime-cell-admission/v1"
# The pinned integration baseline has unresolved App Server production-safety
# blockers.  A benchmark or local manifest cannot override this code gate.
APP_SERVER_PRODUCTION_CELL_ADMITTED = False
RUNTIME_CELL_GATE_NAMES = frozenset(
    {
        "same_domain_exclusion",
        "different_domain_parallelism",
        "stale_token_cas",
        "process_runtime_identity",
        "cross_cell_authority_rejection",
        "context_cleanup_zero_residual",
        "failure_recovery_matrix",
        "safety_counters_zero",
        "runtime_cell_host_lifecycle_benchmark",
        "app_server_production_safety",
    }
)
RUNTIME_CELL_GATE_SCHEMAS = MappingProxyType(
    {
        **{name: "applypilot-runtime-cell-gate-receipt/v1" for name in RUNTIME_CELL_GATE_NAMES},
        "runtime_cell_host_lifecycle_benchmark": ("applypilot-runtime-cell-host-lifecycle/v1"),
        "app_server_production_safety": ("applypilot-app-server-production-safety/v1"),
    }
)
RuntimeCellMode = Literal["off", "shadow", "canary"]


def source_manifest_identity(source_root: Path | str) -> str:
    """Hash the executable ApplyPilot Python source used by this generation."""

    root = Path(source_root).expanduser().resolve()
    if not (root / "pyproject.toml").is_file():
        raise ValueError("source_root must be the ApplyPilot repository root")
    files = sorted((root / "src" / "applypilot").rglob("*.py"))
    manifest = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in files
    ]
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: object, name: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase sha256")
    return text


@dataclass(frozen=True, slots=True)
class RuntimeCellGateReceipt:
    schema_version: str
    sha256: str

    def __post_init__(self) -> None:
        if not str(self.schema_version or "").strip():
            raise ValueError("gate receipt schema_version is required")
        object.__setattr__(self, "sha256", _digest(self.sha256, "gate receipt sha256"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RuntimeCellGateReceipt:
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            sha256=str(value.get("sha256") or ""),
        )

    def as_dict(self) -> dict[str, str]:
        return {"schema_version": self.schema_version, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class RuntimeCellAdmissionManifest:
    source_identity: str
    workers: int
    gate_receipts: Mapping[str, RuntimeCellGateReceipt]
    production_authority: bool
    authority_ref: str | None
    local_diagnostic: bool = False
    schema_version: str = RUNTIME_CELL_ADMISSION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_CELL_ADMISSION_SCHEMA:
            raise ValueError("unsupported Runtime Cell admission schema")
        object.__setattr__(self, "source_identity", _digest(self.source_identity, "source_identity"))
        if isinstance(self.workers, bool) or self.workers != 2:
            raise ValueError("Runtime Cell admission must explicitly bind workers=2")
        receipts = dict(self.gate_receipts)
        if set(receipts) != RUNTIME_CELL_GATE_NAMES:
            raise ValueError("Runtime Cell admission must contain every exact gate receipt")
        frozen: dict[str, RuntimeCellGateReceipt] = {}
        for name in sorted(receipts):
            receipt = receipts[name]
            if not isinstance(receipt, RuntimeCellGateReceipt):
                raise TypeError("gate receipts must use RuntimeCellGateReceipt")
            if receipt.schema_version != RUNTIME_CELL_GATE_SCHEMAS[name]:
                raise ValueError(f"gate receipt schema is not admissible: {name}")
            frozen[name] = receipt
        object.__setattr__(self, "gate_receipts", MappingProxyType(frozen))
        if self.production_authority and not str(self.authority_ref or "").strip():
            raise ValueError("production authority requires an authority_ref")
        if self.local_diagnostic and self.production_authority:
            raise ValueError("local diagnostic manifests cannot grant production authority")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RuntimeCellAdmissionManifest:
        receipts = value.get("gate_receipts")
        if not isinstance(receipts, Mapping):
            raise TypeError("gate_receipts must be an object")
        parsed_receipts: dict[str, RuntimeCellGateReceipt] = {}
        for key, item in receipts.items():
            if not isinstance(item, Mapping):
                raise TypeError("each gate receipt must be an object")
            parsed_receipts[str(key)] = RuntimeCellGateReceipt.from_mapping(item)
        return cls(
            source_identity=str(value.get("source_identity") or ""),
            workers=value.get("workers"),  # type: ignore[arg-type]
            gate_receipts=parsed_receipts,
            production_authority=value.get("production_authority") is True,
            authority_ref=(str(value["authority_ref"]) if value.get("authority_ref") is not None else None),
            local_diagnostic=value.get("local_diagnostic") is True,
            schema_version=str(value.get("schema_version") or ""),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_identity": self.source_identity,
            "workers": self.workers,
            "gate_receipts": {name: receipt.as_dict() for name, receipt in self.gate_receipts.items()},
            "production_authority": self.production_authority,
            "authority_ref": self.authority_ref,
            "local_diagnostic": self.local_diagnostic,
        }


def load_admission_manifest(path: Path | str) -> RuntimeCellAdmissionManifest:
    manifest_path = Path(path).expanduser().resolve()
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise TypeError("Runtime Cell admission manifest must be an object")
    return RuntimeCellAdmissionManifest.from_mapping(raw)


@dataclass(frozen=True, slots=True)
class RuntimeCellAdmissionDecision:
    mode: RuntimeCellMode
    requested_workers: int
    effective_cells: int
    status: str
    reasons: tuple[str, ...]
    source_identity: str
    production_authority: bool

    @property
    def canary_enabled(self) -> bool:
        return self.mode == "canary" and self.effective_cells == 2

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "applypilot-runtime-cell-decision/v1",
            "mode": self.mode,
            "requested_workers": self.requested_workers,
            "effective_cells": self.effective_cells,
            "status": self.status,
            "reasons": list(self.reasons),
            "source_identity": self.source_identity,
            "production_authority": self.production_authority,
        }


def resolve_runtime_cell_admission(
    *,
    mode: str,
    current_source_identity: str,
    requested_workers: int,
    manifest: RuntimeCellAdmissionManifest | None,
) -> RuntimeCellAdmissionDecision:
    normalized = str(mode or "").strip().casefold()
    if normalized not in {"off", "shadow", "canary"}:
        raise ValueError("Runtime Cell mode must be off, shadow, or canary")
    source_identity = _digest(current_source_identity, "current_source_identity")
    if isinstance(requested_workers, bool) or not isinstance(requested_workers, int) or requested_workers < 1:
        raise ValueError("requested_workers must be a positive integer")
    reasons: list[str] = []
    if normalized == "off":
        reasons.append("runtime_cell_mode_off")
    if manifest is None:
        reasons.append("admission_manifest_absent")
    else:
        if manifest.source_identity != source_identity:
            reasons.append("source_identity_mismatch")
        if manifest.workers != 2 or requested_workers != 2:
            reasons.append("workers_must_be_explicitly_two")
        if set(manifest.gate_receipts) != RUNTIME_CELL_GATE_NAMES:
            reasons.append("gate_receipts_incomplete")
        if manifest.local_diagnostic:
            reasons.append("local_diagnostic_has_no_production_authority")
        if not manifest.production_authority:
            reasons.append("production_authority_absent")
    admitted = (
        normalized == "canary"
        and manifest is not None
        and not reasons
        and requested_workers == 2
        and APP_SERVER_PRODUCTION_CELL_ADMITTED
    )
    if normalized == "canary" and not APP_SERVER_PRODUCTION_CELL_ADMITTED:
        reasons.append("app_server_production_cell_not_admitted")
    if normalized == "shadow" and not reasons:
        reasons.append("shadow_observes_but_never_executes_two_cells")
    return RuntimeCellAdmissionDecision(
        mode=normalized,  # type: ignore[arg-type]
        requested_workers=requested_workers,
        effective_cells=2 if admitted else 1,
        status="ADMITTED" if admitted else "NOT_ADMITTED",
        reasons=tuple(dict.fromkeys(reasons)),
        source_identity=source_identity,
        production_authority=bool(admitted and manifest and manifest.production_authority),
    )


def configured_runtime_cell_admission(
    *,
    mode: str,
    requested_workers: int,
    source_root: Path | str,
    manifest_path: Path | str | None,
) -> RuntimeCellAdmissionDecision:
    """Load a configured manifest fail-closed; malformed input never enables canary."""

    identity = source_manifest_identity(source_root)
    manifest = None
    if manifest_path:
        try:
            manifest = load_admission_manifest(manifest_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            manifest = None
    return resolve_runtime_cell_admission(
        mode=mode,
        current_source_identity=identity,
        requested_workers=requested_workers,
        manifest=manifest,
    )


@dataclass(frozen=True, slots=True)
class RuntimeCellBinding:
    cell_id: str
    generation: int
    runtime_id: str
    source_identity: str
    process_id: int
    process_birth_time: int


class _RuntimeCellCoordinatorBase:
    """Internal scheduler facade supporting shared-transaction claims."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        *,
        decision: RuntimeCellAdmissionDecision,
    ) -> None:
        self._connection_factory = connection_factory
        self.decision = decision

    def register(
        self,
        *,
        cell_index: int,
        generation: int,
        runtime_id: str,
        process_id: int,
        process_birth_time: int,
        connection: sqlite3.Connection | None = None,
    ) -> RuntimeCellBinding:
        if isinstance(cell_index, bool) or not 0 <= cell_index < self.decision.effective_cells:
            raise ValueError("cell_index is outside the effective Cell allocation")
        cell_id = f"runtime-cell-{cell_index}"
        owns = connection is None
        conn = connection or self._connection_factory()
        try:
            row = storage.register_generation(
                conn,
                cell_id=cell_id,
                generation=generation,
                runtime_id=runtime_id,
                source_identity=self.decision.source_identity,
                process_id=process_id,
                process_birth_time=process_birth_time,
            )
            return RuntimeCellBinding(
                row.cell_id,
                row.generation,
                row.runtime_id,
                row.source_identity,
                row.process_id,
                row.process_birth_time,
            )
        finally:
            if owns:
                conn.close()

    def register_next(
        self,
        *,
        cell_index: int,
        runtime_id: str,
        process_id: int,
        process_birth_time: int,
    ) -> RuntimeCellBinding:
        """Register the next generation under one transaction-owned Cell slot."""

        if isinstance(cell_index, bool) or not 0 <= cell_index < self.decision.effective_cells:
            raise ValueError("cell_index is outside the effective Cell allocation")
        cell_id = f"runtime-cell-{cell_index}"
        conn = self._connection_factory()
        try:
            conn.execute("SAVEPOINT runtime_cell_register_next")
            try:
                storage.ensure_schema(conn)
                row = conn.execute(
                    "SELECT COALESCE(MAX(generation),0) FROM runtime_cell_generations "
                    "WHERE cell_id=?",
                    (cell_id,),
                ).fetchone()
                generation = int(row[0]) + 1
                binding = self.register(
                    cell_index=cell_index,
                    generation=generation,
                    runtime_id=runtime_id,
                    process_id=process_id,
                    process_birth_time=process_birth_time,
                    connection=conn,
                )
                conn.execute("RELEASE SAVEPOINT runtime_cell_register_next")
            except BaseException:
                conn.execute("ROLLBACK TO SAVEPOINT runtime_cell_register_next")
                conn.execute("RELEASE SAVEPOINT runtime_cell_register_next")
                raise
            return binding
        finally:
            conn.close()

    def close_generation(
        self,
        binding: RuntimeCellBinding,
        *,
        context_cleanup_verified: bool,
        residual_resources: int | None,
    ) -> None:
        """Close an idle generation with its complete process/source identity."""

        conn = self._connection_factory()
        try:
            storage.close_generation_after_cleanup(
                conn,
                cell_id=binding.cell_id,
                generation=binding.generation,
                runtime_id=binding.runtime_id,
                source_identity=binding.source_identity,
                process_id=binding.process_id,
                process_birth_time=binding.process_birth_time,
                context_cleanup_verified=context_cleanup_verified,
                residual_resources=residual_resources,
            )
        finally:
            conn.close()

    def claim(
        self,
        binding: RuntimeCellBinding,
        *,
        application_id: str,
        actor_id: str,
        attempt_id: str,
        application_url: str,
        ttl_seconds: int = 300,
        connection: sqlite3.Connection | None = None,
    ) -> storage.RuntimeCellLeaseToken:
        parsed = urlsplit(application_url)
        hostname = parsed.hostname
        if parsed.scheme not in {"https", "http"} or not hostname:
            raise ValueError("application_url must contain an exact HTTP(S) hostname")
        owns = connection is None
        conn = connection or self._connection_factory()
        try:
            lease = storage.claim_lease(
                conn,
                lease_id=f"cell-lease-{uuid.uuid4()}",
                cell_id=binding.cell_id,
                generation=binding.generation,
                runtime_id=binding.runtime_id,
                source_identity=binding.source_identity,
                application_id=application_id,
                actor_id=actor_id,
                attempt_id=attempt_id,
                hostname=hostname,
                ttl_seconds=ttl_seconds,
            )
            return storage.token_from_lease(lease)
        finally:
            if owns:
                conn.close()


class RuntimeCellCoordinator(_RuntimeCellCoordinatorBase):
    """Production coordinator whose Cell ceiling is always self-evaluated."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        *,
        mode: str,
        requested_workers: int,
        source_root: Path | str,
        manifest_path: Path | str | None,
    ) -> None:
        decision = configured_runtime_cell_admission(
            mode=mode,
            requested_workers=requested_workers,
            source_root=source_root,
            manifest_path=manifest_path,
        )
        if not APP_SERVER_PRODUCTION_CELL_ADMITTED and decision.effective_cells != 1:
            raise RuntimeError("disabled App Server production gate cannot allocate two Cells")
        super().__init__(connection_factory, decision=decision)


class DiagnosticRuntimeCellCoordinator(_RuntimeCellCoordinatorBase):
    """Explicit non-production scheduler used only by local microbenchmarks/tests."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        *,
        source_identity: str,
        cells: int,
    ) -> None:
        if isinstance(cells, bool) or cells not in {1, 2}:
            raise ValueError("diagnostic cells must be 1 or 2")
        decision = RuntimeCellAdmissionDecision(
            mode="off",
            requested_workers=cells,
            effective_cells=cells,
            status="DIAGNOSTIC_ONLY",
            reasons=("diagnostic_coordinator_has_no_production_authority",),
            source_identity=_digest(source_identity, "source_identity"),
            production_authority=False,
        )
        super().__init__(connection_factory, decision=decision)


def recovery_disposition(state: RuntimeCellExecutionState | None, *, state_readable: bool = True) -> str:
    """Return the only safe recovery lane for an observed App Server failure."""

    if not state_readable or state is None:
        return "park"
    if state.submit_started:
        return "receipt_only"
    if state.request_accepted or state.tool_or_effect_started:
        return "park"
    return "fallback"


@dataclass(slots=True)
class RuntimeCellApplication:
    lease_token: storage.RuntimeCellLeaseToken
    context_lease: ApplicationContextLease
    agent_stop: Callable[[], None]
    contain_runtime: Callable[[], None]


class RuntimeCellHost:
    """Join one App Server stop port and one M5 context host per Cell.

    Teardown order is invariant: stop Agent, close/verify the application
    context, then release the durable domain/cell lease.  Any unreadable or
    residual state quarantines the generation, which can never be reused.
    """

    def __init__(
        self,
        *,
        coordinator: _RuntimeCellCoordinatorBase,
        binding: RuntimeCellBinding,
        context_runtime: HotBrowserContextRuntime,
        connection_factory: Callable[[], sqlite3.Connection],
    ) -> None:
        self.coordinator = coordinator
        self.binding = binding
        self.context_runtime = context_runtime
        self._connection_factory = connection_factory

    def open_application(
        self,
        *,
        application_id: str,
        actor_id: str,
        attempt_id: str,
        application_url: str,
        scope: BrowserStateScope,
        state: ScopedBrowserState,
        agent_stop: Callable[[], None],
        contain_runtime: Callable[[], None],
    ) -> RuntimeCellApplication:
        token = self.coordinator.claim(
            self.binding,
            application_id=application_id,
            actor_id=actor_id,
            attempt_id=attempt_id,
            application_url=application_url,
        )
        try:
            context_lease = self.context_runtime.open_application(
                application_id=application_id, scope=scope, state=state
            )
        except BaseException as open_error:
            try:
                contain_runtime()
            except BaseException as containment_error:  # noqa: BLE001
                open_error.add_note(f"runtime containment also failed: {type(containment_error).__name__}")
            try:
                self.context_runtime.close()
            except BrowserContextRuntimeError as context_close_error:
                open_error.add_note(f"context containment also failed: {type(context_close_error).__name__}")
            conn = self._connection_factory()
            try:
                storage.quarantine_after_cleanup_failure(
                    conn,
                    token,
                    reason="context_open_failed",
                )
            finally:
                conn.close()
            raise
        return RuntimeCellApplication(token, context_lease, agent_stop, contain_runtime)

    def open_claimed_application(
        self,
        *,
        lease_token: storage.RuntimeCellLeaseToken,
        application_id: str,
        actor_id: str,
        attempt_id: str,
        application_url: str,
        scope: BrowserStateScope,
        state: ScopedBrowserState,
        agent_stop: Callable[[], None],
        contain_runtime: Callable[[], None],
    ) -> RuntimeCellApplication:
        """Adopt the exact lease atomically acquired with the job attempt."""

        parsed = urlsplit(application_url)
        hostname = storage.normalize_hostname(parsed.hostname)
        if (
            lease_token.cell_id != self.binding.cell_id
            or lease_token.generation != self.binding.generation
            or lease_token.runtime_id != self.binding.runtime_id
            or lease_token.application_id != application_id
            or lease_token.actor_id != actor_id
            or lease_token.attempt_id != attempt_id
            or lease_token.hostname != hostname
        ):
            raise storage.StaleRuntimeCellTokenError(
                "preclaimed Runtime Cell lease does not match this host"
            )
        conn = self._connection_factory()
        try:
            storage.heartbeat_lease(conn, lease_token)
        finally:
            conn.close()
        try:
            context_lease = self.context_runtime.open_application(
                application_id=lease_token.application_id,
                scope=scope,
                state=state,
            )
        except BaseException as open_error:
            try:
                contain_runtime()
            except BaseException as containment_error:  # noqa: BLE001
                open_error.add_note(
                    "runtime containment also failed: "
                    f"{type(containment_error).__name__}"
                )
            try:
                self.context_runtime.close()
            except BrowserContextRuntimeError as context_close_error:
                open_error.add_note(
                    "context containment also failed: "
                    f"{type(context_close_error).__name__}"
                )
            conn = self._connection_factory()
            try:
                storage.quarantine_after_cleanup_failure(
                    conn,
                    lease_token,
                    reason="context_open_failed",
                )
            finally:
                conn.close()
            raise
        return RuntimeCellApplication(
            lease_token,
            context_lease,
            agent_stop,
            contain_runtime,
        )

    def close_application(self, application: RuntimeCellApplication) -> None:
        agent_stopped = False
        cleanup_verified = False
        residual: int | None = None
        failure: BaseException | None = None
        try:
            application.agent_stop()
            agent_stopped = True
        except BaseException as exc:  # noqa: BLE001 - containment continues after stop ambiguity.
            failure = exc
        if not agent_stopped:
            try:
                application.contain_runtime()
            except BaseException as exc:  # noqa: BLE001 - browser containment must still run.
                failure = failure or exc
            try:
                self.context_runtime.close()
            except BrowserContextRuntimeError as exc:
                failure = failure or exc
        else:
            try:
                self.context_runtime.close_application(application.context_lease)
            except BrowserContextRuntimeError as exc:
                failure = failure or exc
                try:
                    application.contain_runtime()
                except BaseException as containment_exc:  # noqa: BLE001
                    failure = failure or containment_exc
                try:
                    self.context_runtime.close()
                except BrowserContextRuntimeError as containment_exc:
                    failure = failure or containment_exc
        metrics = self.context_runtime.metrics
        residual = (
            metrics.active_contexts
            + metrics.pages_after_close
            + metrics.frames_after_close
            + metrics.service_workers_after_close
        )
        cleanup_verified = residual == 0 and (agent_stopped or metrics.closed)
        conn = self._connection_factory()
        try:
            storage.release_after_cleanup(
                conn,
                application.lease_token,
                agent_stopped=agent_stopped,
                context_cleanup_verified=cleanup_verified,
                residual_resources=residual,
            )
        except storage.RuntimeCellQuarantinedError as exc:
            failure = failure or exc
        finally:
            conn.close()
        if failure is not None:
            raise storage.RuntimeCellQuarantinedError("Runtime Cell application cleanup was quarantined") from failure

    def close(self) -> None:
        """Close the idle context runtime before releasing its generation identity."""

        failure: BaseException | None = None
        try:
            self.context_runtime.close()
        except BrowserContextRuntimeError as exc:
            failure = exc
        metrics = self.context_runtime.metrics
        residual = (
            metrics.active_contexts
            + metrics.pages_after_close
            + metrics.frames_after_close
            + metrics.service_workers_after_close
        )
        try:
            self.coordinator.close_generation(
                self.binding,
                context_cleanup_verified=failure is None and metrics.closed,
                residual_resources=residual,
            )
        except (
            storage.RuntimeCellQuarantinedError,
            storage.StaleRuntimeCellTokenError,
        ) as exc:
            failure = failure or exc
        if failure is not None:
            raise storage.RuntimeCellQuarantinedError(
                "Runtime Cell generation shutdown was quarantined"
            ) from failure


__all__ = [
    "APP_SERVER_PRODUCTION_CELL_ADMITTED",
    "RUNTIME_CELL_ADMISSION_SCHEMA",
    "RUNTIME_CELL_GATE_NAMES",
    "RUNTIME_CELL_GATE_SCHEMAS",
    "DiagnosticRuntimeCellCoordinator",
    "RuntimeCellAdmissionDecision",
    "RuntimeCellAdmissionManifest",
    "RuntimeCellApplication",
    "RuntimeCellBinding",
    "RuntimeCellCoordinator",
    "RuntimeCellGateReceipt",
    "RuntimeCellHost",
    "configured_runtime_cell_admission",
    "load_admission_manifest",
    "recovery_disposition",
    "resolve_runtime_cell_admission",
    "source_manifest_identity",
]
