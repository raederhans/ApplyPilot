from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.apply.runtime_cell_scheduler_microbenchmark import (
    run_runtime_cell_scheduler_microbenchmark,
)


def test_scheduler_microbenchmark_is_non_admission_auditable_and_immutable(
    tmp_path: Path,
) -> None:
    source_root = Path(__file__).resolve().parents[1]
    fixture = source_root / "tests/fixtures/benchmarks/runtime_cell_scheduler_microfixture_v1.json"
    output = tmp_path / "report.json"
    report = run_runtime_cell_scheduler_microbenchmark(
        fixture,
        output_path=output,
        source_root=source_root,
        measured_blocks=2,
        warmup_blocks=0,
    )
    assert report["schema_version"] == ("applypilot-runtime-cell-scheduler-microbenchmark/v1")
    assert report["diagnostic"]["status"] in {"QUALIFIED", "NOT_QUALIFIED"}
    assert report["diagnostic"]["admission_evidence"] is False
    for block in report["paired_blocks"]:
        assert block["two_cells"]["same_domain_peak"] == 1
        assert block["two_cells"]["overall_concurrency_peak"] == 2
        for lane in ("one_cell", "two_cells"):
            assert block[lane]["cell_cross_writes"] == 0
    assert set(report["unavailable_observations"].values()) == {
        "unavailable_not_exercised",
        "unavailable_not_instrumented",
    }
    assert len(report["report_sha256"]) == 64
    with pytest.raises(FileExistsError):
        run_runtime_cell_scheduler_microbenchmark(
            fixture,
            output_path=output,
            source_root=source_root,
            measured_blocks=2,
            warmup_blocks=0,
        )
