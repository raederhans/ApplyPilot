"""Bounded advisory timing spans for application-path attribution.

This module cannot grant authority or alter an application result. Production
callers use only the ``advisory_*`` helpers, which swallow telemetry failures.
Spans are nested diagnostics, not additive wall-clock accounting.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

SCHEMA_VERSION = "applypilot-performance-attribution/v2"
TRACE_JOB_KEY = "_performance_attribution_trace"
ROUTE_JOB_KEY = "_performance_attribution_route"
MAX_DURATION_MS = 86_400_000.0
MAX_SPANS = 32
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$"
)
_PROVIDERS = frozenset(
    {"workday", "smartrecruiters", "greenhouse", "lever", "linkedin", "direct_email"}
)

SPAN_GROUPS = {
    "agent.turn": "agent",
    "agent.startup": "agent",
    "mcp.first_tool_ready": "mcp",
    "model.first_output": "agent",
    "model.first_tool_decision": "agent",
    "browser.prepare": "browser",
    "audit.pre_submit": "observation",
    "submit.agent": "submission",
    "receipt.reconciliation": "observation",
    "recovery.agent": "recovery",
}
_GROUP_ROOT_SPANS = {
    "agent": frozenset({"agent.turn"}),
    "mcp": frozenset({"mcp.first_tool_ready"}),
    "observation": frozenset({"audit.pre_submit", "receipt.reconciliation"}),
    "recovery": frozenset({"recovery.agent"}),
}
_PRIMARY_TURN_SPANS = frozenset({"browser.prepare", "submit.agent"})


def _safe_hostname(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 253 or not value:
        return None
    candidate = value.casefold()
    if any(character.isspace() or ord(character) < 32 for character in candidate):
        return None
    parsed = urlparse(f"https://{candidate}")
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.hostname != candidate
        or not _HOSTNAME_RE.fullmatch(candidate)
    ):
        return None
    return candidate


def _route_dimensions(job: Mapping[str, object]) -> dict[str, int | str]:
    route = job.get(ROUTE_JOB_KEY)
    if not isinstance(route, Mapping):
        return {
            "provider": "unavailable",
            "domain": "unavailable",
            "worker_application_index": "unavailable",
            "worker_id": "unavailable",
        }
    provider = route.get("provider")
    domain = _safe_hostname(route.get("domain"))
    index = route.get("worker_application_index")
    worker_id = route.get("worker_id")
    return {
        "provider": provider if isinstance(provider, str) and provider in _PROVIDERS else "unavailable",
        "domain": domain or "unavailable",
        "worker_application_index": (
            index if isinstance(index, int) and not isinstance(index, bool) and index > 0 else "unavailable"
        ),
        "worker_id": (
            worker_id
            if isinstance(worker_id, int) and not isinstance(worker_id, bool) and worker_id >= 0
            else "unavailable"
        ),
    }


def bind_attempt_route(
    job: dict[str, object],
    *,
    provider: object,
    target_url: object,
    worker_application_index: object,
    worker_id: object,
) -> None:
    """Bind dimensions only from admitted ATS/authorization route facts."""

    candidate_provider = str(provider or "").strip().casefold()
    parsed = urlparse(str(target_url or ""))
    hostname = _safe_hostname(parsed.hostname or "")
    if candidate_provider not in _PROVIDERS or hostname is None:
        return
    if (
        isinstance(worker_application_index, bool)
        or not isinstance(worker_application_index, int)
        or worker_application_index <= 0
        or isinstance(worker_id, bool)
        or not isinstance(worker_id, int)
        or worker_id < 0
    ):
        return
    job[ROUTE_JOB_KEY] = {
        "provider": candidate_provider,
        "domain": hostname,
        "worker_application_index": worker_application_index,
        "worker_id": worker_id,
    }
    existing = job.get(TRACE_JOB_KEY)
    if isinstance(existing, PerformanceTrace):
        existing.dimensions = _route_dimensions(job)


def safe_bind_attempt_route(
    job: dict[str, object],
    **kwargs: object,
) -> None:
    try:
        bind_attempt_route(job, **kwargs)
    except Exception:  # noqa: BLE001 - telemetry must never affect authority
        return


@dataclass(slots=True)
class PerformanceTrace:
    dimensions: dict[str, int | str]
    _spans_ms: dict[str, float] = field(default_factory=dict)

    def record(self, name: str, duration_ms: object) -> None:
        if name not in SPAN_GROUPS:
            raise ValueError(f"unknown performance attribution span: {name}")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
            raise TypeError("attribution duration must be a number")
        duration = float(duration_ms)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("attribution duration must be finite and non-negative")
        self._spans_ms[name] = min(
            MAX_DURATION_MS,
            self._spans_ms.get(name, 0.0) + min(duration, MAX_DURATION_MS),
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "measurement_model": "nested_advisory_spans",
            "dimensions": dict(self.dimensions),
            "spans": [
                {
                    "name": name,
                    "group": SPAN_GROUPS[name],
                    "duration_ms": round(duration, 3),
                    "dimensions": dict(self.dimensions),
                }
                for name, duration in sorted(self._spans_ms.items())
            ],
        }


class _NoopTrace:
    def record(self, _name: str, _duration_ms: object) -> None:
        return


def trace_for_job(job: dict[str, object]) -> PerformanceTrace:
    existing = job.get(TRACE_JOB_KEY)
    if isinstance(existing, PerformanceTrace):
        return existing
    trace = PerformanceTrace(_route_dimensions(job))
    job[TRACE_JOB_KEY] = trace
    return trace


def advisory_trace_for_job(job: dict[str, object]) -> PerformanceTrace | _NoopTrace:
    try:
        return trace_for_job(job)
    except Exception:  # noqa: BLE001 - telemetry must never affect authority
        return _NoopTrace()


def safe_record(trace: object, name: str, duration_ms: object) -> None:
    try:
        record = trace.record  # type: ignore[attr-defined]
        record(name, duration_ms)
    except Exception:  # noqa: BLE001 - telemetry must never affect authority
        return


def safe_record_job_span(job: dict[str, object], name: str, duration_ms: object) -> None:
    safe_record(advisory_trace_for_job(job), name, duration_ms)


def attribution_snapshot(job: Mapping[str, object]) -> dict[str, object] | None:
    trace = job.get(TRACE_JOB_KEY)
    return trace.snapshot() if isinstance(trace, PerformanceTrace) else None


def safe_attribution_snapshot(job: Mapping[str, object]) -> dict[str, object] | None:
    try:
        return attribution_snapshot(job)
    except Exception:  # noqa: BLE001 - telemetry must never affect authority
        return None


def normalize_attribution(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, Mapping):
        return None
    provider = dimensions.get("provider")
    domain = _safe_hostname(dimensions.get("domain"))
    index = dimensions.get("worker_application_index")
    worker_id = dimensions.get("worker_id")
    if (
        not isinstance(provider, str)
        or provider not in _PROVIDERS
        or domain is None
        or isinstance(index, bool)
        or not isinstance(index, int)
        or index <= 0
        or isinstance(worker_id, bool)
        or not isinstance(worker_id, int)
        or worker_id < 0
    ):
        return None
    normalized_dimensions: dict[str, int | str] = {
        "provider": provider,
        "domain": domain,
        "worker_application_index": index,
        "worker_id": worker_id,
    }
    raw_spans = value.get("spans")
    if not isinstance(raw_spans, list) or not raw_spans or len(raw_spans) > MAX_SPANS:
        return None
    normalized_spans: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in raw_spans:
        if not isinstance(raw, Mapping):
            return None
        name = raw.get("name")
        duration = raw.get("duration_ms")
        if (
            not isinstance(name, str)
            or name not in SPAN_GROUPS
            or name in seen
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) < 0
        ):
            return None
        seen.add(name)
        normalized_spans.append(
            {
                "name": name,
                "group": SPAN_GROUPS[name],
                "duration_ms": round(min(float(duration), MAX_DURATION_MS), 3),
                "dimensions": dict(normalized_dimensions),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "measurement_model": "nested_advisory_spans",
        "dimensions": normalized_dimensions,
        "spans": sorted(normalized_spans, key=lambda item: str(item["name"])),
    }


def safe_normalize_attribution(value: object) -> dict[str, object] | None:
    try:
        return normalize_attribution(value)
    except Exception:  # noqa: BLE001 - telemetry must never affect terminal state
        return None


def summarize_amplification(samples: Iterable[object]) -> dict[str, object]:
    """Report observed groups without inventing zero-duration observations."""

    primary_turn_ms = 0.0
    sample_count = 0
    observed: dict[str, dict[str, float | int]] = {}
    for sample in samples:
        normalized = safe_normalize_attribution(sample)
        if normalized is None:
            continue
        sample_count += 1
        for span in normalized["spans"]:  # type: ignore[index]
            name = str(span["name"])
            duration = float(span["duration_ms"])
            if name in _PRIMARY_TURN_SPANS:
                primary_turn_ms += duration
            for group, root_spans in _GROUP_ROOT_SPANS.items():
                if name in root_spans:
                    aggregate = observed.setdefault(
                        group, {"duration_ms": 0.0, "observed_span_count": 0}
                    )
                    aggregate["duration_ms"] = float(aggregate["duration_ms"]) + duration
                    aggregate["observed_span_count"] = int(aggregate["observed_span_count"]) + 1
    return {
        "measurement_model": "nested_advisory_spans",
        "sample_count": sample_count,
        "primary_turn_ms": round(primary_turn_ms, 3) if primary_turn_ms else None,
        "groups": {
            group: {
                "duration_ms": round(float(values["duration_ms"]), 3),
                "observed_span_count": int(values["observed_span_count"]),
                "ratio_to_primary_turn": (
                    None
                    if primary_turn_ms == 0
                    else round(float(values["duration_ms"]) / primary_turn_ms, 6)
                ),
            }
            for group, values in sorted(observed.items())
        },
    }
