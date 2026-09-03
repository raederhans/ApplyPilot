"""Fail-closed worker admission and one-way runtime downshift policy.

Local cohort evidence is deliberately diagnostic.  It may describe workers that
met the statistical gates, but it cannot by itself raise the production worker
cap.  A caller must supply a separately admitted manifest with an external
authority reference before the governor can allow more than one worker.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

REPORT_SCHEMA_VERSION = "applypilot-full-stack-no-submit/v1"
ADMISSION_SCHEMA_VERSION = "applypilot-performance-admission/v1"

DEFAULT_THRESHOLDS: dict[str, float] = {
    "two_worker_speedup_lower_bound": 1.6,
    "four_worker_speedup_lower_bound": 2.6,
    "job_p95_ratio_max": 1.2,
    "available_memory_fraction_max": 0.60,
    "sqlite_lock_wait_p95_ms_max": 50.0,
    "sqlite_lock_wait_max_ms_max": 500.0,
    "submit_lane_wait_p95_ms_max": 250.0,
    "submit_lane_wait_max_ms_max": 1000.0,
    "submit_lane_hold_p95_ms_max": 200.0,
}

_CIRCUIT_FIELDS = (
    "duplicate_submit_attempts",
    "actor_cross_talk",
    "profile_cross_talk",
    "page_cross_talk",
    "admitted_stale_writes",
    "repeated_admitted_actions",
    "receipt_identity_drift",
)


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return numeric


@dataclass(frozen=True, slots=True)
class AdmissionManifest:
    """Version-bound worker ceiling admitted by an authority outside the benchmark."""

    source_manifest_sha256: str
    fixture_sha256: str
    thresholds_sha256: str
    admitted_workers: int = 1
    default_workers: int = 1
    production_authority: bool = False
    authority_ref: str | None = None
    schema_version: str = ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ADMISSION_SCHEMA_VERSION:
            raise ValueError("unsupported performance admission schema")
        for name in (
            "source_manifest_sha256",
            "fixture_sha256",
            "thresholds_sha256",
        ):
            value = str(getattr(self, name) or "")
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be lowercase sha256")
        admitted = _positive_int(self.admitted_workers, "admitted_workers")
        default = _positive_int(self.default_workers, "default_workers")
        if admitted not in {1, 2, 4} or default not in {1, 2} or default > admitted:
            raise ValueError("worker admission must use conservative 1/2/4 bounds")
        if self.production_authority and not str(self.authority_ref or "").strip():
            raise ValueError("production authority requires an authority_ref")

    @classmethod
    def local_diagnostic(
        cls,
        *,
        source_manifest_sha256: str,
        fixture_sha256: str,
        eligible_workers: int,
    ) -> AdmissionManifest:
        return cls(
            source_manifest_sha256=source_manifest_sha256,
            fixture_sha256=fixture_sha256,
            thresholds_sha256=_sha256(DEFAULT_THRESHOLDS),
            admitted_workers=max(1, min(eligible_workers, 4)),
            default_workers=1,
            production_authority=False,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_manifest_sha256": self.source_manifest_sha256,
            "fixture_sha256": self.fixture_sha256,
            "thresholds_sha256": self.thresholds_sha256,
            "admitted_workers": self.admitted_workers,
            "default_workers": self.default_workers,
            "production_authority": self.production_authority,
            "authority_ref": self.authority_ref,
        }


@dataclass(frozen=True, slots=True)
class GovernorDecision:
    requested_workers: int
    effective_workers: int
    state: str
    reasons: tuple[str, ...]
    circuit_open: bool


@dataclass(slots=True)
class PerformanceGovernor:
    """Clamp to an admitted ceiling and only move downward during one run."""

    manifest: AdmissionManifest
    requested_workers: int
    explicit_four_workers: bool = False
    rss_budget_bytes: int | None = None
    available_memory_bytes: int | None = None
    _effective_workers: int = field(init=False, repr=False)
    _reasons: list[str] = field(default_factory=list, init=False, repr=False)
    _circuit_open: bool = field(default=False, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        requested = _positive_int(self.requested_workers, "requested_workers")
        if requested not in {1, 2, 4}:
            raise ValueError("requested_workers must be 1, 2, or 4")
        if self.rss_budget_bytes is not None:
            _positive_int(self.rss_budget_bytes, "rss_budget_bytes")
        if self.available_memory_bytes is not None:
            _positive_int(self.available_memory_bytes, "available_memory_bytes")
        ceiling = self.manifest.admitted_workers if self.manifest.production_authority else 1
        if not self.manifest.production_authority:
            self._reasons.append("local_diagnostic_has_no_production_authority")
        if requested == 4 and not self.explicit_four_workers:
            ceiling = min(ceiling, 2)
            self._reasons.append("four_workers_require_explicit_opt_in")
        self._effective_workers = min(requested, ceiling)
        if self._effective_workers < requested:
            self._reasons.append("requested_workers_clamped_to_admitted_ceiling")

    @property
    def effective_workers(self) -> int:
        with self._lock:
            return self._effective_workers

    def permits(self, worker_id: int) -> bool:
        if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id < 0:
            raise ValueError("worker_id must be a non-negative integer")
        with self._lock:
            return not self._circuit_open and worker_id < self._effective_workers

    def observe(self, sample: Mapping[str, object]) -> GovernorDecision:
        """Apply one bounded sample.  No sample can increase the worker count."""

        with self._lock:
            circuit = [name for name in _CIRCUIT_FIELDS if _non_negative(sample.get(name, 0), name) > 0]
            rss = _non_negative(sample.get("process_tree_rss_bytes", 0), "process_tree_rss_bytes")
            if self.rss_budget_bytes is not None and rss > self.rss_budget_bytes:
                circuit.append("rss_budget_exceeded")
            if (
                self.available_memory_bytes is not None
                and rss > self.available_memory_bytes * DEFAULT_THRESHOLDS["available_memory_fraction_max"]
            ):
                circuit.append("available_memory_fraction_exceeded")
            if circuit:
                self._circuit_open = True
                self._effective_workers = 0
                self._reasons.extend(f"circuit_open:{name}" for name in circuit)
                return self.decision()

            pressure: list[str] = []
            if _non_negative(sample.get("sqlite_busy_errors", 0), "sqlite_busy_errors") > 0:
                pressure.append("sqlite_busy")
            if pressure and self._effective_workers > 1:
                self._effective_workers = 2 if self._effective_workers == 4 else 1
                self._reasons.extend(f"downshift:{item}" for item in pressure)
            return self.decision()

    def decision(self) -> GovernorDecision:
        with self._lock:
            return GovernorDecision(
                requested_workers=self.requested_workers,
                effective_workers=self._effective_workers,
                state="circuit_open" if self._circuit_open else "active",
                reasons=tuple(dict.fromkeys(self._reasons)),
                circuit_open=self._circuit_open,
            )


def load_cohort_report(path: Path | str) -> dict[str, Any]:
    report_path = Path(path).expanduser().resolve()
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError("unsupported cohort report schema")
    return raw


def inspect_cohort_report(path: Path | str) -> dict[str, object]:
    """Return a compact read-only operator projection with explicit reasons."""

    raw = load_cohort_report(path)
    admission = raw.get("admission") if isinstance(raw.get("admission"), Mapping) else {}
    return {
        "read_only": True,
        "schema_version": raw["schema_version"],
        "fixture_sha256": raw.get("fixture_sha256"),
        "source_manifest_sha256": raw.get("source_manifest_sha256"),
        "eligible_workers": admission.get("eligible_workers", 1),
        "production_admitted_workers": admission.get("production_admitted_workers", 1),
        "status": admission.get("status", "NOT_ADMITTED"),
        "local_gate_status": admission.get("local_gate_status", "NOT_QUALIFIED"),
        "reasons": list(admission.get("reasons", ())),
        "speedups": raw.get("speedups", {}),
        "gates": raw.get("gates", {}),
        "governor": raw.get("governor", {}),
    }


__all__ = [
    "ADMISSION_SCHEMA_VERSION",
    "DEFAULT_THRESHOLDS",
    "REPORT_SCHEMA_VERSION",
    "AdmissionManifest",
    "GovernorDecision",
    "PerformanceGovernor",
    "inspect_cohort_report",
    "load_cohort_report",
]
