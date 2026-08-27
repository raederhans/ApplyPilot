from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot.cli import app


def test_initialize_from_files_creates_a_secret_free_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import applypilot.wizard.init as onboarding
    from applypilot import config

    workspace = tmp_path / "workspace"
    resume = tmp_path / "resume.txt"
    profile = tmp_path / "profile.json"
    searches = tmp_path / "searches.yaml"
    resume.write_text("Python and SQL", encoding="utf-8")
    profile.write_text(json.dumps({"personal": {"full_name": "Test User"}}), encoding="utf-8")
    searches.write_text("queries:\n  - query: Data Analyst\n", encoding="utf-8")

    monkeypatch.setattr(onboarding, "APP_DIR", workspace)
    monkeypatch.setattr(onboarding, "PROFILE_PATH", workspace / "profile.json")
    monkeypatch.setattr(onboarding, "RESUME_PATH", workspace / "resume.txt")
    monkeypatch.setattr(onboarding, "RESUME_PDF_PATH", workspace / "resume.pdf")
    monkeypatch.setattr(onboarding, "SEARCH_CONFIG_PATH", workspace / "searches.yaml")
    monkeypatch.setattr(config, "APP_DIR", workspace)
    monkeypatch.setattr(
        onboarding,
        "ensure_dirs",
        lambda: workspace.mkdir(parents=True, exist_ok=True),
    )

    onboarding.initialize_from_files(resume=resume, profile=profile, searches=searches)

    assert (workspace / "resume.txt").read_text(encoding="utf-8") == "Python and SQL"
    assert json.loads((workspace / "profile.json").read_text(encoding="utf-8"))["personal"] == {
        "full_name": "Test User"
    }
    assert (workspace / "searches.yaml").is_file()
    assert not (workspace / ".env").exists()

    with pytest.raises(FileExistsError, match="Refusing to replace"):
        onboarding.initialize_from_files(resume=resume, profile=profile, searches=searches)


def test_initialize_from_files_validates_structured_inputs(tmp_path: Path) -> None:
    from applypilot.wizard.init import initialize_from_files

    resume = tmp_path / "resume.txt"
    profile = tmp_path / "profile.json"
    searches = tmp_path / "searches.yaml"
    resume.write_text("Resume", encoding="utf-8")
    profile.write_text("[]", encoding="utf-8")
    searches.write_text("queries: []\n", encoding="utf-8")

    with pytest.raises(TypeError, match="Profile JSON must contain an object"):
        initialize_from_files(resume=resume, profile=profile, searches=searches)


def test_init_requires_the_complete_non_interactive_input_set(tmp_path: Path) -> None:
    resume = tmp_path / "resume.txt"
    resume.write_text("Resume", encoding="utf-8")

    result = CliRunner().invoke(app, ["init", "--resume", str(resume)])

    assert result.exit_code == 2
    assert "Usage: applypilot init" in result.output
