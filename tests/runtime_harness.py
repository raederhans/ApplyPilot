"""Small executable wrapper for the offline runtime replay fixture."""

from __future__ import annotations

import json
from pathlib import Path

from applypilot.apply.runtime_evaluation import (
    EvaluationReport,
    compare_runtimes,
    replay_registry_from_fixture,
)

DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "runtime" / "scenarios.json"


def run_fixture(path: Path = DEFAULT_FIXTURE) -> EvaluationReport:
    registry, scenarios = replay_registry_from_fixture(path)
    return compare_runtimes(scenarios, registry)


def main() -> int:
    report = run_fixture()
    print(
        json.dumps(
            {
                "label": report.label,
                "passed": report.passed,
                "scorecard": report.scorecard(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a script
    raise SystemExit(main())
