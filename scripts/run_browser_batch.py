import argparse
import os
import sys
from pathlib import Path

from applypilot.apply.browser_batch import BrowserBatch, BrowserBatchError, claim_worker_bridges


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a batch of browser jobs.")
    parser.add_argument("--manifest", type=Path, required=True, help="Path to JSON manifest")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory for logs and status.json")
    parser.add_argument("--min-ram-mb", type=float, default=1024.0, help="Minimum RAM reserve in MiB")

    args = parser.parse_args()

    source_root = str(Path(__file__).resolve().parents[2])
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = source_root if not existing_pythonpath else os.pathsep.join((source_root, existing_pythonpath))

    try:
        batch = BrowserBatch(
            manifest_path=args.manifest,
            output_dir=args.output_dir,
            min_ram_mb=args.min_ram_mb
        )

        bridge_dirs = [j.bridge_dir for j in batch.jobs]
        with claim_worker_bridges(bridge_dirs) as leases:
            batch.set_leases(leases)
            success = batch.run()

        if not success:
            sys.exit(1)

    except BrowserBatchError as e:
        sys.exit(f"Batch error: {e}")
    except KeyboardInterrupt:
        sys.exit(1)

if __name__ == "__main__":
    main()
