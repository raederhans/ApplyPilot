"""Run the local replay/synthetic/dry-run cohort benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from applypilot.apply.dry_run_benchmark import run_dry_cohort_benchmark


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run matched 1/2/4 local synthetic dry-run cohorts. "
            "This is not a real provider benchmark."
        )
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--db-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_dry_cohort_benchmark(
        args.fixture,
        db_root=args.db_root,
        data_root=args.data_root,
        output_path=args.output,
    )
    print(json.dumps({
        "status": "passed" if report["passed"] else "failed",
        "label": report["label"],
        "fixture_sha256": report["fixture_sha256"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
