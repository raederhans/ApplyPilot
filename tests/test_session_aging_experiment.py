from __future__ import annotations

import json
from pathlib import Path

import pytest

from applypilot.apply.session_aging_experiment import (
    MODES,
    compile_report,
    experiment_definition,
    load_fixture,
    run_session_aging_experiment,
)

FIXTURE = Path(__file__).parent / "fixtures" / "benchmarks" / "applypilot_session_aging_v1.json"


def _samples() -> list[dict[str, object]]:
    return [
        {
            "mode": mode,
            "dimensions": {
                "provider": "workday" if index == 1 else "smartrecruiters",
                "domain": "tenant.myworkdayjobs.com" if index == 1 else "jobs.smartrecruiters.com",
                "application_index": index,
                "task_id": "wd-aging-01" if index == 1 else "sr-aging-01",
            },
            "browser_instance": index if mode == "fresh_process" else 1,
            "context_instance": index if mode == "fresh_context" else 1,
            "observation_ms": 1.5,
            "end_to_end_ms": 3.0,
            "lifecycle_spans_ms": {"context_create_ms": 1.0, "page_create_ms": 0.5},
            "execution_position": (MODES.index(mode) - (index - 1)) % len(MODES) + 1,
            "submit_attempts": 0,
        }
        for mode in MODES
        for index in (1, 2)
    ]


def test_fixture_and_a_b_c_definitions_are_fixed() -> None:
    fixture, digest = load_fixture(FIXTURE)

    assert len(digest) == 64
    assert [task["provider"] for task in fixture["tasks"]] == ["workday", "smartrecruiters"] * 3
    definitions = experiment_definition()
    assert definitions["shared_context"]["label"] == "A"
    assert definitions["fresh_context"]["label"] == "B"
    assert definitions["fresh_process"]["label"] == "C"


def test_report_keeps_missing_agent_mcp_and_recovery_attribution_unavailable() -> None:
    report = compile_report(fixture_sha256="a" * 64, samples=_samples())

    assert report["diagnostic_only"] is True
    assert report["production_authority"] is False
    assert report["submission_authority"] is False
    coverage = report["attribution_coverage"]
    assert coverage["coverage_ratio"] == 0.25
    assert coverage["attribution_complete"] is False
    assert coverage["groups"]["agent"]["status"] == "unavailable"
    assert coverage["groups"]["mcp"]["status"] == "unavailable"
    assert coverage["groups"]["observation"]["status"] == "observed"
    assert coverage["groups"]["recovery"]["status"] == "unavailable"
    assert all(report["modes"][mode]["sample_count"] == 2 for mode in MODES)


def test_report_rejects_provider_domain_drift() -> None:
    samples = _samples()
    samples[0]["dimensions"]["domain"] = "example.invalid"  # type: ignore[index]

    with pytest.raises(ValueError, match="provider/domain/application index"):
        compile_report(fixture_sha256="a" * 64, samples=samples)


def test_report_rejects_submit_or_lifetime_drift() -> None:
    samples = _samples()
    samples[0]["submit_attempts"] = 1

    with pytest.raises(ValueError, match="zero submit attempts"):
        compile_report(fixture_sha256="a" * 64, samples=samples)

    samples = _samples()
    samples[1]["context_instance"] = 2
    with pytest.raises(ValueError, match="shared_context"):
        compile_report(fixture_sha256="a" * 64, samples=samples)

    samples = _samples()
    samples[0]["end_to_end_ms"] = 2.0
    with pytest.raises(ValueError, match="lifecycle and observation"):
        compile_report(fixture_sha256="a" * 64, samples=samples)


def test_report_rejects_incomplete_or_cross_mode_cohort_matrix() -> None:
    samples = _samples()
    incomplete = [sample for sample in samples if sample["mode"] != "fresh_process"]
    with pytest.raises(ValueError, match="complete cohort"):
        compile_report(fixture_sha256="a" * 64, samples=incomplete)

    samples = _samples()
    samples[-1]["dimensions"]["provider"] = "workday"  # type: ignore[index]
    samples[-1]["dimensions"]["domain"] = "tenant.myworkdayjobs.com"  # type: ignore[index]
    with pytest.raises(ValueError, match="cohort matrices"):
        compile_report(fixture_sha256="a" * 64, samples=samples)


def test_injected_clock_includes_lifecycle_cost_in_end_to_end_span() -> None:
    from applypilot.apply.session_aging_experiment import end_to_end_attribution

    report = end_to_end_attribution(
        started_at=10.0,
        lifecycle_spans_ms={"browser_process_create_ms": 40.0, "context_create_ms": 30.0},
        observation_ms=20.0,
        clock=lambda: 10.1,
    )

    assert report["end_to_end_ms"] == 100.0
    assert report["lifecycle_spans_ms"]["browser_process_create_ms"] == 40.0
    assert report["observation_ms"] == 20.0


@pytest.mark.browser
def test_isolated_chromium_canary_distinguishes_a_b_and_c_lifetimes(tmp_path: Path) -> None:
    report = run_session_aging_experiment(
        FIXTURE,
        workspace_root=tmp_path / "session-aging",
        source_root=Path(__file__).parents[1],
    )

    assert report["production_authority"] is False
    assert report["submission_authority"] is False
    shared = report["modes"]["shared_context"]["samples"]
    fresh_context = report["modes"]["fresh_context"]["samples"]
    fresh_process = report["modes"]["fresh_process"]["samples"]
    assert {(item["browser_instance"], item["context_instance"]) for item in shared} == {(1, 1)}
    assert {(item["browser_instance"], item["context_instance"]) for item in fresh_context} == {
        (1, index) for index in range(1, 7)
    }
    assert {(item["browser_instance"], item["context_instance"]) for item in fresh_process} == {
        (index, 1) for index in range(1, 7)
    }
    assert [entry["mode_order"] for entry in report["execution_schedule"]] == [
        ["shared_context", "fresh_context", "fresh_process"],
        ["fresh_context", "fresh_process", "shared_context"],
        ["fresh_process", "shared_context", "fresh_context"],
        ["shared_context", "fresh_context", "fresh_process"],
        ["fresh_context", "fresh_process", "shared_context"],
        ["fresh_process", "shared_context", "fresh_context"],
    ]
    assert all(item["submit_attempts"] == 0 for mode in report["modes"].values() for item in mode["samples"])
    workspace = report["workspace"]
    assert Path(workspace["sqlite"]).is_file()
    assert Path(workspace["logs"]).is_file()
    assert json.loads(Path(workspace["ports"]).read_text(encoding="utf-8"))["debugging_port"] == "not_used"
