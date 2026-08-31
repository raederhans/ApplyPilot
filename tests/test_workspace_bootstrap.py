from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_cli_workspace_option_binds_paths_before_config_import(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "submission_policy": {
                    "authorization_granted": True,
                    "standing_auto_authorize_ready_jobs": True,
                    "batch_authorization_required": False,
                }
            }
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("APPLYPILOT_DIR", None)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["COLUMNS"] = "300"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "applypilot.cli",
            "--workspace",
            str(tmp_path),
            "doctor",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Active workspace" in result.stdout
    assert tmp_path.name in result.stdout
    assert str(Path.home() / ".applypilot") not in result.stdout
