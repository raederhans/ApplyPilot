"""Exercise the attended host JS contracts in existing Python CI tiers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = (
    "scripts/browser-form-state.test.mjs",
    "scripts/visual-bridge-host.test.mjs",
    "scripts/browser-observation-feedback.test.mjs",
)


def _run_node(files: tuple[str, ...], extra_env: dict[str, str] | None = None) -> None:
    node = shutil.which("node")
    assert node, "Node.js 18+ is required for the attended host contract tests"
    result = subprocess.run(
        [node, "--test", *files],
        cwd=ROOT,
        env={**os.environ, **(extra_env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_attended_host_node_contracts() -> None:
    _run_node(CONTRACTS)


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows filesystem/process tier")
def test_attended_host_node_contracts_windows() -> None:
    _run_node(CONTRACTS)


@pytest.mark.browser
def test_attended_feedback_chromium() -> None:
    import playwright

    # Reuse the JS driver already installed with Python Playwright. No npm install.
    driver = Path(playwright.__file__).resolve().parent / "driver" / "package" / "index.mjs"
    assert driver.is_file(), "Installed Playwright JS driver is missing"
    _run_node(
        ("scripts/browser-observation-feedback.chromium.test.mjs",),
        {"APPLYPILOT_TEST_PLAYWRIGHT_MODULE": str(driver)},
    )
