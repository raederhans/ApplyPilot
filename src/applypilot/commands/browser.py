"""Attended browser entry points, separate from the unattended apply launcher."""
import sqlite3
from pathlib import Path


def run_browser_work(runtime, *, bridge_dir, task_file, phase, timeout_seconds):
    from applypilot.apply.browser_worker import run_browser_worker
    from applypilot.apply.visual_bridge import VisualBridgeError

    try:
        code = run_browser_worker(bridge_dir=bridge_dir, task_file=task_file,
                                  phase=phase, timeout_seconds=timeout_seconds)
    except (ValueError, OSError, VisualBridgeError) as exc:
        runtime.console.print(str(exc), markup=False)
        raise runtime.typer.Exit(code=2) from exc
    raise runtime.typer.Exit(code=code)


def run_browser_batch(runtime, *, manifest, output_dir, min_ram_mb):
    from applypilot.apply.browser_batch import BrowserBatch, BrowserBatchError, claim_worker_bridges

    try:
        batch = BrowserBatch(manifest, output_dir, min_ram_mb=min_ram_mb)
        with claim_worker_bridges([job.bridge_dir for job in batch.jobs]) as leases:
            batch.set_leases(leases)
            batch.run()
    except KeyboardInterrupt as exc:
        raise runtime.typer.Exit(code=130) from exc
    except (BrowserBatchError, ValueError, OSError) as exc:
        runtime.console.print(str(exc), markup=False)
        raise runtime.typer.Exit(code=2) from exc
    runtime._print_json(data={"status_file": str(batch.status_path),
                             "submission_authority": False})
    raise runtime.typer.Exit(code=0 if all(s.status == "completed" for s in batch.states.values()) else 1)


def run_attended_plan(runtime, *, db: Path, attempt_id: str):
    from applypilot.apply.attended_plan import build_attended_plan

    try:
        # Read-only URI prevents this status command from creating/upgrading a DB.
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            result = build_attended_plan(connection, attempt_id)
    except (ValueError, OSError, sqlite3.Error) as exc:
        runtime.console.print(str(exc), markup=False)
        raise runtime.typer.Exit(code=2) from exc
    runtime._print_json(data=result)
