"""Run one attended ApplyPilot worker against a live in-app-browser host."""

from __future__ import annotations

import argparse
from pathlib import Path

from applypilot.apply.browser_worker import DEFAULT_TIMEOUT_SECONDS, run_browser_worker
from applypilot.apply.visual_bridge import VisualBridgeError


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a supervised discovery/prepare/authorized-submit goal on one attached IAB tab."
    )
    parser.add_argument("--bridge-dir", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--phase", choices=("discovery", "prepare", "submit"), required=True)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--model")
    parser.add_argument("--codex-executable", type=Path)
    args = parser.parse_args()
    try:
        exit_code = run_browser_worker(
            bridge_dir=args.bridge_dir,
            task_file=args.task_file,
            phase=args.phase,
            timeout_seconds=args.timeout_seconds,
            model=args.model,
            codex_executable=args.codex_executable,
        )
    except (ValueError, FileNotFoundError, VisualBridgeError) as exc:
        parser.error(str(exc))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
