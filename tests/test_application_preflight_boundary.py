from __future__ import annotations

from applypilot.apply import launcher


def test_launcher_preflight_wrapper_resolves_patched_dependencies_at_call_time(
    monkeypatch,
) -> None:
    patched_ats = object()
    captured: dict[str, object] = {}

    def run_preflight(host, job):
        captured["host"] = host
        captured["job"] = job
        return {"provider": "delegated"}

    monkeypatch.setattr(launcher, "ats_mod", patched_ats)
    monkeypatch.setattr(
        launcher.application_preflight_mod,
        "run_read_only_preflight",
        run_preflight,
    )
    job = {"url": "https://example.test/jobs/1"}

    result = launcher._run_read_only_preflight(job)

    assert result == {"provider": "delegated"}
    assert captured["job"] is job
    assert captured["host"].ats_mod is patched_ats
    assert captured["host"].get_connection is launcher.get_connection
