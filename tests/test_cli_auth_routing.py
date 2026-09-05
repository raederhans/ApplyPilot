"""Operational CLI contracts for session ownership and score-led routing."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from applypilot import cli, config, database, resume_library
from applypilot.apply import chrome


def test_browser_session_survives_edge_launch_process_handoff(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_BROWSER_PROFILE_MODE", "persistent")
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(chrome, "resolve_browser_backend", lambda *_a, **_k: "edge")
    monkeypatch.setattr(chrome, "get_browser_executable", lambda _backend: "edge.exe")
    monkeypatch.setattr(chrome, "allocate_cdp_port", lambda _worker: 12345)
    process = SimpleNamespace(poll=lambda: 0)
    monkeypatch.setattr(chrome, "launch_chrome", lambda *_a, **_k: process)
    states = iter([True, True, False])
    monkeypatch.setattr(chrome, "cdp_endpoint_reachable", lambda _port: next(states))
    sleeps = []
    cleanup = []
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    monkeypatch.setattr(chrome, "cleanup_worker", lambda w, p: cleanup.append((w, p)))
    monkeypatch.setattr(chrome, "release_cdp_port", lambda w: cleanup.append(w))

    result = CliRunner().invoke(
        cli.app, ["browser-session", "--url", "https://employer.example/careers"]
    )

    assert result.exit_code == 0, result.output
    assert sleeps == [0.5, 0.5]
    assert cleanup == [(0, process), 0]


@pytest.mark.parametrize("extra_args, expected", [([], 6), (["--min-score", "8"], 8)])
def test_resume_route_uses_workspace_floor_or_explicit_override(monkeypatch, extra_args, expected):
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(
        config, "load_profile", lambda: {"submission_policy": {"minimum_fit_score": 6}}
    )
    conn = SimpleNamespace(
        execute=lambda *_a: SimpleNamespace(fetchall=lambda: [{"url": "https://example/job"}])
    )
    monkeypatch.setattr(database, "get_connection", lambda: conn)
    monkeypatch.setattr(resume_library, "sync_resume_library", lambda *_a: None)
    calls = []

    def route(_conn, _job, _profile, **kwargs):
        calls.append(kwargs["minimum_fit_score"])
        return {"decision": "create_variant"}

    monkeypatch.setattr(resume_library, "route_resume_for_job", route)
    result = CliRunner().invoke(
        cli.app, ["resume-route", "--url", "https://example/job", *extra_args]
    )

    assert result.exit_code == 0, result.output
    assert calls == [expected]
