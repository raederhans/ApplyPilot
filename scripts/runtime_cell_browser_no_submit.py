"""Run the current-source Runtime Cell A/B/C local browser diagnostic."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from applypilot.apply.runtime_cell_browser_diagnostic import (
    run_runtime_cell_browser_diagnostic,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local no-submit Runtime Cell A/B/C diagnostics.")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    report = run_runtime_cell_browser_diagnostic(
        args.fixture,
        workspace_root=args.workspace_root,
        source_root=args.source_root,
    )
    print(
        json.dumps(
            {
                "report": str(args.workspace_root.expanduser().resolve() / "report.json"),
                "source_identity": report["source_identity"],
                "production_effective_cells": report["production_effective_cells"],
                "context_cleanup_verified": report["context_cleanup_verified"],
                "safety_counters": report["safety_counters"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
