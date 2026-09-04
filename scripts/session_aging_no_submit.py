"""Run the local A/B/C session-aging diagnostic without Submit authority."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from applypilot.apply.session_aging_experiment import run_session_aging_experiment


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local intercepted A/B/C Chromium session-aging diagnostics.")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    report = run_session_aging_experiment(
        args.fixture,
        workspace_root=args.workspace_root,
        source_root=args.source_root,
    )
    print(
        json.dumps(
            {
                "report": str(args.workspace_root.expanduser().resolve() / "report.json"),
                "production_authority": report["production_authority"],
                "submission_authority": report["submission_authority"],
                "coverage_ratio": report["attribution_coverage"]["coverage_ratio"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
