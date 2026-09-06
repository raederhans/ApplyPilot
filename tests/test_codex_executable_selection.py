import pytest

from applypilot.apply import agent_runtime


@pytest.mark.parametrize("npm_version,expected", [((0, 149, 0), "app"), ((0, 154, 0), "npm")])
def test_selects_newer_verified_install(monkeypatch, tmp_path, npm_version, expected):
    app = tmp_path / "OpenAI/Codex/bin/release/codex.exe"
    app.parent.mkdir(parents=True)
    app.touch()
    npm = tmp_path / "npm/codex.exe"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("APPLYPILOT_CODEX_EXECUTABLE", raising=False)
    monkeypatch.setattr(agent_runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(agent_runtime.shutil, "which", lambda name: str(npm) if name == "codex.exe" else None)
    monkeypatch.setattr(agent_runtime, "_codex_executable_version", lambda p: (0, 153, 4) if p == app else npm_version)
    assert agent_runtime.resolve_codex_command() == [str(app if expected == "app" else npm)]


def test_explicit_missing_executable_is_not_silently_replaced(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_CODEX_EXECUTABLE", str(tmp_path / "missing.exe"))
    with pytest.raises(FileNotFoundError, match="Configured Codex"):
        agent_runtime.resolve_codex_command()
