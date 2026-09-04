"""Run the isolated Runtime Cell SQLite scheduler microbenchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from applypilot.apply.runtime_cell_scheduler_microbenchmark import (
    run_runtime_cell_scheduler_microbenchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Microbenchmark the 1-Cell versus 2-Cell SQLite scheduler")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--measured-blocks", type=int, default=10)
    parser.add_argument("--warmup-blocks", type=int, default=2)
    args = parser.parse_args()
    report = run_runtime_cell_scheduler_microbenchmark(
        args.fixture,
        output_path=args.output,
        source_root=args.source_root,
        measured_blocks=args.measured_blocks,
        warmup_blocks=args.warmup_blocks,
    )
    print(
        json.dumps(
            {
                "report": str(args.output.expanduser().resolve()),
                "status": report["diagnostic"]["status"],
                "paired_bootstrap_95_lower_bound": report["paired_bootstrap_95_lower_bound"],
                "admission_evidence": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
