from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from applypilot.apply import launcher
from applypilot.apply import performance_attribution as attribution_mod


def _bound_job() -> dict[str, object]:
    job: dict[str, object] = {}
    attribution_mod.bind_attempt_route(
        job,
        provider="workday",
        target_url="https://acme.myworkdayjobs.com/job/123",
        worker_application_index=3,
        worker_id="worker-1",
    )
    return job


def test_no_submit_baseline_reports_only_observed_non_additive_groups() -> None:
    job = _bound_job()
    for name, duration in (
        ("agent.turn", 100),
        ("agent.startup", 15),
        ("mcp.first_tool_request", 20),
        ("model.first_text_output", 30),
        ("model.first_tool_decision", 45),
        ("browser.prepare", 100),
        ("audit.pre_submit", 10),
        ("recovery.agent", 25),
    ):
        attribution_mod.safe_record_job_span(job, name, duration)

    snapshot = attribution_mod.safe_attribution_snapshot(job)

    assert snapshot is not None
    assert snapshot["schema_version"] == attribution_mod.SCHEMA_VERSION
    assert snapshot["dimensions"] == {
        "provider": "workday",
        "domain": "acme.myworkdayjobs.com",
        "worker_application_index": 3,
        "worker_id": "worker-1",
    }
    summary = attribution_mod.summarize_amplification([snapshot])
    assert summary["primary_turn_ms"] == 100.0
    assert summary["groups"] == {
        "agent": {
            "duration_ms": 100.0,
            "observed_span_count": 1,
            "ratio_to_primary_turn": 1.0,
        },
        "browser": {
            "duration_ms": 100.0,
            "observed_span_count": 1,
            "ratio_to_primary_turn": 1.0,
        },
        "mcp": {
            "duration_ms": 20.0,
            "observed_span_count": 1,
            "ratio_to_primary_turn": 0.2,
        },
        "observation": {
            "duration_ms": 10.0,
            "observed_span_count": 1,
            "ratio_to_primary_turn": 0.1,
        },
        "recovery": {
            "duration_ms": 25.0,
            "observed_span_count": 1,
            "ratio_to_primary_turn": 0.25,
        },
    }
    assert "submission" not in summary["groups"]
    assert all(span["dimensions"] == snapshot["dimensions"] for span in snapshot["spans"])


@pytest.mark.parametrize(
    "bad_domain",
    ["user@example.test", "example.test/path", "example.test?token=x", "example.test\nsecret"],
)
def test_normalizer_rejects_untrusted_provider_or_domain(bad_domain: str) -> None:
    job = _bound_job()
    attribution_mod.safe_record_job_span(job, "agent.turn", 1)
    snapshot = attribution_mod.safe_attribution_snapshot(job)
    assert snapshot is not None
    bad = dict(snapshot)
    bad["dimensions"] = {**snapshot["dimensions"], "domain": bad_domain}
    assert attribution_mod.normalize_attribution(bad) is None
    bad["dimensions"] = {**snapshot["dimensions"], "provider": "candidate name"}
    assert attribution_mod.normalize_attribution(bad) is None


def test_advisory_helpers_swallow_trace_and_snapshot_faults(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _bound_job()

    def fail_trace(_job: dict[str, object]):
        raise RuntimeError("fault injected")

    monkeypatch.setattr(attribution_mod, "trace_for_job", fail_trace)
    attribution_mod.safe_record_job_span(job, "agent.turn", 1)

    def fail_snapshot(_job: object):
        raise RuntimeError("fault injected")

    monkeypatch.setattr(attribution_mod, "attribution_snapshot", fail_snapshot)
    assert attribution_mod.safe_attribution_snapshot(job) is None


def test_receipt_span_is_emitted_only_by_actual_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _bound_job()
    monkeypatch.setattr(launcher, "get_connection", lambda: object())
    monkeypatch.setattr(
        launcher.receipt_observer_mod,
        "process_receipt_observation",
        lambda *_args, **_kwargs: {"status": "no_match", "provider": "gmail"},
    )

    assert launcher._process_receipt_observer_result(
        job,
        provider="gmail",
        submitted_at=datetime(2026, 9, 4, tzinfo=UTC),
        observation={"confirmation_receipt": None},
    ) == {"status": "no_match", "provider": "gmail"}

    snapshot = attribution_mod.safe_attribution_snapshot(job)
    assert snapshot is not None
    assert [span["name"] for span in snapshot["spans"]] == ["receipt.reconciliation"]


def test_unbound_or_invalid_attribution_is_unavailable_not_zero_filled() -> None:
    job: dict[str, object] = {}
    attribution_mod.safe_record_job_span(job, "agent.turn", 1)
    snapshot = attribution_mod.safe_attribution_snapshot(job)

    assert snapshot is not None
    assert snapshot["dimensions"] == {
        "provider": "unavailable",
        "domain": "unavailable",
        "worker_application_index": "unavailable",
        "worker_id": "unavailable",
    }
    assert attribution_mod.normalize_attribution(snapshot) is None
    assert attribution_mod.normalize_attribution({"schema_version": attribution_mod.SCHEMA_VERSION, "spans": []}) is None
    with pytest.raises(ValueError, match="unknown performance"):
        attribution_mod.trace_for_job(job).record("submission.authority", 1)
    with pytest.raises(ValueError, match="finite and non-negative"):
        attribution_mod.trace_for_job(job).record("agent.turn", math.nan)


def test_attempt_route_mismatch_is_rejected_even_when_shape_is_valid() -> None:
    job = _bound_job()
    attribution_mod.safe_record_job_span(job, "agent.turn", 1)
    snapshot = attribution_mod.safe_attribution_snapshot(job)
    assert snapshot is not None

    assert attribution_mod.attribution_for_attempt(
        snapshot,
        worker_id="worker-1",
        job_url="https://jane-doe.example.test/apply",
    ) is None
