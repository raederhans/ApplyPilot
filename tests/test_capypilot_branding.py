import tomllib
from pathlib import Path

from typer.testing import CliRunner

from applypilot import cli

ROOT = Path(__file__).resolve().parents[1]


def test_capypilot_brand_keeps_package_cli_and_environment_compatibility() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dashboard = (ROOT / "src/applypilot/frontend/dashboard.html").read_text(
        encoding="utf-8"
    )

    assert result.exit_code == 0
    assert "CapyPilot" in result.stdout
    assert cli.app.info.name == "applypilot"
    assert project["project"]["name"] == "applypilot-local"
    assert project["project"]["scripts"]["applypilot"] == "applypilot.cli:app"
    assert "APPLYPILOT_DIR" in (ROOT / "src/applypilot/config.py").read_text(
        encoding="utf-8"
    )
    assert "<title>CapyPilot" in dashboard
    assert "Happy Pilot" not in dashboard
