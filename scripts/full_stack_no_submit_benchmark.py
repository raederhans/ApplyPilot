"""Run the isolated ApplyPilot full-stack no-submit worker cohort."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from applypilot.apply.full_stack_no_submit_benchmark import (
    run_full_stack_no_submit_benchmark,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run matched 1/2/4-worker local Chromium cohorts without Submit authority."
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--measured-blocks", type=int, default=10)
    parser.add_argument("--warmup-blocks", type=int, default=2)
    parser.add_argument("--rss-budget-bytes", type=int, default=6_000_000_000)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = run_full_stack_no_submit_benchmark(
        args.fixture,
        workspace_root=args.workspace_root,
        output_path=args.output,
        source_root=args.source_root,
        measured_blocks=args.measured_blocks,
        warmup_blocks=args.warmup_blocks,
        rss_budget_bytes=args.rss_budget_bytes,
    )
    print(
        json.dumps(
            {
                "report": str(args.output.expanduser().resolve()),
                "local_gate_status": report["admission"]["local_gate_status"],
                "production_status": report["admission"]["status"],
                "eligible_workers": report["admission"]["eligible_workers"],
                "reasons": report["admission"]["reasons"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
