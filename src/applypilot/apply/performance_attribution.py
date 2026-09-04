"""Bounded, advisory timing spans for application-path attribution.

The collector is deliberately separate from browser authority and durable
workflow decisions.  It only records a small, named set of elapsed durations
on the in-memory job envelope; callers may attach its normalized snapshot to
terminal evidence after their existing authorization path has completed.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

SCHEMA_VERSION = "applypilot-performance-attribution/v1"
TRACE_JOB_KEY = "_performance_attribution_trace"
MAX_DURATION_MS = 86_400_000.0
MAX_SPANS = 32

SPAN_GROUPS = {
    "agent.turn": "agent",
    "agent.startup": "agent",
    "mcp.startup": "mcp",
    "model.first_output": "agent",
    "model.first_tool_decision": "agent",
    "browser.prepare": "browser",
    "audit.pre_submit": "observation",
    "submit.agent": "submission",
    "receipt.reconciliation": "observation",
    "recovery.agent": "recovery",
}
_PRIMARY_TURN_SPANS = frozenset({"browser.prepare", "submit.agent"})


def _bounded_text(value: object, *, maximum: int = 120) -> str:
    return str(value or "").strip()[:maximum]


def _provider(job: Mapping[str, object]) -> str:
    for key in ("provider", "source_site", "site"):
        value = _bounded_text(job.get(key))
        if value:
            return value.casefold()
    return "unavailable"


def _domain(job: Mapping[str, object]) -> str:
    for key in ("application_url", "url"):
        raw = _bounded_text(job.get(key), maximum=2_000)
        if raw:
            hostname = (urlparse(raw).hostname or "").casefold()
            if hostname:
                return hostname[:253]
    return "unavailable"


def _application_index(job: Mapping[str, object]) -> int | str:
    value = job.get("_performance_application_index")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return "unavailable"


def dimensions_for_job(job: Mapping[str, object]) -> dict[str, int | str]:
    """Produce the required bounded attribution dimensions without PII."""

    return {
        "provider": _provider(job),
        "domain": _domain(job),
        "application_index": _application_index(job),
    }


@dataclass(slots=True)
class PerformanceTrace:
    """Aggregate named timings for one application without changing its flow."""

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

    def record_elapsed(
        self,
        name: str,
        started_at: float,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.record(name, (clock() - started_at) * 1000)

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


def trace_for_job(job: dict[str, object]) -> PerformanceTrace:
    """Return the one advisory collector shared by shallow job copies."""

    existing = job.get(TRACE_JOB_KEY)
    if isinstance(existing, PerformanceTrace):
        return existing
    trace = PerformanceTrace(dimensions_for_job(job))
    job[TRACE_JOB_KEY] = trace
    return trace


def record_job_span(job: dict[str, object], name: str, duration_ms: object) -> None:
    """Record one safe span; validation errors are deliberately local to telemetry."""

    trace_for_job(job).record(name, duration_ms)


def attribution_snapshot(job: Mapping[str, object]) -> dict[str, object] | None:
    trace = job.get(TRACE_JOB_KEY)
    return trace.snapshot() if isinstance(trace, PerformanceTrace) else None


def normalize_attribution(value: object) -> dict[str, object] | None:
    """Fail closed when durable evidence is not this collector's bounded schema."""

    if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, Mapping):
        return None
    provider = _bounded_text(dimensions.get("provider"))
    domain = _bounded_text(dimensions.get("domain"), maximum=253)
    application_index = dimensions.get("application_index")
    if not provider or not domain or (
        application_index != "unavailable"
        and (isinstance(application_index, bool) or not isinstance(application_index, int) or application_index <= 0)
    ):
        return None
    normalized_dimensions: dict[str, int | str] = {
        "provider": provider,
        "domain": domain,
        "application_index": application_index,
    }
    raw_spans = value.get("spans")
    if not isinstance(raw_spans, list) or len(raw_spans) > MAX_SPANS:
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


def summarize_amplification(samples: Iterable[object]) -> dict[str, object]:
    """Summarize nested Agent/MCP/observation/recovery work without double counting.

    Ratios use only normal browser/submit turns as their denominator.  The
    individual groups are intentionally not added together because startup and
    first-output spans sit inside an Agent turn.
    """

    totals = {group: 0.0 for group in sorted(set(SPAN_GROUPS.values()))}
    primary_turn_ms = 0.0
    sample_count = 0
    for sample in samples:
        normalized = normalize_attribution(sample)
        if normalized is None:
            continue
        sample_count += 1
        for span in normalized["spans"]:  # type: ignore[index]
            name = str(span["name"])
            duration = float(span["duration_ms"])
            totals[SPAN_GROUPS[name]] += duration
            if name in _PRIMARY_TURN_SPANS:
                primary_turn_ms += duration
    return {
        "measurement_model": "nested_advisory_spans",
        "sample_count": sample_count,
        "primary_turn_ms": round(primary_turn_ms, 3),
        "groups": {
            group: {
                "duration_ms": round(duration, 3),
                "ratio_to_primary_turn": (
                    None if primary_turn_ms == 0 else round(duration / primary_turn_ms, 6)
                ),
            }
            for group, duration in sorted(totals.items())
        },
    }
