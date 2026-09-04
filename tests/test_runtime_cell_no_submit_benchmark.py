from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.apply.runtime_cell_no_submit_benchmark import (
    run_runtime_cell_no_submit_benchmark,
)


def test_runtime_cell_benchmark_is_no_submit_auditable_and_immutable(tmp_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1]
    fixture = source_root / "tests/fixtures/benchmarks/runtime_cell_no_submit_v1.json"
    output = tmp_path / "report.json"
    report = run_runtime_cell_no_submit_benchmark(
        fixture,
        output_path=output,
        source_root=source_root,
        measured_blocks=2,
        warmup_blocks=0,
    )
    assert report["schema_version"] == "applypilot-runtime-cell-no-submit/v1"
    assert report["admission"]["status"] in {"ADMITTED", "NOT_ADMITTED"}
    assert report["admission"]["production_authority"] is False
    assert report["admission"]["effective_production_cells"] == 1
    assert report["admission"]["canary_enabled"] is False
    for block in report["paired_blocks"]:
        assert block["two_cells"]["same_domain_peak"] == 1
        assert block["two_cells"]["overall_concurrency_peak"] == 2
        for lane in ("one_cell", "two_cells"):
            for field in (
                "duplicate_submit_attempts",
                "submit_attempts",
                "effect_attempts",
                "submission_gate_attempts",
                "reservation_attempts",
                "receipt_attempts",
                "cell_cross_writes",
            ):
                assert block[lane][field] == 0
    assert len(report["report_sha256"]) == 64
    with pytest.raises(FileExistsError):
        run_runtime_cell_no_submit_benchmark(
            fixture,
            output_path=output,
            source_root=source_root,
            measured_blocks=2,
            warmup_blocks=0,
        )
