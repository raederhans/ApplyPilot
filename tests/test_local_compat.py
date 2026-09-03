from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pandas as pd
import pytest

from applypilot import config, llm
from applypilot.apply import (
    agent_runtime,
    chrome,
    credential_relay,
    credential_relay_mcp,
    launcher,
    page_observation,
    prompt,
    worker_orchestration,
)
from applypilot.database import (
    canonicalize_job_url,
    extract_platform_job_id,
    get_application_fact_revisions,
    get_jobs_by_stage,
    get_stats,
    get_unanswered_questions,
    import_linkedin_applied_export,
    init_db,
    record_application_fact_revision,
    record_submission_observation,
    record_unanswered_questions,
    store_jobs,
)
from applypilot.discovery import jobspy
from applypilot.eligibility import evaluate_job_eligibility, refresh_job_eligibility
from applypilot.enrichment import detail
from applypilot.scoring import cover_letter, scorer, tailor
from applypilot.scoring import pdf as pdf_renderer
from applypilot.scoring.validator import (
    validate_cover_letter,
    validate_json_fields,
    validate_tailored_resume,
)


def _clear_llm_env(monkeypatch) -> None:
    for name in (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "LLM_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_deepseek_provider_detection(monkeypatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")

    assert llm._detect_provider() == (
        "https://api.deepseek.com",
        "deepseek-v4-pro",
        "test-only",
    )
    assert config.has_llm_provider() is True


def test_remote_custom_endpoint_requires_key(monkeypatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_URL", "https://example.invalid/v1")
    assert config.has_llm_provider() is False

    monkeypatch.setenv("LLM_API_KEY", "test-only")
    assert config.has_llm_provider() is True


def test_local_endpoint_does_not_require_key(monkeypatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_URL", "http://127.0.0.1:11434/v1")
    assert config.has_llm_provider() is True


def test_llm_retry_count_is_bounded_by_environment(monkeypatch) -> None:
    client = llm.LLMClient("https://example.invalid", "test-model", "test-key")
    attempts = {"count": 0}

    def always_timeout(*args, **kwargs):
        attempts["count"] += 1
        raise httpx.ReadTimeout("bounded timeout")

    monkeypatch.setenv("APPLYPILOT_LLM_MAX_RETRIES", "1")
    monkeypatch.setattr(client, "_chat_compat", always_timeout)
    with pytest.raises(httpx.ReadTimeout):
        client.chat([{"role": "user", "content": "test"}])

    assert attempts["count"] == 1
    client.close()


def test_edge_is_detected_on_windows(monkeypatch, tmp_path: Path) -> None:
    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.touch()

    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path))
    monkeypatch.setattr(config.platform, "system", lambda: "Windows")

    assert config.get_chrome_path() == str(edge)


def test_windows_claude_resolver_prefers_native_npm_executable(
    monkeypatch, tmp_path: Path
) -> None:
    npm = tmp_path / "npm"
    shim = npm / "claude.cmd"
    native = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    native.parent.mkdir(parents=True)
    shim.write_text("@echo off", encoding="utf-8")
    native.write_bytes(b"test")
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda name: str(shim) if name in {"claude", "claude.cmd"} else None,
    )

    assert launcher._resolve_claude_command() == [str(native)]


def test_windows_claude_resolver_wraps_cmd_shim_when_native_missing(
    monkeypatch, tmp_path: Path
) -> None:
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda name: str(shim) if name in {"claude", "claude.cmd"} else None,
    )
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    assert launcher._resolve_claude_command() == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/s",
        "/c",
        str(shim),
    ]


def test_windows_codex_resolver_prefers_native_npm_executable(monkeypatch, tmp_path: Path) -> None:
    shim = tmp_path / "codex.cmd"
    native = (
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    shim.write_text("@echo off", encoding="utf-8")
    native.write_bytes(b"test")
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda name: str(shim) if name == "codex.cmd" else r"C:\WindowsApps\codex.exe",
    )

    assert launcher._resolve_codex_command() == [str(native)]


def test_codex_agent_command_isolated_to_review_browser_tools(monkeypatch, tmp_path: Path) -> None:
    native = tmp_path / "codex.exe"
    native.write_bytes(b"test")
    monkeypatch.setattr(launcher, "_resolve_codex_command", lambda: [str(native)])

    command, final_path = launcher._build_agent_command(
        backend="codex",
        model="gpt-5.6-sol",
        port=9432,
        worker_dir=tmp_path,
        mcp_config_path=tmp_path / "unused.json",
        credential_relay_authorized=True,
    )
    rendered = " ".join(command)
    overrides = {
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c"
    }

    assert command[:2] == [str(native), "exec"]
    assert "--ignore-user-config" in command
    assert "--ephemeral" in command
    assert "gpt-5.6-sol" in command
    assert "http://localhost:9432" in rendered
    assert "browser_fill_form" in rendered
    assert "browser_file_upload" in rendered
    assert "mcp_servers.credential_relay.command" in rendered
    assert "applypilot.apply.credential_relay_mcp" in rendered
    assert "mcp_servers.credential_relay.env_vars" in rendered
    for name in (
        "APPLYPILOT_ATS_CREDENTIAL_FILE",
        "APPLYPILOT_CDP_PORT",
        "APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS",
        "APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS",
        "APPLYPILOT_CREDENTIAL_ALLOW_KNOWN_ATS_REDIRECT",
        "APPLYPILOT_CREDENTIAL_ROOT_TARGET_IDS",
        "APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED",
    ):
        assert name in rendered
    assert (
        'mcp_servers.applypilot_ats.env_vars=["APPLYPILOT_ATS_CONTEXT_PATH"]'
        in overrides
    )
    assert (
        "mcp_servers.applypilot_control.env_vars="
        '["APPLYPILOT_AGENT_REPORT_PATH", "APPLYPILOT_AGENT_RUN_ID"]'
        in overrides
    )
    ats_env = next(
        value for value in overrides
        if value.startswith("mcp_servers.applypilot_ats.env_vars=")
    )
    control_env = next(
        value for value in overrides
        if value.startswith("mcp_servers.applypilot_control.env_vars=")
    )
    assert "CREDENTIAL" not in ats_env
    assert "CREDENTIAL" not in control_env
    assert "browser_evaluate" not in rendered
    assert "mcp_servers.mailbox.required=false" in rendered
    assert 'mcp_servers.mailbox.enabled_tools=["search_emails", "read_email"]' in rendered
    assert "send_email" not in rendered
    assert "--sandbox read-only" in rendered
    assert "features.shell_tool=false" in rendered
    assert 'web_search="disabled"' in rendered
    assert "gstack-browse" in rendered
    assert 'skills.config=[{path="' in rendered
    assert '"enabled": false' not in rendered
    assert final_path == tmp_path / "codex-final-message.txt"


def test_posix_codex_command_uses_direct_npx_and_omits_unauthorized_relay(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(agent_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(agent_runtime.shutil, "which", lambda name: "/usr/bin/npx" if name == "npx" else None)

    command, _ = agent_runtime.build_agent_command(
        "codex",
        "gpt-5.6-sol",
        9432,
        tmp_path,
        tmp_path / "unused.json",
        resolve_codex=lambda: ["/usr/bin/codex"],
        credential_relay_authorized=False,
    )
    rendered = " ".join(command)

    assert 'mcp_servers.playwright.command="/usr/bin/npx"' in rendered
    assert "cmd.exe" not in rendered
    assert '"/d"' not in rendered
    assert "credential_relay" not in rendered


def test_credential_relay_mcp_rejects_direct_calls_without_profile_authorization(
    monkeypatch,
) -> None:
    monkeypatch.delenv("APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED", raising=False)
    monkeypatch.setattr(
        credential_relay_mcp,
        "_decrypt_password",
        lambda _path: pytest.fail("password must not be decrypted"),
    )

    response = credential_relay_mcp._handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "fill_ats_credentials", "arguments": {"field": "password"}},
    })

    assert response["result"]["isError"] is True
    assert "not authorized" in response["result"]["content"][0]["text"]


def test_credential_relay_registration_respects_profile_and_browser_policy() -> None:
    enabled = {"authentication": {"credential_relay_authorized": True}}
    disabled = {"authentication": {"credential_relay_authorized": False}}
    legacy_enabled = {"authentication": {"ats_account_creation_authorized": True}}
    explicit_override = {
        "authentication": {
            "ats_account_creation_authorized": True,
            "credential_relay_authorized": False,
        }
    }

    assert launcher._credential_relay_allowed(enabled, {"_browser_backend": "edge"}) is True
    assert launcher._credential_relay_allowed(disabled, {"_browser_backend": "edge"}) is False
    assert launcher._credential_relay_allowed(legacy_enabled, {"_browser_backend": "edge"}) is True
    assert launcher._credential_relay_allowed(explicit_override, {"_browser_backend": "edge"}) is False
    assert launcher._credential_relay_allowed(enabled, {"_browser_backend": "cloak"}) is False


def test_fresh_browser_profile_does_not_clone_daily_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPLYPILOT_BROWSER_PROFILE_MODE", "fresh")
    monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path)
    stale_extension = tmp_path / "worker-0" / "Default" / "Extensions" / "stale"
    stale_extension.mkdir(parents=True)
    (stale_extension / "manifest.json").write_text("{}", encoding="utf-8")

    profile = chrome.setup_worker_profile(
        0,
        profile_lock=SimpleNamespace(profile_path=(tmp_path / "worker-0").resolve()),
    )

    assert profile == tmp_path / "worker-0"
    assert profile.is_dir()
    assert not (profile / "Default").exists()


def test_persistent_browser_profile_never_clones_daily_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPLYPILOT_BROWSER_PROFILE_MODE", "persistent")
    monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path / "workers")
    daily_profile = tmp_path / "daily" / "Default"
    daily_profile.mkdir(parents=True)
    (daily_profile / "Cookies").write_text("sensitive", encoding="utf-8")
    monkeypatch.setattr(config, "get_chrome_user_data", lambda: daily_profile.parent)

    profile = chrome.setup_worker_profile(
        0,
        profile_lock=SimpleNamespace(
            profile_path=(tmp_path / "workers" / "worker-0").resolve()
        ),
    )

    assert profile == tmp_path / "workers" / "worker-0"
    assert profile.is_dir()
    assert not (profile / "Default" / "Cookies").exists()


def test_cloak_clone_mode_becomes_an_isolated_persistent_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPLYPILOT_BROWSER_PROFILE_MODE", "clone")
    monkeypatch.setattr(config, "CLOAK_WORKER_DIR", tmp_path / "cloak-workers")
    daily_profile = tmp_path / "daily" / "Default"
    daily_profile.mkdir(parents=True)
    (daily_profile / "Cookies").write_text("sensitive", encoding="utf-8")
    monkeypatch.setattr(config, "get_chrome_user_data", lambda: daily_profile.parent)

    profile = chrome.setup_worker_profile(
        0,
        "cloak",
        profile_lock=SimpleNamespace(
            profile_path=(tmp_path / "cloak-workers" / "worker-0").resolve()
        ),
    )

    assert profile == tmp_path / "cloak-workers" / "worker-0"
    assert profile.is_dir()
    assert not (profile / "Default" / "Cookies").exists()


def test_browser_backend_validation_rejects_auto_for_a_concrete_launch() -> None:
    assert chrome.resolve_browser_backend("auto") == "auto"
    with pytest.raises(ValueError, match="edge or cloak"):
        chrome.resolve_browser_backend("auto", allow_auto=False)


def test_cdp_ports_are_ephemeral_owned_and_unique(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path)
    assert not hasattr(chrome, "_kill_on_port")

    first = chrome.allocate_cdp_port(0)
    second = chrome.allocate_cdp_port(1)
    try:
        assert first != second
        assert (tmp_path / "cdp-port-locks" / f"{first}.lock").is_file()
        assert (tmp_path / "cdp-port-locks" / f"{second}.lock").is_file()
    finally:
        chrome.release_cdp_port(0)
        chrome.release_cdp_port(1)

    assert not (tmp_path / "cdp-port-locks" / f"{first}.lock").exists()
    assert not (tmp_path / "cdp-port-locks" / f"{second}.lock").exists()


def test_worker_releases_cdp_claim_when_profile_loading_fails(monkeypatch) -> None:
    released: list[int] = []
    monkeypatch.setattr(launcher, "allocate_cdp_port", lambda _worker_id: 9432)
    monkeypatch.setattr(launcher, "release_cdp_port", released.append)
    monkeypatch.setattr(
        launcher.config,
        "load_profile",
        lambda: (_ for _ in ()).throw(ValueError("invalid profile")),
    )

    with pytest.raises(ValueError, match="invalid profile"):
        launcher.worker_loop()

    assert released == [0]


def test_worker_runtime_port_validation_reports_missing_contract() -> None:
    with pytest.raises(TypeError, match="missing required ports"):
        worker_orchestration.worker_loop(SimpleNamespace())


def test_dead_cdp_lock_is_removed_only_after_liveness_check(monkeypatch, tmp_path: Path) -> None:
    lock_path = tmp_path / "12345.lock"
    lock_path.write_text("pid=123 worker=0\n", encoding="ascii")
    monkeypatch.setattr(chrome, "_lock_owner_is_running", lambda _path: False)

    chrome._remove_stale_cdp_locks(tmp_path)

    assert not lock_path.exists()


def test_cookie_bridge_scopes_to_bound_https_application_urls() -> None:
    assert chrome._scoped_cookie_urls([
        "https://jobs.lever.co/example/role",
        "https://careers.example.com/apply",
        "http://insecure.example.test/",
        "javascript:alert(1)",
    ]) == [
        "https://jobs.lever.co/example/role",
        "https://careers.example.com/apply",
    ]


def test_cloak_binary_rejects_verification_bypass_override(monkeypatch) -> None:
    monkeypatch.setenv("CLOAKBROWSER_SKIP_CHECKSUM", "1")

    with pytest.raises(RuntimeError, match="rejects binary/checksum overrides"):
        chrome.get_browser_executable("cloak")


def test_cloak_login_policy_disables_authorized_ats_account_creation() -> None:
    steps = prompt._build_login_steps(
        _application_profile(),
        allow_account_creation=False,
        allow_credential_relay=False,
    )

    assert "Do not create a new account" in steps
    assert "account creation with" not in steps
    assert "Credential relay is not authorized" in steps
    assert "mcp__credential_relay__fill_ats_credentials" not in steps


def test_prompt_consumes_launcher_runtime_relay_denial(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _application_profile()
    profile["authentication"].update(
        {
            "ordinary_ats_sign_in_authorized": False,
            "credential_relay_authorized": True,
            "ats_account_creation_authorized": False,
            "google_sso_existing_session_authorized": False,
        }
    )
    resume_txt = tmp_path / "tailored.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_txt.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")
    job = {
        "url": "https://jobs.example.test/apply",
        "application_url": "https://jobs.example.test/apply",
        "title": "Data Analyst Intern",
        "company_name": "Example",
        "source_site": "generic",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
        "_browser_backend": "cloak",
    }

    runtime_relay_authorized = launcher._credential_relay_allowed(profile, job)
    built = prompt.build_prompt(
        job,
        "Verified resume",
        dry_run=True,
        credential_relay_authorized=runtime_relay_authorized,
    )

    assert runtime_relay_authorized is False
    assert "mcp__credential_relay__fill_ats_credentials" not in built
    assert "do not authenticate or create an account" in built
    assert (
        "RESULT:LOGIN_ISSUE -- authentication or account creation is required but is not authorized"
        in built
    )
    assert "one bounded authorized authentication attempt failed" not in built


def test_unknown_browser_profile_mode_is_rejected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPLYPILOT_BROWSER_PROFILE_MODE", "mystery")
    monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path)

    with pytest.raises(ValueError, match="fresh, persistent, or clone"):
        chrome.setup_worker_profile(
            0,
            profile_lock=SimpleNamespace(profile_path=(tmp_path / "worker-0").resolve()),
        )


def test_cdp_readiness_accepts_edge_zero_exit_relaunch(monkeypatch) -> None:
    class Process:
        returncode = 0

        @staticmethod
        def poll():
            return 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(chrome, "urlopen", lambda *_args, **_kwargs: Response())

    chrome._wait_for_cdp_ready(Process(), 9432, timeout_seconds=0.1)


def test_cdp_readiness_rejects_nonzero_browser_exit() -> None:
    class Process:
        returncode = 12

        @staticmethod
        def poll():
            return 12

    with pytest.raises(RuntimeError, match=r"exit=12"):
        chrome._wait_for_cdp_ready(Process(), 9432, timeout_seconds=0.1)


def test_cleanup_closes_edge_child_after_zero_exit_relaunch(monkeypatch) -> None:
    class Process:
        pid = 456

        @staticmethod
        def poll():
            return 0

    closed_ports: list[int] = []
    killed_pids: list[int] = []
    monkeypatch.setattr(chrome, "_close_browser_via_cdp", closed_ports.append)
    monkeypatch.setattr(chrome, "_kill_process_tree", killed_pids.append)
    chrome._chrome_procs[37] = Process()
    chrome._chrome_ports[37] = 9432

    chrome.cleanup_worker(37, chrome._chrome_procs[37])

    assert closed_ports == [9432]
    assert killed_pids == []
    assert 37 not in chrome._chrome_procs
    assert 37 not in chrome._chrome_ports


def test_close_browser_via_cdp_sends_browser_close(monkeypatch) -> None:
    from playwright import sync_api

    sent: list[str] = []
    endpoints: list[str] = []

    class Session:
        def send(self, method):
            sent.append(method)

    class Browser:
        @staticmethod
        def new_browser_cdp_session():
            return Session()

    class Chromium:
        @staticmethod
        def connect_over_cdp(endpoint):
            endpoints.append(endpoint)
            return Browser()

    class Playwright:
        chromium = Chromium()

    class Context:
        def __enter__(self):
            return Playwright()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: Context())

    chrome._close_browser_via_cdp(9432)

    assert endpoints == ["http://127.0.0.1:9432"]
    assert sent == ["Browser.close"]


def test_agent_log_slug_removes_windows_invalid_source_characters() -> None:
    assert launcher._safe_log_slug("official:grab:rss") == "official_grab_rss"
    assert launcher._safe_log_slug(" ") == "unknown"


def _install_fake_profile_lock(monkeypatch, profile: Path) -> None:
    chrome._chrome_procs.clear()
    chrome._chrome_ports.clear()
    chrome._profile_locks.clear()
    chrome._profile_paths.clear()
    chrome._launching_workers.clear()
    chrome._allocating_cdp_workers.clear()
    chrome._releasing_cdp_workers.clear()

    class FakeLock:
        def __init__(self, requested_profile: Path) -> None:
            self.profile_path = Path(requested_profile)
            self.sidecar_path = self.profile_path.parent / ".fake-sidecar"
            self.held = False
            self._spawn_attempted = False

        def acquire(self):
            self.held = True
            return self

        @property
        def owned_by_current_thread(self) -> bool:
            return self.held

        @property
        def has_native_resource(self) -> bool:
            return self.held

        @property
        def spawn_attempted(self) -> bool:
            return self._spawn_attempted

        @property
        def requires_recovery(self) -> bool:
            return False

        def record_browser(self, _pid: int) -> None:
            return None

        def record_spawn_attempt(self) -> None:
            self._spawn_attempted = True

        def actual_browser_stopped(self) -> bool:
            return True

        def release_before_spawn(self) -> None:
            self.held = False

        def release_after_stop(self, **_kwargs) -> None:
            self.held = False

        def mark_recovery_required(self) -> None:
            return None

    monkeypatch.setattr(chrome, "ProfileLock", FakeLock)
    monkeypatch.setattr(chrome, "resolve_worker_profile_path", lambda *_args: profile)
    monkeypatch.setattr(chrome, "_resolve_actual_browser_pid", lambda _port: 999)


def _clear_fake_browser_generation(worker_id: int) -> None:
    chrome._chrome_procs.pop(worker_id, None)
    chrome._chrome_ports.pop(worker_id, None)
    chrome._profile_locks.pop(worker_id, None)
    chrome._profile_paths.pop(worker_id, None)
    chrome._launching_workers.discard(worker_id)


def test_browser_launch_disables_system_extensions(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    class Process:
        pid = 456

        @staticmethod
        def poll():
            return 0

    _install_fake_profile_lock(monkeypatch, tmp_path)
    monkeypatch.setattr(chrome, "setup_worker_profile", lambda *_args: tmp_path)
    monkeypatch.setattr(chrome, "_suppress_restore_nag", lambda _profile: None)
    monkeypatch.setattr(config, "get_chrome_path", lambda: "msedge.exe")
    monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
    monkeypatch.setattr(chrome, "_wait_for_cdp_ready", lambda *_args, **_kwargs: None)

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(chrome.subprocess, "Popen", fake_popen)

    chrome.launch_chrome(worker_id=0, port=9432)

    assert "--disable-extensions" in captured["command"]
    assert "--disable-component-extensions-with-background-pages" in captured["command"]
    assert f"--user-data-dir={tmp_path}" in captured["command"]
    assert "--remote-debugging-address=127.0.0.1" in captured["command"]
    assert "--use-fake-device-for-media-stream" not in captured["command"]
    assert "--use-fake-ui-for-media-stream" not in captured["command"]
    _clear_fake_browser_generation(0)


def test_browser_launch_opens_requested_start_url(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    class Process:
        pid = 456

        @staticmethod
        def poll():
            return 0

    _install_fake_profile_lock(monkeypatch, tmp_path)
    monkeypatch.setattr(chrome, "setup_worker_profile", lambda *_args: tmp_path)
    monkeypatch.setattr(chrome, "_suppress_restore_nag", lambda _profile: None)
    monkeypatch.setattr(config, "get_chrome_path", lambda: "msedge.exe")
    monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
    monkeypatch.setattr(chrome, "_wait_for_cdp_ready", lambda *_args, **_kwargs: None)

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return Process()

    monkeypatch.setattr(chrome.subprocess, "Popen", fake_popen)

    chrome.launch_chrome(
        worker_id=0,
        port=9432,
        start_url="https://www.linkedin.com/login",
    )

    assert captured["command"][-1] == "https://www.linkedin.com/login"
    _clear_fake_browser_generation(0)


def test_cloak_launch_uses_separate_profile_and_stable_fingerprint(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    profile = tmp_path / "cloak-worker"
    profile.mkdir()

    class Process:
        pid = 789
        returncode = None

        @staticmethod
        def poll():
            return None

    _install_fake_profile_lock(monkeypatch, profile)
    monkeypatch.setattr(chrome, "setup_worker_profile", lambda *_args: profile)
    monkeypatch.setattr(chrome, "_suppress_restore_nag", lambda _profile: None)
    monkeypatch.setattr(chrome, "get_browser_executable", lambda _backend: "cloak-chrome.exe")
    monkeypatch.setattr(chrome, "_wait_for_cdp_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chrome, "_cloak_fingerprint_seed", lambda _profile: 424242)
    monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return Process()

    monkeypatch.setattr(chrome.subprocess, "Popen", fake_popen)

    chrome.launch_chrome(worker_id=0, port=9433, browser_backend="cloak")

    assert captured["command"][0] == "cloak-chrome.exe"
    assert "--fingerprint=424242" in captured["command"]
    assert f"--user-data-dir={profile}" in captured["command"]
    _clear_fake_browser_generation(0)


def test_cloak_fingerprint_seed_persists_per_profile(monkeypatch, tmp_path: Path) -> None:
    values = iter((123456, 654321))
    monkeypatch.setattr(chrome.secrets, "randbelow", lambda _limit: next(values) - 100000)

    first = chrome._cloak_fingerprint_seed(tmp_path)
    second = chrome._cloak_fingerprint_seed(tmp_path)

    assert first == 123456
    assert second == first


def test_agent_watchdog_kills_process_at_wall_clock_deadline(monkeypatch) -> None:
    killed = []

    class RunningProcess:
        pid = 321

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(launcher, "_kill_process_tree", killed.append)
    timed_out, timer = launcher._start_timeout_watchdog(RunningProcess(), 0.01)

    assert timed_out.wait(1)
    timer.join(timeout=1)
    assert killed == [321]


def test_runtime_timeout_status_distinguishes_budget_exhaustion_from_submission_uncertainty() -> None:
    assert (
        launcher._runtime_timeout_status(submission_phase="prepare", dry_run=False)
        == "failed:agent_runtime_timeout"
    )
    assert (
        launcher._runtime_timeout_status(submission_phase="submit", dry_run=False)
        == "submission_uncertain"
    )
    assert (
        launcher._runtime_timeout_status(submission_phase="submit", dry_run=True)
        == "failed:agent_runtime_timeout"
    )


def test_preview_audit_requires_non_submission_and_verified_resume() -> None:
    valid = (
        'RESULT:PREVIEWED\nPREVIEW_AUDIT: {"filled_fields": ["Full name"], '
        '"manual_review_fields": [], "resume_uploaded": true, '
        '"final_control_label": "Submit application", "submission_attempted": false}'
    )

    assert launcher._validate_preview_audit(valid) is None
    fenced = (
        'RESULT:PREVIEWED\n\n```json\n{"filled_fields": ["legal name"], '
        '"manual_review_fields": ["LinkedIn headline is stale"], "resume_uploaded": true, '
        '"final_control_label": "Submit application", "submission_attempted": false}\n```'
    )
    assert launcher._validate_preview_audit(fenced) is None
    mapped_fields = valid.replace(
        '"filled_fields": ["Full name"]',
        '"filled_fields": {"legal_name": "Taylor Chen", "email": "applicant@example.com"}',
    )
    assert launcher._validate_preview_audit(mapped_fields) is None
    assert launcher._validate_preview_audit("RESULT:PREVIEWED") == "preview_audit_missing"
    assert (
        launcher._validate_preview_audit(valid.replace('"resume_uploaded": true', '"resume_uploaded": false'))
        == "preview_resume_not_verified"
    )
    assert (
        launcher._validate_preview_audit(valid.replace('"submission_attempted": false', '"submission_attempted": true'))
        == "preview_submission_state_unsafe"
    )


def test_worker_browser_evidence_is_archived_before_reset(monkeypatch, tmp_path: Path) -> None:
    worker_dir = tmp_path / "worker-0"
    worker_dir.mkdir()
    (worker_dir / "final-preview.png").write_bytes(b"preview")
    (worker_dir / "captcha-blocked.png").write_bytes(b"captcha")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")

    archived = launcher._archive_worker_evidence(
        worker_dir,
        {"company_name": "WIZ.AI", "title": "AI Builder Intern"},
        worker_id=0,
        timestamp="20260814_194446",
    )

    assert [path.name for path in archived] == ["final-preview.png", "captcha-blocked.png"]
    assert all(path.is_file() for path in archived)
    assert archived[0].read_bytes() == b"preview"
    assert "WIZ.AI_AI_Builder_Intern" in str(archived[0].parent)


def test_discovery_accepts_shipped_board_and_country_fields(monkeypatch) -> None:
    captured = {}

    def fake_full_crawl(**kwargs):
        captured.update(kwargs)
        return {"new": 0}

    monkeypatch.setattr(jobspy, "require_jobboards", lambda: object())
    monkeypatch.setattr(jobspy, "_full_crawl", fake_full_crawl)

    result = jobspy.run_discovery(
        {
            "queries": [{"query": "data analyst", "tier": 1}],
            "locations": [{"location": "Singapore", "remote": False}],
            "boards": ["indeed", "linkedin", "google"],
            "country": "Singapore",
            "defaults": {"results_per_site": 10, "hours_old": 168},
        }
    )

    assert result == {"new": 0}
    assert captured["sites"] == ["indeed", "linkedin", "google"]
    assert captured["results_per_site"] == 10
    assert captured["hours_old"] == 168
    assert captured["search_cfg"]["defaults"]["country_indeed"] == "Singapore"


def test_jobspy_retry_uses_configured_wall_clock_timeout(monkeypatch) -> None:
    captured = {}

    def fake_scrape(kwargs, timeout_seconds):
        captured["kwargs"] = kwargs
        captured["timeout_seconds"] = timeout_seconds
        return "completed"

    monkeypatch.setattr(jobspy, "_scrape_once_with_timeout", fake_scrape)

    result = jobspy._scrape_with_retry(
        {"search_term": "AI intern"},
        max_retries=0,
        timeout_seconds=12.5,
    )

    assert result == "completed"
    assert captured == {
        "kwargs": {"search_term": "AI intern"},
        "timeout_seconds": 12.5,
    }


def _application_profile() -> dict:
    return {
        "personal": {
            "full_name": "Taylor Chen",
            "preferred_name": "Ryan",
            "preferred_display_name": "Ryan Yu",
            "email": "applicant@example.com",
            "phone": "+65 9000 0000",
            "phone_country_code": "+65",
            "phone_national_number": "90000000",
            "address": "19 Jurong West Ave 5",
            "address_line_2": "#15/17",
            "city": "Singapore",
            "province_state": "",
            "country": "Singapore",
            "country_of_birth": "China",
            "nationality": "China",
            "postal_code": "649491",
        },
        "work_authorization": {
            "legally_authorized_to_work": "Conditional",
            "require_sponsorship": "Role-specific",
            "work_permit_type": "Student's Pass",
            "form_answer_policy": {
                "programme_credit_bearing_internship": {
                    "legally_authorized": "Yes",
                    "requires_sponsorship": "No",
                    "status": "Approved programme-credit-bearing internship",
                },
                "post_graduation_full_time": {
                    "legally_authorized": "No",
                    "requires_sponsorship": "Yes",
                    "status": "Employer sponsorship required",
                },
            },
        },
        "availability": {
            "earliest_start_date": "2026-11-10",
            "generic_application_availability_date": "2026-11-10",
            "credit_bearing_internship_start": "2026-11-10",
            "credit_bearing_internship_hours_per_week": "Full-time",
        },
        "compensation": {
            "salary_expectation": "Negotiable",
            "salary_currency": "SGD",
            "internship_monthly_min": 1500,
            "internship_monthly_max": 2000,
            "internship_monthly_default": 1750,
            "full_time_annual_min": 55000,
            "full_time_annual_max": 80000,
        },
        "experience": {
            "years_of_experience_total": 2,
            "education_level": "Master's degree; current student",
            "target_role": "Data Analyst",
        },
        "mobility": {
            "willing_to_relocate_within_singapore": "Yes",
            "willing_to_relocate_to_another_country": "No",
            "willing_to_travel": "Yes",
            "maximum_travel_percentage": 25,
        },
        "screening": {
            "age_18_or_older": "Yes",
            "willing_to_complete_background_check": "Yes",
            "willing_to_complete_drug_test": "Yes",
            "criminal_convictions_to_disclose": "No",
            "drivers_license": "No",
            "has_transportation": "Yes",
            "willing_to_sign_nda": "Yes",
            "employment_or_non_compete_restrictions": "None",
            "prior_internship_product_startup_logistics_ecommerce_b2b_saas": False,
            "previously_worked_for_target_employer": "No",
        },
        "eeo_voluntary": {},
        "authentication": {
            "ordinary_ats_sign_in_authorized": True,
            "credential_relay_authorized": True,
            "google_sso_existing_session_authorized": True,
            "ats_account_creation_authorized": True,
            "ats_signup_email": "candidate@example.com",
            "gmail_verification_authorized": True,
            "gmail_verification_mailbox": "candidate@example.com",
        },
        "screening_answer_policy": {
            "required_experience_yes_policy": (
                "Answer Yes when direct or sufficiently adjacent same-domain evidence supports the category."
            ),
            "exact_tool_policy": "Never invent an absent exact framework or duration.",
        },
        "current_employment": {
            "company": "Example",
            "title": "Analyst",
            "current_salary_monthly": 4500,
            "current_salary_currency": "SGD",
        },
        "application_facts": [
            {
                "key": "full_time_internship_earliest_start",
                "value": "2026-11-10",
                "context": "credit-bearing internship",
                "source": "user_confirmed",
                "confirmed_at": "2026-08-22",
            }
        ],
        "application_source": {
            "use_actual_discovery_source": True,
            "form_source_default": "Other",
            "form_source_fallback": "Company website",
        },
        "linkedin_easy_apply": {
            "uploaded_resume_variants": [
                {
                    "filename": "Data Analyst",
                    "keywords": ["data analyst", "sql"],
                },
                {
                    "filename": "AI Automation",
                    "keywords": ["llm", "automation"],
                },
            ]
        },
        "submission_policy": {
            "maximum_verified_submissions_per_rolling_hour": 2,
            "minimum_seconds_between_verified_submissions": 20,
        },
    }


def test_preferred_display_name_is_not_duplicated() -> None:
    profile = _application_profile()

    hard_rules = prompt._build_hard_rules(profile)

    assert 'display name = "Ryan Yu"' in hard_rules
    assert "only when the field explicitly asks" in hard_rules
    assert 'Treat "Full name"' in hard_rules
    assert "Ryan Yu Yu" not in hard_rules


def test_hard_rules_allow_audited_same_level_education_taxonomy() -> None:
    profile = _application_profile()
    profile["education"] = [{
        "institution": "Nanyang Technological University",
        "degree": "Master of Computing in Applied Artificial Intelligence",
    }]

    hard_rules = prompt._build_hard_rules(profile)

    assert "closest option at the same degree level" in hard_rules
    assert "Never select a different degree level" in hard_rules
    assert "Master of Computing in Applied Artificial Intelligence" in hard_rules


def test_salary_guidance_is_singapore_and_quality_first() -> None:
    salary = prompt._build_salary_section(_application_profile())

    assert "no salary-based rejection" in salary
    assert "SGD 1750 per month" in salary
    assert "enter 4500" in salary
    assert "SGD 67500 per year" in salary
    assert "containing that value" in salary
    assert "$110K" not in salary
    assert "Divide your annual answer by 2080" not in salary


def test_profile_summary_uses_configured_screening_and_full_address() -> None:
    summary = prompt._build_profile_summary(_application_profile())

    assert "19 Jurong West Ave 5, #15/17, Singapore, Singapore, 649491" in summary
    assert "Previously Worked Here: No" in summary
    assert "Salary Strategy (SGD): Negotiable" in summary
    assert "Country/Region of Birth: China" in summary
    assert "Citizenship/Nationality: China" in summary


def test_routine_form_defaults_are_not_escalated_to_manual_review() -> None:
    defaults = prompt._build_routine_form_defaults_section(_application_profile())

    assert "Country/Region of Birth -> China" in defaults
    assert "Use the actual discovery source when it is available" in defaults
    assert 'Otherwise prefer "Other"' in defaults
    assert "Do not stop" in defaults


def test_education_country_facts_are_explicit_and_not_inferred_from_nationality() -> None:
    profile = _application_profile()
    profile["education"] = [
        {
            "institution": "Nanyang Technological University",
            "country": "Singapore",
            "degree": "Master of Computing",
            "expected_graduation": "May 2027",
        },
        {
            "institution": "University of Pennsylvania",
            "country": "United States",
            "degree": "Master of City Planning",
            "graduation": "May 2026",
            "gpa": "3.46/4.0",
        },
        {
            "institution": "University College London",
            "country": "United Kingdom",
            "degree": "BSc Urban Planning",
            "graduation": "May 2024",
            "gpa": "3.7/4.0",
        },
    ]
    profile["education_country_answer_policy"] = (
        "For a generic singular 'Study or Graduated In' field, use Singapore "
        "for the current NTU programme. Never substitute nationality or country of birth."
    )

    summary = prompt._build_profile_summary(profile)
    defaults = prompt._build_routine_form_defaults_section(profile)

    assert "Country of study Singapore" in summary
    assert "Country of study United States" in summary
    assert "GPA 3.46/4.0" in summary
    assert "University College London -> United Kingdom" in defaults
    assert "Never substitute nationality or country of birth" in defaults


def test_contextual_application_facts_and_soft_availability_are_rendered() -> None:
    profile = _application_profile()
    profile["availability"]["internship_end_date"] = "2027-06-30"
    profile["availability"]["exact_period_answer_rule"] = (
        "Use the confirmed start and end dates when an exact period is required."
    )

    facts = prompt._build_application_facts_section(profile)
    availability = prompt._build_availability_section(profile)

    assert "full_time_internship_earliest_start: 2026-11-10" in facts
    assert "context: credit-bearing internship" in facts
    assert "usually negotiable" in availability
    assert "automatic rejection gates" in availability
    assert "Confirmed internship end date = 2027-06-30" in availability
    assert "Use the confirmed start and end dates" in availability
    assert "RESULT:FAILED:not_eligible_availability" not in availability


def test_phone_and_linkedin_uploaded_resume_routing_are_contextual() -> None:
    profile = _application_profile()

    assert prompt._national_phone_digits(profile["personal"]) == "90000000"
    assert prompt._linkedin_resume_preference(
        profile,
        {"title": "Data Analyst Intern", "full_description": "Build SQL dashboards."},
    ) == "Data Analyst"


def test_login_policy_allows_google_ats_signup_and_narrow_gmail_verification() -> None:
    steps = prompt._build_login_steps(
        _application_profile(),
        available_tools=("mailbox_search", "mailbox_get_message"),
    )

    assert "Continue with Google" in steps
    assert "already signed-in account" in steps
    assert "candidate@example.com" in steps
    assert "mcp__credential_relay__fill_ats_credentials" in steps
    assert "read-only mailbox tools" in steps
    assert "within the last 10 minutes" in steps
    assert "exact employer ATS mailbox OTP admitted" in steps
    assert "identity-provider security code/security challenge" in steps
    assert "credential_relay_required" in steps
    assert "actively make one bounded ordinary authentication attempt" in steps
    assert "click the ordinary Sign in, Log in, or Continue control" in steps
    assert "reuse an already authenticated browser session" in steps
    assert "already signed-in Google account" in steps
    assert "already signed-in Google or LinkedIn account" not in steps
    assert "Do not use LinkedIn as a third-party ATS OAuth provider" in steps
    assert "Do not return RESULT:LOGIN_ISSUE merely because a login page appears" in steps
    assert "Credential relay is independently authorized" in steps
    assert "must not click Sign in, Continue, Apply, or Submit" in steps
    assert "authorizes this relay without a separate confirmation" in steps
    assert "Only after that one bounded attempt fails" in steps
    assert "account recovery, unavailable authorized credentials, or broader OAuth scopes" in steps

    claude_steps = prompt._build_login_steps(
        _application_profile(),
        agent_backend="claude",
        available_tools=("mailbox_search", "mailbox_get_message"),
    )
    assert ".\\fill-ats-credentials.ps1" in claude_steps
    assert "never repeat the code" in claude_steps
    assert "within the last 10 minutes" in claude_steps
    assert "password reset" in claude_steps


def test_linkedin_ordinary_login_policy_defers_apply_to_launcher() -> None:
    steps = prompt._build_login_steps(
        _application_profile(),
        application_url="https://www.linkedin.com/jobs/view/4455274411/",
    )

    assert "current host is linkedin.com" in steps
    assert "Continue with Google" in steps
    assert "launcher exclusively owns" in steps
    assert "Do not click that control in an ordinary Agent turn" in steps
    assert "unexpected login dialog requires RESULT:LOGIN_ISSUE" in steps
    assert "identity-provider security code/security challenge, account recovery" in steps


def test_linkedin_login_only_prompt_forbids_agent_apply_and_navigation(
    monkeypatch, tmp_path: Path
) -> None:
    resume_txt = tmp_path / "tailored.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_txt.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(config, "load_profile", _application_profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    job = {
        "url": "https://www.linkedin.com/jobs/view/4455274411/",
        "application_url": "https://www.linkedin.com/jobs/view/4455274411/",
        "title": "Data Analyst Intern",
        "company_name": "Example",
        "source_site": "linkedin",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
        "_linkedin_login_only": True,
        "_linkedin_login_entry_stage": "pre_entry_authwall",
    }

    built = prompt.build_prompt(job, "Verified resume", dry_run=False)

    assert "through Google 继续" not in built
    assert "通过 Google 继续" in built
    assert "before any Apply control was clicked" in built
    assert "ordinary Sign in or 登录" in built
    assert "Do not click Join now" in built
    assert "Do not click Apply, Easy Apply, 申请, 轻松申请" in built
    assert "Do not call browser_navigate" in built
    assert "launcher owns the second causal Apply click" in built
    assert "RESULT:LINKEDIN_LOGIN_COMPLETED" in built
    assert "Upload the bound Resume PDF" not in built

    job["_linkedin_login_entry_stage"] = "pre_entry_login_dialog"
    dialog_prompt = prompt.build_prompt(job, "Verified resume", dry_run=False)
    assert "presented a login dialog before any Apply control was clicked" in dialog_prompt
    assert "already clicked the exact current job's primary Apply control" not in dialog_prompt


def test_ats_account_creation_does_not_authorize_google_or_linkedin_sso() -> None:
    profile = _application_profile()
    profile["authentication"]["google_sso_existing_session_authorized"] = False

    steps = prompt._build_login_steps(
        profile,
        application_url="https://careers.example.com/jobs/123",
    )

    assert "Google SSO reuse is not authorized" in steps
    assert "Credential relay is independently authorized" in steps
    assert "Do not use LinkedIn as a third-party ATS OAuth provider" in steps


def test_explicit_login_capabilities_do_not_authorize_account_creation() -> None:
    profile = _application_profile()
    profile["authentication"].update(
        {
            "ordinary_ats_sign_in_authorized": True,
            "credential_relay_authorized": True,
            "ats_account_creation_authorized": False,
            "google_sso_existing_session_authorized": False,
        }
    )

    steps = prompt._build_login_steps(profile)

    assert "trusted profile already authorizes this ordinary login action" in steps
    assert "Credential relay is independently authorized" in steps
    assert "Do not create a new account" in steps
    assert "does not authorize account creation" in steps
    assert "Google SSO reuse is not authorized" in steps
    assert "must not click Sign in, Continue, Apply, or Submit" in steps


def test_explicit_login_capabilities_override_legacy_account_creation_fallback() -> None:
    profile = _application_profile()
    profile["authentication"].update(
        {
            "ordinary_ats_sign_in_authorized": False,
            "credential_relay_authorized": False,
            "ats_account_creation_authorized": True,
        }
    )

    steps = prompt._build_login_steps(profile)

    assert "Do not start an ordinary first-party ATS sign-in flow" in steps
    assert "Credential relay is not authorized" in steps
    assert "account creation with candidate@example.com is authorized" in steps


def test_legacy_account_creation_policy_still_authorizes_bounded_sign_in_and_relay() -> None:
    profile = {
        "personal": {"email": "legacy@example.com"},
        "authentication": {"ats_account_creation_authorized": True},
    }

    steps = prompt._build_login_steps(profile)

    assert "trusted profile already authorizes this ordinary login action" in steps
    assert "Credential relay is independently authorized" in steps
    assert "account creation with legacy@example.com is authorized" in steps


def test_authorized_login_result_code_requires_a_bounded_attempt(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _application_profile()
    resume_txt = tmp_path / "tailored.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_txt.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")
    job = {
        "url": "https://example.com/job",
        "title": "Data Analyst Intern",
        "company_name": "Example",
        "source_site": "lever",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_path": None,
        "cover_letter_status": "not_required",
    }

    built = prompt.build_prompt(job, "Verified resume", dry_run=False)

    assert (
        "RESULT:LOGIN_ISSUE -- one bounded authorized authentication attempt failed, "
        "or MFA/identity-provider security challenge, recovery, unavailable authorized credentials, or abnormal OAuth scope blocked it"
        in built
    )


def test_login_policy_stops_when_google_reuse_is_not_authorized() -> None:
    profile = _application_profile()
    profile.pop("authentication")

    steps = prompt._build_login_steps(profile)

    assert "do not authenticate or create an account" in steps
    assert "Continue with Google" not in steps


def test_screening_uses_configured_mobility() -> None:
    screening = prompt._build_screening_section(_application_profile())

    assert "willing to relocate within Singapore: Yes" in screening
    assert "willing to relocate to another country: No" in screening
    assert "maximum 25%" in screening
    assert "cannot relocate" not in screening


def test_standing_screening_facts_render_only_their_exact_confirmed_scope() -> None:
    profile = _application_profile()
    profile["screening"].update({
        "government_or_public_agency_employment_last_5_years": "No",
        "civil_servant_cabinet_or_legislator_last_5_years": "No",
        "conflict_of_interest_activities_at_target_employer": "No",
        "family_or_close_relationship_employed_by_major_company": "No",
        "government_body_regulatory_or_procurement_relationship_with_target_employer": "No",
        "family_or_close_relationship_government_influence_over_target_employer": "No",
    })

    summary = prompt._build_profile_summary(profile)
    screening = prompt._build_screening_section(profile)

    expected_facts = (
        "Government/public-agency employment in the last 5 years: No",
        "Civil servant, cabinet member, or legislator in the last 5 years: No",
        "Target-employer conflict-of-interest activities: No",
        "Family/close relationship employed by a major company in conflict-of-interest scope: No",
        "Government regulatory/procurement relationship with target employer: No",
        "Family/close relationship with government influence over target employer: No",
    )
    for fact in expected_facts:
        assert fact in summary
        assert fact in screening
    assert "do not answer other identity, criminal, work-authorization" in screening
    assert "Previous employment at this employer: No" in screening
    assert "Use the configured previous-employment fact" in screening


def test_missing_standing_screening_facts_remain_manual_review() -> None:
    profile = _application_profile()

    summary = prompt._build_profile_summary(profile)
    screening = prompt._build_screening_section(profile)

    assert "Government/public-agency employment in the last 5 years: Manual review" in summary
    assert "Civil servant, cabinet member, or legislator in the last 5 years: Manual review" in screening
    assert "Target-employer conflict-of-interest activities: Manual review" in screening
    assert (
        "Family/close relationship employed by a major company in conflict-of-interest scope: "
        "Manual review" in screening
    )
    assert "Government regulatory/procurement relationship with target employer: Manual review" in screening
    assert (
        "Family/close relationship with government influence over target employer: Manual review"
        in screening
    )
    assert "Previously Worked Here: No" in summary


def test_screening_allows_supported_adjacent_category_but_not_exact_tool_claim() -> None:
    screening = prompt._build_screening_section(_application_profile())

    assert "sufficiently adjacent same-domain" in screening
    assert "hybrid-RAG" in screening
    assert "justify YES" in screening
    assert "Never invent an absent exact framework or duration" in screening
    assert "Never invent exact years or months" in screening


def test_credential_relay_host_policy_is_ats_scoped() -> None:
    assert credential_relay.host_is_allowed("jobs.lever.co", set()) is False
    assert credential_relay.host_is_known_ats("jobs.lever.co") is True
    assert credential_relay.host_is_allowed(
        "careers.example.com", {"careers.example.com"}
    ) is True
    assert credential_relay.host_is_allowed("accounts.google.com", {"google.com"}) is False
    assert credential_relay.host_is_allowed("evil.example", {"careers.example.com"}) is False


def test_credential_password_host_requires_known_ats_or_explicit_profile_host(
    monkeypatch,
) -> None:
    monkeypatch.delenv("APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS", raising=False)
    assert credential_relay._password_host_is_allowed("careers.example.com") is False
    assert credential_relay._password_host_is_allowed("jobs.lever.co") is True
    monkeypatch.setenv(
        "APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS", "careers.example.com"
    )
    assert credential_relay._password_host_is_allowed("careers.example.com") is True


def test_credential_relay_prefers_exact_host_over_unique_ats_redirect() -> None:
    exact = {
        "page_index": 1,
        "frame_url": "https://careers.example.com/login",
        "page_url": "https://careers.example.com/login",
        "match": "exact",
        "field_count": 1,
    }
    redirect = {
        "page_index": 2,
        "frame_url": "https://jobs.lever.co/example",
        "page_url": "https://jobs.lever.co/example",
        "match": "known_ats_redirect",
        "field_count": 2,
    }

    assert credential_relay._select_candidate([redirect, exact]) is exact


def test_credential_relay_rejects_multiple_matching_tabs() -> None:
    candidates = [
        {
            "page_index": index,
            "frame_url": url,
            "page_url": url,
            "match": "known_ats_redirect",
            "field_count": 2,
        }
        for index, url in enumerate(
            ("https://jobs.lever.co/one", "https://jobs.lever.co/two")
        )
    ]

    with pytest.raises(credential_relay.CredentialRelayError, match="multiple browser tabs"):
        credential_relay._select_candidate(candidates)


def test_credential_relay_target_lineage_allows_redirect_descendant_only() -> None:
    infos = {
        "root": {"targetId": "root"},
        "ats-child": {"targetId": "ats-child", "openerId": "root"},
        "unrelated": {"targetId": "unrelated"},
    }

    assert credential_relay._target_descends_from("root", {"root"}, infos) is True
    assert credential_relay._target_descends_from("ats-child", {"root"}, infos) is True
    assert credential_relay._target_descends_from("unrelated", {"root"}, infos) is False


def test_credential_relay_both_mode_rejects_partial_forms() -> None:
    assert credential_relay._requested_fields_present("both", True, True) is True
    assert credential_relay._requested_fields_present("both", True, False) is False
    assert credential_relay._requested_fields_present("email", True, False) is True


def test_credential_relay_fills_password_and_confirmation_without_exposing_value() -> None:
    class Field:
        def __init__(self) -> None:
            self.values: list[str] = []

        def fill(self, value: str) -> None:
            self.values.append(value)

    password = Field()
    confirmation = Field()

    assert credential_relay._fill_password_fields(
        [password, confirmation], "test-only-secret"
    ) == 2
    assert password.values == ["test-only-secret"]
    assert confirmation.values == ["test-only-secret"]

    with pytest.raises(credential_relay.CredentialRelayError, match="more than two"):
        credential_relay._fill_password_fields(
            [password, confirmation, Field()], "test-only-secret"
        )


def test_apply_backend_defaults_to_codex(monkeypatch) -> None:
    monkeypatch.delenv("APPLYPILOT_APPLY_BACKEND", raising=False)

    assert config.get_apply_backend() == "codex"


def test_pdf_renderer_preserves_education_rows() -> None:
    rendered = pdf_renderer.build_html(
        {
            "name": "Ryan Yu",
            "title": "AI Intern",
            "location": "",
            "contact": "candidate@example.com",
            "sections": {
                "EDUCATION": "NTU | MCAAI\nUPenn | MCP & Analytics",
            },
        }
    )

    assert "NTU | MCAAI<br>UPenn | MCP &amp; Analytics" in rendered


def test_pdf_renderer_supports_contact_directly_below_name_and_source_section_order() -> None:
    parsed = pdf_renderer.parse_resume(
        "Ryan Yu\nryan@example.com | github.com/ryan\n\n"
        "SUMMARY\nApplied AI developer building grounded agent workflows.\n\n"
        "EDUCATION\nNTU | MCAAI\n\n"
        "TECHNICAL SKILLS\nApplied AI: Python, RAG\n\n"
        "EXPERIENCE\nExample Co.\nAI Intern | 2026\n- Built a grounded workflow."
    )
    rendered = pdf_renderer.build_html(parsed)

    assert parsed["title"] == ""
    assert parsed["contact"].startswith("ryan@example.com")
    assert '<div class="title">' not in rendered
    assert "text-wrap: pretty" in rendered
    assert ".skill-row" in rendered
    assert "break-after: avoid" in rendered
    assert rendered.index("Education</div>") < rendered.index("Technical Skills</div>")


def test_pdf_summary_tail_density_requires_five_words_after_wrapping() -> None:
    assert pdf_renderer._summary_tail_is_dense([17]) is True
    assert pdf_renderer._summary_tail_is_dense([17, 5]) is True
    assert pdf_renderer._summary_tail_is_dense([17, 4]) is False


def test_pdf_technical_skill_tail_density_checks_every_row() -> None:
    assert pdf_renderer._skill_tails_are_dense([[12], [11, 5]]) is True
    assert pdf_renderer._skill_tails_are_dense([[12], [11, 4]]) is False
    assert pdf_renderer._skill_tails_are_dense([[12], [11, 5]], min_words=6) is False


def test_pdf_multi_page_gate_requires_a_usefully_filled_final_page() -> None:
    assert pdf_renderer._last_page_is_usefully_filled([860.0]) is True
    assert pdf_renderer._last_page_is_usefully_filled([860.0, 350.0]) is True
    assert pdf_renderer._last_page_is_usefully_filled([860.0, 300.0]) is False


def test_internship_resume_assembly_prioritizes_education_and_omits_target_title() -> None:
    _, _, data = _grounded_tailor_payload()
    data["title"] = "AI Application Development Intern"
    profile = {
        "personal": {
            "preferred_display_name": "Ryan Yu",
            "email": "ryan@example.com",
            "github_url": "https://github.com/raederhans/",
        },
        "tailoring": {
            "resume_layout": {
                "internship_section_order": [
                    "SUMMARY", "EDUCATION", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS"
                ]
            }
        },
    }

    assembled = tailor.assemble_resume_text(data, profile)
    header = [line for line in assembled.splitlines() if line][:2]

    assert header == ["Ryan Yu", "ryan@example.com | github.com/raederhans"]
    assert "AI Application Development Intern" not in assembled
    assert assembled.index("\nEDUCATION\n") < assembled.index("\nTECHNICAL SKILLS\n")


def test_resume_layout_validator_rejects_title_line_and_tiny_summary_sentence() -> None:
    text = (
        "Ryan Yu\nAI Intern\nryan@example.com\n\nSUMMARY\n"
        "Applied AI developer building grounded workflows. Strong fit.\n\n"
        "TECHNICAL SKILLS\nApplied AI: Python\n\nEXPERIENCE\nExample Co.\n"
        "Role | 2026\n- Built a grounded workflow with Python.\n\n"
        "EDUCATION\nExample University"
    )
    profile = {
        "personal": {"preferred_display_name": "Ryan Yu", "email": "ryan@example.com"},
        "tailoring": {
            "resume_layout": {
                "header_contact_immediately_after_name": True,
                "summary_min_final_sentence_words": 8,
            }
        },
    }

    result = validate_tailored_resume(text, profile)

    assert any("contact line immediately after the name" in error for error in result["errors"])
    assert any("undersized sentence" in error for error in result["errors"])


def test_singapore_citizen_requirement_is_scored_not_hard_excluded() -> None:
    status, reason = evaluate_job_eligibility(
        {
            "title": "AI Apprentice",
            "full_description": "**Eligibility**\n* Singapore Citizen\n* Degree holder",
        }
    )

    assert status == "eligible"
    assert reason is None


def test_soft_citizen_encouragement_is_not_excluded() -> None:
    status, reason = evaluate_job_eligibility(
        {
            "title": "AI Intern",
            "full_description": "Singapore citizens are encouraged to apply, and all qualified applicants are welcome.",
        }
    )

    assert status == "eligible"
    assert reason is None


def test_singapore_pr_requirement_is_scored_not_hard_excluded() -> None:
    status, reason = evaluate_job_eligibility(
        {
            "title": "Data Intern",
            "full_description": "Eligibility:\n- Singapore Permanent Resident",
        }
    )

    assert status == "eligible"
    assert reason is None


@pytest.mark.parametrize(
    ("citizen", "permanent_resident", "expected_status"),
    [
        (False, True, "eligible"),
        (True, False, "eligible"),
        (False, False, "ineligible"),
    ],
)
def test_explicit_citizen_or_pr_exclusion_respects_either_eligible_status(
    citizen: bool, permanent_resident: bool, expected_status: str
) -> None:
    status, _reason = evaluate_job_eligibility(
        {
            "title": "Data Intern",
            "full_description": (
                "If you are not a Singapore citizen or permanent resident, "
                "do not apply."
            ),
        },
        profile={
            "personal": {"nationality": "Malaysia"},
            "work_authorization": {
                "singapore_citizen": citizen,
                "singapore_permanent_resident": permanent_resident,
            },
        },
    )

    assert status == expected_status


def test_ineligible_jobs_are_hidden_from_stages_and_stats(
    tmp_path: Path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    monkeypatch.setattr(
        "applypilot.config.load_profile",
        lambda: {"personal": {"nationality": "China"}},
    )
    jobs = [
        (
            "https://example.com/citizen-only",
            "AI Apprentice",
            "If you are not a Singapore citizen, please do not apply.",
        ),
        (
            "https://example.com/open",
            "AI Engineer Intern",
            "Open to qualified students in Singapore.",
        ),
    ]
    for url, title, description in jobs:
        conn.execute(
            "INSERT INTO jobs (url, title, description, full_description, detail_scraped_at) "
            "VALUES (?, ?, ?, ?, 'now')",
            (url, title, description, description),
        )
    conn.commit()

    eligibility = refresh_job_eligibility(
        conn, profile={"personal": {"nationality": "China"}}
    )
    pending = get_jobs_by_stage(conn=conn, stage="pending_score", limit=0)
    stats = get_stats(conn)

    assert eligibility["ineligible"] == 1
    assert [job["url"] for job in pending] == ["https://example.com/open"]
    assert stats["total"] == 1
    assert stats["excluded_ineligible"] == 1


def test_strict_cover_letter_rejects_truncated_response() -> None:
    result = validate_cover_letter(
        "Dear Hiring Manager,\n\nI built a hybrid RAG assistant. That",
        mode="strict",
        expected_signoff="Ryan Yu",
    )

    assert result["passed"] is False
    assert any("incomplete or truncated" in error for error in result["errors"])
    assert any("Must end with" in error for error in result["errors"])


def test_strict_cover_letter_accepts_complete_four_paragraph_body() -> None:
    paragraphs = [
        "I am applying for the AI Engineer internship at Infineon because the role connects reliable data preparation with production-facing artificial intelligence systems for semiconductor engineering teams.",
        "At Damon and Ryan, I built a Python hybrid RAG legal assistant using curated examples, tool calling, and an API workflow for approximately ten users working across fifteen years of public records.",
        "During my work with WRT, I maintained Python and SQL analysis pipelines across ten years of transportation and property data, then validated outputs for seven reports and three client briefings.",
        "Infineon's focus on structured technical knowledge and dependable engineering workflows makes this role a strong setting for my applied AI experience, validation discipline, and current NTU computing studies.",
    ]
    letter = (
        "Dear Hiring Manager,\n\n"
        + "\n\n".join(paragraphs)
        + "\n\nSincerely,\n\nRyan Yu"
    )

    result = validate_cover_letter(
        letter,
        mode="strict",
        expected_signoff="Ryan Yu",
    )

    assert result["passed"] is True
    assert result["errors"] == []
    assert any("not a hard limit" in warning for warning in result["warnings"])


def test_cover_letter_repetition_is_a_hard_error() -> None:
    repeated = "I built a Python RAG workflow that connected verified records to a reliable API result."
    letter = (
        "Dear Hiring Manager,\n\n"
        f"{repeated}\n\n{repeated}\n\n"
        "Infineon needs dependable engineering evidence, and my validation work addresses that requirement directly.\n\n"
        "Sincerely,\n\nRyan Yu"
    )

    result = validate_cover_letter(letter, mode="strict", expected_signoff="Ryan Yu")

    assert result["passed"] is False
    assert any("Repeated or near-duplicate sentences" in error for error in result["errors"])


def test_cover_letter_rejects_invented_current_title() -> None:
    letter = (
        "Dear Hiring Manager,\n\n"
        "I am applying for the AI Engineer internship at Infineon because the role connects dependable data preparation with practical artificial intelligence systems.\n\n"
        "I currently hold a Modeling and Data Lead role, where I build Python workflows and review technical outputs with collaborators before client delivery.\n\n"
        "My additional project work includes a hybrid RAG assistant and API validation, which directly supports the role's emphasis on reliable AI engineering.\n\n"
        "Infineon's semiconductor setting would let me contribute careful implementation and validation while developing deeper production engineering experience.\n\n"
        "Sincerely,\n\nRyan Yu"
    )

    result = validate_cover_letter(
        letter,
        mode="strict",
        expected_signoff="Ryan Yu",
        company_name="Infineon Technologies",
        expected_current_title="Consulting & AI Solutions Lead",
        expected_current_company="Damon & Ryan Design and Planning LLC.",
    )

    assert result["passed"] is False
    assert any("configured title exactly" in error for error in result["errors"])


def test_cover_letter_accepts_exact_current_title_and_company() -> None:
    letter = (
        "Dear Hiring Manager,\n\n"
        "I am applying for the AI Engineer internship at Infineon because the role connects dependable data preparation with practical artificial intelligence systems.\n\n"
        "I currently work as Consulting & AI Solutions Lead at Damon & Ryan Design and Planning LLC., where I build Python workflows and review technical outputs before client delivery.\n\n"
        "My additional project work includes a hybrid RAG assistant and API validation, which directly supports the role's emphasis on reliable AI engineering.\n\n"
        "Infineon's semiconductor setting would let me contribute careful implementation and validation while developing deeper production engineering experience.\n\n"
        "Sincerely,\n\nRyan Yu"
    )

    result = validate_cover_letter(
        letter,
        mode="strict",
        expected_signoff="Ryan Yu",
        company_name="Infineon Technologies",
        expected_current_title="Consulting & AI Solutions Lead",
        expected_current_company="Damon & Ryan Design and Planning LLC.",
    )

    assert not any("Current-role statement" in error for error in result["errors"])


def test_cover_evidence_excerpt_prefers_jd_relevant_lines() -> None:
    source = (
        "Designed an unrelated urban plaza concept for a studio review.\n"
        "Built a Python hybrid RAG assistant with tool calling and verified legal citations.\n"
        "Prepared general presentation materials for stakeholders.\n"
        "Validated API outputs against curated examples and human review annotations."
    )

    excerpt = cover_letter._source_excerpt(
        source,
        "Build Python RAG and API workflows with validation and tool calling.",
        max_lines=2,
    )

    assert "Python hybrid RAG" in excerpt
    assert "Validated API outputs" in excerpt
    assert "urban plaza" not in excerpt


def test_empty_evidence_plan_response_is_a_validation_failure() -> None:
    with pytest.raises(cover_letter.CoverLetterValidationError, match="empty model response"):
        cover_letter._parse_json_object("   ")


def test_compact_evidence_plan_protocol_is_parsed() -> None:
    payload = cover_letter._parse_evidence_plan_response(
        "MAP: Python workflow validation ||| primary_selected_resume ||| Built a Python workflow with verified API outputs and human review.\n"
        "MAP: data pipelines ||| supplemental_resume_1 ||| Maintained Python and SQL pipelines across ten years of transportation data.\n"
        "MAP: semiconductor experience ||| NONE ||| NONE\n"
        "COMPANY_REASON: The role applies AI to semiconductor failure analysis."
    )

    assert len(payload["requirements"]) == 3
    assert payload["requirements"][0]["source_label"] == "primary_selected_resume"
    assert payload["requirements"][2]["gap"] == "No source-verified quote"
    assert "semiconductor failure analysis" in payload["company_specific_reason"]


def test_evidence_plan_retries_one_empty_model_response(monkeypatch) -> None:
    responses = iter([
        "",
        (
            "MAP: Python workflow validation ||| primary_selected_resume ||| Built a Python workflow with verified API outputs and human review.\n"
            "MAP: data pipelines ||| primary_selected_resume ||| Maintained Python and SQL pipelines across ten years of transportation data.\n"
            "MAP: semiconductor experience ||| NONE ||| NONE\n"
            "COMPANY_REASON: The role applies AI to semiconductor analysis."
        ),
    ])

    class FakeClient:
        def chat(self, *args, **kwargs):
            return next(responses)

    monkeypatch.setenv("APPLYPILOT_PLAN_MAX_RETRIES", "1")
    monkeypatch.setattr(cover_letter, "get_client", lambda: FakeClient())
    source_text = (
        "Built a Python workflow with verified API outputs and human review.\n"
        "Maintained Python and SQL pipelines across ten years of transportation data."
    )

    plan = cover_letter.build_evidence_plan(
        {
            "title": "AI Intern",
            "company_name": "Example Semiconductor",
            "full_description": "Python workflow validation and data pipelines for semiconductor analysis.",
        },
        [{"label": "primary_selected_resume", "path": "resume.txt", "text": source_text}],
    )

    assert len(plan["requirements"]) == 3
    assert sum(bool(item["source_quote"]) for item in plan["requirements"]) == 2


def test_jobspy_persists_company_separately_from_source(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "jobs.db")
    frame = pd.DataFrame([
        {
            "job_url": "https://example.com/job/1",
            "title": "AI Intern",
            "company": "Example Semiconductor",
            "location": "Singapore",
            "description": "Build machine learning workflows " * 20,
            "site": "linkedin",
            "job_url_direct": "https://example.com/apply/1",
        }
    ])

    assert jobspy.store_jobspy_results(conn, frame, "linkedin") == (1, 0)
    row = conn.execute(
        "SELECT company_name, source_site, site FROM jobs WHERE url=?",
        ("https://example.com/job/1",),
    ).fetchone()

    assert tuple(row) == ("Example Semiconductor", "linkedin", "linkedin")


def test_linkedin_identity_dedupes_exact_id_but_keeps_repost_candidate(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "jobs.db")
    first = {
        "url": "https://www.linkedin.com/jobs/view/data-analyst-123456789?trackingId=old",
        "title": "Data Analyst",
        "company_name": "Example Co.",
        "description": "Analyze data",
        "location": "Singapore",
    }

    assert extract_platform_job_id(first["url"]) == "linkedin:123456789"
    assert canonicalize_job_url(first["url"]) == (
        "https://www.linkedin.com/jobs/view/123456789"
    )
    assert store_jobs(conn, [first], "linkedin", "test") == (1, 0)
    conn.execute(
        "UPDATE jobs SET applied_at = ?, apply_status = 'applied' WHERE url = ?",
        (datetime.now(UTC).isoformat(), first["url"]),
    )
    conn.commit()

    same_id = dict(first, url="https://linkedin.com/jobs/view/123456789?refId=new")
    assert store_jobs(conn, [same_id], "linkedin", "test") == (0, 1)

    repost = dict(first, url="https://www.linkedin.com/jobs/view/data-analyst-987654321")
    assert store_jobs(conn, [repost], "linkedin", "test") == (1, 0)
    row = conn.execute(
        "SELECT dedupe_status, possible_repost_of FROM jobs WHERE url = ?",
        (repost["url"],),
    ).fetchone()
    assert tuple(row) == ("possible_repost", first["url"])


def test_existing_job_page_is_upgraded_to_explicit_official_apply_url(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "apply-url-upgrade.db")
    job_url = "https://jobs.ashbyhq.com/example/job-1"
    first = {
        "url": job_url,
        "application_url": job_url,
        "title": "Data Analyst Intern",
        "company_name": "Example",
        "location": "Singapore",
        "description": "Analyze product data.",
    }
    assert store_jobs(conn, [first], "official_careers", "ashby") == (1, 0)

    explicit_apply = f"{job_url}/application"
    refreshed = {**first, "application_url": explicit_apply}
    assert store_jobs(conn, [refreshed], "official_careers", "ashby") == (0, 1)
    assert conn.execute(
        "SELECT application_url FROM jobs WHERE url = ?", (job_url,)
    ).fetchone()[0] == explicit_apply

    manually_verified = "https://apply.example.test/custom-flow"
    conn.execute(
        "UPDATE jobs SET application_url = ? WHERE url = ?", (manually_verified, job_url)
    )
    conn.commit()
    assert store_jobs(conn, [refreshed], "official_careers", "ashby") == (0, 1)
    assert conn.execute(
        "SELECT application_url FROM jobs WHERE url = ?", (job_url,)
    ).fetchone()[0] == manually_verified


def test_unanswered_questions_are_parsed_and_attached_to_existing_job(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/jobs/1"
    store_jobs(
        conn,
        [{"url": url, "title": "Analyst", "company_name": "Example"}],
        "example",
        "test",
    )
    output = (
        'RESULT:FAILED:manual_review_required\nUNANSWERED_QUESTIONS: '
        '[{"question":"Expected salary?","field_type":"number",'
        '"required":true,"reason":"No confirmed full-time value",'
        '"proposed_context":"full-time role"}]'
    )
    questions = launcher._parse_unanswered_questions(output)

    assert questions is not None
    record_unanswered_questions(url, questions, conn)
    records = get_unanswered_questions(conn)
    assert records[0]["questions"][0]["question"] == "Expected salary?"
    assert records[0]["questions"][0]["required"] is True


def test_submission_rate_policy_applies_gap_and_hourly_cap(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "jobs.db")
    now = datetime.now(UTC)
    for index, seconds_ago in enumerate((5, 30), start=1):
        url = f"https://example.com/jobs/{index}"
        store_jobs(conn, [{"url": url, "title": str(index)}], "example", "test")
        conn.execute(
            "UPDATE jobs SET applied_at = ?, apply_status = 'applied' WHERE url = ?",
            ((now - timedelta(seconds=seconds_ago)).isoformat(), url),
        )
    conn.commit()
    profile = _application_profile()

    allowed, cooldown, reason = launcher._submission_rate_status(conn, profile, now)
    assert allowed is False
    assert cooldown == 0
    assert reason == "rolling_hour_submission_cap"

    profile["submission_policy"]["maximum_verified_submissions_per_rolling_hour"] = 3
    allowed, cooldown, reason = launcher._submission_rate_status(conn, profile, now)
    assert allowed is True
    assert 14 <= cooldown <= 16
    assert reason == "minimum_submission_gap"


def test_submission_observation_updates_state_without_setting_a_retry_block(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://www.linkedin.com/jobs/view/123456789"
    store_jobs(conn, [{"url": url, "title": "Analyst"}], "linkedin", "test")

    status = record_submission_observation(
        url,
        {
            "submit_clicked": True,
            "receipt_visible": False,
            "page_url": url + "/apply/",
        },
        conn,
    )
    row = conn.execute(
        "SELECT apply_status, apply_retry_blocked, verification_confidence "
        "FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()

    assert status == "submission_uncertain"
    assert tuple(row) == (
        "submission_uncertain",
        1,
        "browser_observation_pending",
    )

    status = record_submission_observation(
        url,
        {
            "receipt_visible": True,
            "receipt_structured": True,
            "page_url": url + "/apply/",
        },
        conn,
    )
    row = conn.execute(
        "SELECT apply_status, applied_at, verification_confidence FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    assert status == "applied"
    assert row["apply_status"] == "applied"
    assert row["applied_at"]
    assert row["verification_confidence"] == "browser_observation"


def test_linkedin_applied_export_merges_manual_and_existing_applications(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    existing_url = "https://www.linkedin.com/jobs/view/123456789"
    store_jobs(
        conn,
        [{"url": existing_url, "title": "Existing title", "company_name": "Example"}],
        "linkedin",
        "test",
    )
    export_path = tmp_path / "linkedin-applied.json"
    export_path.write_text(
        json.dumps(
            {
                "applications": [
                    {
                        "url": existing_url + "?trackingId=manual",
                        "title": "Should not overwrite",
                        "company": "Example",
                        "location": "Singapore (Hybrid)",
                        "applied_at": "2026-08-22T10:00:00+08:00",
                    },
                    {
                        "job_url": "https://www.linkedin.com/jobs/view/new-role-987654321/",
                        "title": "New manual application",
                        "company_name": "Manual Co",
                        "location": "Singapore",
                    },
                    {"url": "https://example.com/not-linkedin"},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = import_linkedin_applied_export(export_path, conn)

    assert {
        key: result[key] for key in ("inserted", "updated", "skipped")
    } == {"inserted": 1, "updated": 1, "skipped": 1}
    assert result["snapshot_id"]
    assert result["completeness"] == "partial"
    existing = conn.execute(
        "SELECT title, location, apply_status, verification_confidence FROM jobs "
        "WHERE platform_job_id = 'linkedin:123456789'"
    ).fetchone()
    imported = conn.execute(
        "SELECT title, company_name, location, apply_status, application_evidence FROM jobs "
        "WHERE platform_job_id = 'linkedin:987654321'"
    ).fetchone()
    assert tuple(existing) == (
        "Existing title",
        "Singapore (Hybrid)",
        "applied",
        "platform_export",
    )
    assert tuple(imported) == (
        "New manual application",
        "Manual Co",
        "Singapore",
        "applied",
        "linkedin_applied_export",
    )


def test_application_fact_revision_history_is_append_only_knowledge(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    first_id = record_application_fact_revision(
        "experience.power_bi.years",
        1,
        2,
        context="screening.experience",
        confirmed_at="2026-08-22",
        note="User corrected the reusable answer",
        conn=conn,
    )
    second_id = record_application_fact_revision(
        "experience.power_bi.years",
        2,
        3,
        context="screening.experience",
        confirmed_at="2026-09-01",
        conn=conn,
    )

    history = get_application_fact_revisions(
        "experience.power_bi.years", conn=conn
    )

    assert second_id > first_id
    assert [item["new_value"] for item in history] == [3, 2]
    assert history[1]["old_value"] == 1
    assert history[1]["source"] == "user_confirmed"


def test_import_exact_job_registers_enrichment_candidate(monkeypatch, tmp_path: Path) -> None:
    from applypilot import single_job

    db_path = tmp_path / "applypilot.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    init_db()

    result = single_job.import_exact_job(
        "https://www.linkedin.com/jobs/view/123456",
        "AI Intern",
        "Example AI",
        "Singapore",
        "linkedin",
    )

    assert result["strategy"] == "exact_url"
    assert result["company"] == "Example AI"
    assert result["eligibility_status"] == "eligible"
    assert result["needs_enrichment"] is True


def test_import_exact_job_rejects_non_https_url() -> None:
    from applypilot import single_job

    with pytest.raises(ValueError, match="absolute HTTPS"):
        single_job.import_exact_job(
            "http://example.test/job/1",
            "AI Intern",
            "Example AI",
        )


def test_import_exact_job_preserves_distinct_official_application_url(
    monkeypatch, tmp_path: Path
) -> None:
    from applypilot import single_job

    db_path = tmp_path / "applypilot.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    init_db()

    result = single_job.import_exact_job(
        "https://careers.example.test/jobs/1",
        "Data Intern",
        "Example",
        site="company_careers",
        application_url="https://ats.example.test/apply/1",
    )

    assert result["application_url"] == "https://ats.example.test/apply/1"


def test_score_exact_job_persists_explicit_resume_for_tailoring(
    monkeypatch, tmp_path: Path
) -> None:
    from applypilot import single_job

    conn = init_db(tmp_path / "jobs.db")
    url = "https://careers.example.test/jobs/data"
    resume = tmp_path / "data-resume.txt"
    resume.write_text("Python SQL dashboards", encoding="utf-8")
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, full_description, eligibility_status) "
        "VALUES (?, 'Data Analyst', 'Example', 'Build dashboards with SQL.', 'eligible')",
        (url,),
    )
    conn.commit()
    monkeypatch.setattr(single_job, "get_connection", lambda: conn)
    monkeypatch.setattr(single_job, "load_profile", dict)
    monkeypatch.setattr(
        single_job,
        "load_evidence_sources",
        lambda profile, path, text: [{"text": text}],
    )
    monkeypatch.setattr(
        single_job,
        "score_job",
        lambda text, job: {"score": 8, "keywords": "SQL", "reasoning": "match"},
    )

    single_job.score_exact_job_for_url(url, str(resume))
    verify = sqlite3.connect(tmp_path / "jobs.db")
    verify.row_factory = sqlite3.Row
    stored = verify.execute(
        "SELECT tailor_source_resume_path FROM jobs WHERE url=?", (url,)
    ).fetchone()

    assert Path(stored["tailor_source_resume_path"]) == resume.resolve()
    verify.close()


def test_run_tailoring_exact_url_never_selects_another_job(monkeypatch, tmp_path: Path) -> None:
    from applypilot.scoring import tailor

    conn = init_db(tmp_path / "jobs.db")
    for suffix in ("wanted", "other"):
        conn.execute(
            "INSERT INTO jobs (url, application_url, title, company_name, full_description, "
            "fit_score, eligibility_status) VALUES (?, ?, ?, 'Example', 'Description', 9, 'eligible')",
            (
                f"https://careers.example.test/{suffix}",
                f"https://ats.example.test/{suffix}",
                suffix,
            ),
        )
    conn.commit()
    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "load_profile", dict)
    seen: list[str] = []

    def fake_select(job: dict, profile: dict):
        seen.append(job["url"])
        raise RuntimeError("stop after selection")

    monkeypatch.setattr(tailor, "select_resume_source", fake_select)
    result = tailor.run_tailoring(
        min_score=0,
        limit=1,
        validation_mode="strict",
        target_url="https://ats.example.test/wanted",
    )

    assert seen == ["https://careers.example.test/wanted"]
    assert result["errors"] == 1


def test_rekey_email_job_preserves_applied_state_and_separates_tracking_url(
    monkeypatch, tmp_path: Path
) -> None:
    from applypilot import single_job

    conn = init_db(tmp_path / "jobs.db")
    source_url = "https://example.com/careers"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, strategy, "
        "application_url, eligibility_status, apply_status, applied_at) "
        "VALUES (?, 'AI Intern', 'Example AI', 'company_careers', 'company_careers', "
        "'exact_url', ?, 'eligible', 'applied', '2026-08-18T00:00:00+00:00')",
        (source_url, source_url),
    )
    conn.commit()
    monkeypatch.setattr(single_job, "get_connection", lambda: conn)

    result = single_job.rekey_email_job(
        source_url,
        "ai-engineer-intern-2026-07-07",
        "AI Engineer Intern",
        "Example AI Pte. Ltd.",
        "Build Python data pipelines and model evaluation workflows.",
    )

    assert result["tracking_url"] == (
        "https://example.com/careers#applypilot-ai-engineer-intern-2026-07-07"
    )
    assert result["application_url"] == source_url
    assert result["strategy"] == "candidate_provided_email_listing"
    assert result["apply_status"] == "applied"
    assert result["applied_at"] == "2026-08-18T00:00:00+00:00"
    row = conn.execute(
        "SELECT full_description, source_site FROM jobs WHERE url=?",
        (result["tracking_url"],),
    ).fetchone()
    assert row["full_description"].startswith("Build Python")
    assert row["source_site"] == "candidate_provided_email"
    assert conn.execute("SELECT 1 FROM jobs WHERE url=?", (source_url,)).fetchone() is None


def test_cover_not_required_requires_successful_exact_preview(
    monkeypatch, tmp_path: Path
) -> None:
    from applypilot import single_job

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO jobs (url, application_url, title, company_name, "
        "eligibility_status, apply_status) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "https://example.com/job",
            "https://ats.example.com/apply/job",
            "AI Intern",
            "Example",
            "eligible",
            "previewed",
        ),
    )
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, eligibility_status, apply_status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("https://example.com/unverified", "ML Intern", "Example", "eligible", None),
    )
    conn.commit()
    conn.close()

    def connect():
        test_conn = sqlite3.connect(db_path)
        test_conn.row_factory = sqlite3.Row
        return test_conn

    monkeypatch.setattr(single_job, "get_connection", connect)
    result = single_job.mark_cover_letter_not_required_for_url(
        "https://ats.example.com/apply/job",
        verified_by="real_browser_preview",
    )

    assert result["status"] == "not_required"
    assert result["url"] == "https://example.com/job"
    check_conn = connect()
    stored = check_conn.execute(
        "SELECT cover_letter_status, cover_letter_approved_by FROM jobs WHERE url=?",
        ("https://example.com/job",),
    ).fetchone()
    check_conn.close()
    assert tuple(stored) == ("not_required", "real_browser_preview")

    with pytest.raises(ValueError, match="only after a successful browser preview"):
        single_job.mark_cover_letter_not_required_for_url(
            "https://example.com/unverified"
        )


@pytest.mark.parametrize(
    "candidate",
    [
        "https://www.linkedin.com/signup/cold-join?source=jobs_registration",
        "https://www.linkedin.com/login?session_redirect=https%3A%2F%2Fexample.test",
        "https://sg.linkedin.com/authwall?trk=jobs",
        "https://www.linkedin.com/checkpoint/challenge/123",
    ],
)
def test_linkedin_auth_links_are_not_application_urls(candidate: str) -> None:
    assert detail.sanitize_application_url(
        "https://www.linkedin.com/jobs/view/123456",
        candidate,
    ) is None


def test_real_external_application_url_is_preserved() -> None:
    assert detail.sanitize_application_url(
        "https://www.linkedin.com/jobs/view/123456",
        "https://jobs.hp.com/job/123456",
    ) == "https://jobs.hp.com/job/123456"


def test_fenced_json_score_response_is_not_misclassified_as_zero() -> None:
    result = scorer._parse_score_response(
        '```json\n{"score": 8, "keywords": "Python, RAG", "reasoning": "Strong match"}\n```'
    )

    assert result == {"score": 8, "keywords": "Python, RAG", "reasoning": "Strong match"}


def test_score_job_disables_reasoning_for_short_structured_response(monkeypatch) -> None:
    captured = {}

    class FakeClient:
        last_response_meta: ClassVar[dict] = {}

        def chat(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return "SCORE: 8\nKEYWORDS: Python, RAG\nREASONING: Strong direct match."

    monkeypatch.setattr(scorer, "get_client", lambda: FakeClient())

    result = scorer.score_job(
        "Built Python and RAG systems.",
        {
            "title": "AI Intern",
            "company_name": "Example AI",
            "site": "linkedin",
            "location": "Singapore",
            "full_description": "Build Python and RAG systems.",
        },
    )

    assert result["score"] == 8
    assert captured["kwargs"]["thinking"] == {"type": "disabled"}


def test_apply_prompt_hides_secrets_and_isolates_worker_attachments(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _application_profile()
    resume_txt = tmp_path / "tailored.txt"
    resume_pdf = tmp_path / "tailored.pdf"
    letter_txt = tmp_path / "letter.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_pdf.write_bytes(b"%PDF-test")
    letter_txt.write_text(
        "Dear Hiring Manager,\n\nOne verified paragraph with enough detail for testing.\n\n"
        "A second verified paragraph with enough detail for testing.\n\n"
        "A third verified paragraph with enough detail for testing.\n\nSincerely,\n\nRyan Yu",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")
    monkeypatch.setenv("CAPSOLVER_API_KEY", "must-not-appear")
    job = {
        "url": "https://www.linkedin.com/jobs/view/123456",
        "application_url": "https://www.linkedin.com/jobs/view/123456",
        "title": "AI Automation Intern",
        "full_description": "Build LLM automation workflows.",
        "company_name": "Example Semiconductor",
        "source_site": "linkedin",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_path": str(letter_txt),
        "cover_letter_status": "human_approved",
        "_agent_reporting_enabled": True,
        "_control_contract": {
            "contract_version": 1,
            "interaction_driver": "playwright",
            "browser_runtime": "edge",
            "phase": "prepare",
            "reason_code": "primary_playwright",
            "single_writer": True,
            "submit_owner": "playwright",
            "requestable_handoffs": ["computer_use"],
            "handoff_requires_fresh_observation": True,
            "runtime_switch_after_submit_forbidden": True,
        },
    }

    built = prompt.build_prompt(
        job,
        "Verified resume",
        dry_run=True,
        worker_id=3,
        submission_phase="prepare",
    )
    real_prepare = prompt.build_prompt(
        job,
        "Verified resume",
        dry_run=False,
        worker_id=3,
        submission_phase="prepare",
    )

    assert "must-not-appear" not in built
    assert "CAPSOLVER_API_KEY" not in built
    assert "CONTROL_CONTRACT:" in built
    assert '"interaction_driver": "playwright"' in built
    assert "RESULT:FAILED:computer_use_handoff_required" in built
    assert "never reuse element refs, screenshot ids, coordinates" in built
    assert "RESULT:PREVIEWED" in built
    assert "click the visible filename or resume card once" in built
    assert "verify that the Selected marker moves" in built
    assert "even when FILES also contains a newly tailored PDF" in built
    assert "Never hand browser file selection to Windows Computer Use" in built
    assert "RESULT:FAILED:resume_upload for this job so the batch can continue" in built
    assert "Browser upload boundary" in built
    assert "must not stop unrelated jobs in the batch" in built
    assert "Required document preflight" in built
    assert "manual_review_required:required_document" in built
    assert "A filename or remove/replace control under Cover letter" in built
    assert "FAILURE_CONTEXT" in built
    assert "LinkedIn/SmartRecruiters city autocomplete" in built
    assert "launcher exclusively owns" in built
    assert "linkedin_launcher_entry_required" in built
    assert "current host is linkedin.com" not in built
    assert "launcher already performed the only authorized" in built
    assert "SmartRecruiters dual upload controls" not in built
    assert "Greenhouse/React Select location controls" not in built
    assert "manual_review_required:location_validation" not in built
    assert "one-character inputs is an identity-verification gate" in built
    assert "exact employer ATS mailbox OTP admitted" in built
    assert "an enabled Submit button or non-empty boxes alone is not a receipt" in built
    assert "Only a successful report_agent_turn call permits browser RESULT:READY_TO_SUBMIT" in built
    assert "at most one corrected report_agent_turn call" in built
    assert "RESULT:FAILED:answer_provenance_report_invalid" in built
    assert "prepared_for_audit and no answer_mappings" in real_prepare
    assert "RESULT:PREPARED_FOR_AUDIT" in real_prepare
    assert "RESULT:PREPARED_FOR_AUDIT" not in built
    assert "Video/audio upload contradiction" in built
    assert "RESULT:FAILED:unsafe_verification" in built
    assert "conditional questions can appear dynamically" not in built
    assert "Standard applicant truthfulness certifications" in built
    assert "are not a separate human-review boundary" in built
    assert "cover_not_required or cover_letter_required" in built
    assert "observations.resume_upload" in built
    assert "visible_filename" in built
    assert "legacy/open label such as `failed:stuck`" in built
    assert "omit the top-level typed `failure`" in built
    assert "`submit_started=true` requires status `submission_uncertain`" in built
    assert "Otherwise use `failed` or `failed:<failure.code>`" in built
    assert "`captcha_required` may use `captcha`" in built
    assert "`expired` may use `expired`" in built
    assert "Same page signature after one corrective attempt" in built
    assert (
        "Fill ALL fields in ONE browser_fill_form call, except Workday segmented/composite "
        "controlled dates" in built
    )
    assert "Workday segmented/composite dates" in built
    assert "never bulk-fill a segmented date or put a complete date into one segment" in built
    assert "If an accessible calendar/date picker is available, it is mandatory" in built
    assert "never use keyboard or per-segment typing" in built
    assert "Only when no accessible calendar/date picker exists" in built
    assert "verifying focus and the visible value before moving to the next segment" in built
    assert "stop immediately for manual review" in built
    assert "never retry, patch, guess, refill, or loop over the date" in built
    assert "A field labelled optional becomes conditionally required" in built
    assert "RESULT:APPLIED with a note that this was a dry run" not in built
    assert "launcher normally opens the exact job URL" in built
    assert "do not wait for the user to type it into the address bar" in built
    worker_attachment = tmp_path / "workers" / "worker-3" / "attachments" / "Taylor_Chen_Resume.pdf"
    assert worker_attachment.exists()
    assert not (tmp_path / "workers" / "current").exists()

    rebound_job = {
        **job,
        "application_url": "https://hp.wd5.myworkdayjobs.com/ExternalCareerSite/job/role",
        "_ats_adapter_context": {"adapter": "workday"},
    }
    rebound = prompt.build_prompt(
        rebound_job,
        "Verified resume",
        dry_run=True,
        worker_id=4,
    )
    assert "do not fill a field, sign in, upload a file" not in rebound
    assert "current host is linkedin.com" not in rebound
    assert "linkedin_launcher_entry_required" not in rebound


def test_apply_prompt_scopes_smartrecruiters_autocomplete_recovery(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _application_profile()
    resume_txt = tmp_path / "tailored.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_txt.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")
    base_job = {
        "url": "https://example.test/jobs/123",
        "title": "Business Analyst Intern",
        "full_description": "Support business and AI projects.",
        "company_name": "Example Employer",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
    }
    smartrecruiters_job = {
        **base_job,
        "application_url": (
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Example/"
            "publication/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        ),
        "_ats_adapter_context": {"adapter": "smartrecruiters"},
    }

    built = prompt.build_prompt(
        smartrecruiters_job,
        "Verified resume",
        dry_run=False,
        worker_id=7,
        submission_phase="prepare",
    )

    assert "Institution and School location/city autocomplete fields strictly serially" in built
    assert "Never include either autocomplete in a bulk browser_fill_form call" in built
    assert "take a fresh snapshot after typing each autocomplete value" in built
    assert "latest snapshot's exact option ref" in built
    assert "the invalid state is gone, the listbox is closed, and the selected value remains" in built
    assert "Do not use a manual-entry fallback when an exact Singapore option is visible" in built
    assert "Personal information City only" in built
    assert "Cannot find your city? Click here to fill in manually" in built
    assert "no selectable exact city/country option" in built
    assert "click that provider-owned fallback at most once" in built
    assert "the exact confirmed value persists" in built
    assert "its associated required or validation alert is gone" in built
    assert "Never use this personal-City fallback for Institution or Education" in built
    assert "one fresh-ref corrective retry per autocomplete field" in built
    assert "do not call browser_navigate, reload, reset, or reopen the application" in built
    assert "preserve the current page and output RESULT:FAILED:stuck" in built
    assert "visible uploaded filename or a Delete/replace control" in built
    conflicting_bulk_rule = (
        "Fill ALL fields in ONE browser_fill_form call, except Workday segmented/composite "
        "controlled dates"
    )
    assert conflicting_bulk_rule not in built
    assert "bulk-fill only ordinary non-autocomplete fields" in built
    assert "exclude Institution and School location/city" in built

    non_smartrecruiters = prompt.build_prompt(
        {
            **base_job,
            "application_url": "https://example.wd5.myworkdayjobs.com/job/123",
            "_ats_adapter_context": {"adapter": "workday"},
        },
        "Verified resume",
        dry_run=False,
        worker_id=8,
        submission_phase="prepare",
    )
    assert "Institution and School location/city autocomplete fields strictly serially" not in (
        non_smartrecruiters
    )
    assert "one fresh-ref corrective retry per autocomplete field" not in non_smartrecruiters
    assert "Personal information City only" not in non_smartrecruiters
    assert "Cannot find your city? Click here to fill in manually" not in non_smartrecruiters
    assert conflicting_bulk_rule in non_smartrecruiters
    assert "bulk-fill only ordinary non-autocomplete fields" not in non_smartrecruiters


def test_apply_prompt_scopes_one_time_validation_repair() -> None:
    section = prompt._build_browser_observation_section({
        "apply_status": "in_progress",
        "_browser_observation": {
            "repair_mode": True,
            "validation_errors": [{
                "label": "Portfolio URL (optional)",
                "message": "Please provide a valid URL",
                "field_type": "url",
                "repairable": True,
            }],
        },
    })

    assert "ONE-TIME POST-SUBMIT VALIDATION REPAIR" in section
    assert "no receipt was observed" in section
    assert "click the final control at most once" in section
    assert "camera, microphone" in section
    assert "required direct-impact" in section
    assert "unsupported-answer gates" not in section


def test_apply_prompt_resumes_manual_verification_without_exposing_codes() -> None:
    section = prompt._build_browser_observation_section({
        "_browser_observation": {"verification_resume": True},
    })

    assert "MANUAL VERIFICATION RESUME" in section
    assert "Do not read, retrieve, repeat, or log any verification code" in section
    assert "click the final control at most once" in section


def test_apply_prompt_rejects_unapproved_cover_letter(monkeypatch) -> None:
    monkeypatch.setattr(config, "load_profile", _application_profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    with pytest.raises(ValueError, match="human-approved"):
        prompt.build_prompt(
            {
                "title": "AI Intern",
                "tailored_resume_path": "unused.txt",
                "tailor_status": "machine_validated",
                "cover_letter_status": "machine_validated",
            },
            "Verified resume",
        )


def test_preview_prompt_allows_no_cover_and_pauses_for_visible_captcha(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _application_profile()
    resume_txt = tmp_path / "tailored.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_txt.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")
    job = {
        "url": "https://example.com/job",
        "title": "Data Analyst Intern",
        "company_name": "Example",
        "source_site": "lever",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_path": None,
        "cover_letter_status": None,
    }

    built = prompt.build_prompt(job, "Verified resume", dry_run=True)

    assert "RESULT:PREVIEWED" in built
    assert "PREVIEW_AUDIT" in built
    assert "submission_attempted must be false" in built
    assert "hidden/background CAPTCHA iframe is only a page signal" in built
    assert "Do not click, solve, inject tokens" in built
    assert "Output RESULT:CAPTCHA immediately" in built
    assert "If a CAPTCHA is found, solve it before continuing" not in built
    assert "CAPTCHA SOLVE" not in built
    assert 'Treat "Full name"' in built
    assert "Current location/city/country fields use Singapore" in built
    assert "2026-11-10" in built
    assert "2026-10-15" not in built
    assert "full-time credit-bearing availability begins January 2027" not in built
    assert "Phone field with country prefix: just type digits 90000000" in built
    assert "UNANSWERED_QUESTIONS: []" in built
    assert "Current Employment title from APPLICANT PROFILE" in built
    assert "target job title" in built
    assert "sufficiently adjacent same-domain" in built
    assert "justify YES" in built
    assert "Continue with Google" in built
    assert "already signed-in account" in built
    assert "mcp__credential_relay__fill_ats_credentials" in built
    assert "FULL-TIME salaried positions only" not in built
    assert "internships or full-time employment" in built
    assert 'aria-label is exactly or starts with "Easy Apply to this job"' not in built
    assert "Lever: select native comboboxes" in built
    assert "Do not click an \"Easy Apply\" job-type chip" not in built
    assert "openSDUIApplyFlow=true" not in built
    assert "goal is always the same: submit" not in built
    assert "send_email with subject" not in built
    assert "After submit:" not in built
    assert "RESULT:APPLIED" not in built
    assert "try sign up" not in built
    assert "no human-approved cover letter" in built


def test_captcha_helper_fails_closed_for_manual_review() -> None:
    guidance = prompt._build_captcha_section()

    assert "Do not click, solve, inject tokens" in guidance
    assert "Output RESULT:CAPTCHA immediately" in guidance
    assert "CapSolver" not in guidance
    assert "manual review" in guidance


def test_submission_prompt_blocks_uncertain_or_missing_resume_state(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _application_profile()
    resume_txt = tmp_path / "tailored.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_txt.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")
    job = {
        "url": "https://example.com/job",
        "title": "Data Analyst Intern",
        "company_name": "Example",
        "source_site": "lever",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_path": None,
        "cover_letter_status": "not_required",
        "apply_status": "submission_uncertain",
        "_browser_observation": {
            "signal": "pre_submit_audit:resume_not_uploaded",
            "status": "attention",
            "issues": ["resume_not_uploaded"],
        },
    }

    built = prompt.build_prompt(
        job,
        "Verified resume",
        dry_run=False,
        manual_captcha_relay=True,
        resume_existing_page=True,
    )

    assert "CAPTCHA CHECK AND VERIFY" in built
    assert "CAPTCHA SOLVE" not in built
    assert "Do not navigate or reload" in built
    assert "verified to have no cover-letter field" in built
    assert "RESULT:SUBMISSION_UNCERTAIN" in built
    assert "PRE-SUBMIT BROWSER GATE" in built
    assert "resume_not_uploaded" in built
    assert "hard submission pause" in built
    assert "Prior local application state: submission_uncertain" in built
    assert "must never trigger another submit click" in built
    assert "call browser_select_option with the selected visible option text" in built
    assert "Lever ordinary application form" in built
    assert "declare progress without visible state change" in built


def test_prepare_prompt_stops_for_advisory_observation_before_submit(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _application_profile()
    resume_txt = tmp_path / "tailored.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_txt.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")
    job = {
        "url": "https://example.com/job",
        "title": "Data Analyst Intern",
        "company_name": "Example",
        "source_site": "lever",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
    }

    built = prompt.build_prompt(
        job,
        "Verified resume",
        dry_run=False,
        manual_captcha_relay=True,
        submission_phase="prepare",
    )

    assert "RESULT:READY_TO_SUBMIT" in built
    assert "advisory observation" in built
    assert "send_email with subject" not in built
    assert "STOP before clicking the final submission control" in built
    assert "never click the upload control again" in built
    assert "Click the final submission control exactly once" not in built
    assert "RESULT:APPLIED only after" not in built


def test_submit_prompt_uses_bound_authorization_without_reconfirmation(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _application_profile()
    resume_txt = tmp_path / "tailored.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_txt.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")
    job = {
        "url": "https://example.com/job",
        "title": "Data Analyst Intern",
        "company_name": "Example",
        "source_site": "lever",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
    }

    built = prompt.build_prompt(
        job,
        "Verified resume",
        dry_run=False,
        manual_captcha_relay=True,
        submission_phase="submit",
    )

    assert "binding authorization to this exact job and submission materials" in built
    assert "do not ask the user for another confirmation" in built
    assert "visible CAPTCHA, assessment, missing resume after repair" in built
    assert "click the final submission control exactly once" in built
    assert "Otherwise output RESULT:SUBMISSION_UNCERTAIN" in built


def test_pre_submit_snapshot_enforces_identity_resume_and_hard_answers() -> None:
    profile = _application_profile()
    job = {
        "url": "https://jobs.lever.co/portcast/example",
        "application_url": "https://jobs.lever.co/portcast/example?lever-source=Indeed",
    }
    snapshot = {
        "url": "https://jobs.lever.co/portcast/example/apply",
        "required_unfilled": [],
        "resume_field_present": True,
        "resume_uploaded": True,
        "full_name_values": ["Taylor Chen"],
        "current_location_values": ["Singapore"],
        "select_fields": [
            {
                "text": "Where are you currently based, and do you have the legal right to work?",
                "selected": "Singapore",
            }
        ],
        "radio_questions": [
            {
                "text": "Are you available for a full-time internship starting September?",
                "selected": "No",
            },
            {
                "text": "Do you have prior internship in a product-based startup in logistics or B2B SaaS?",
                "selected": "No",
            },
        ],
        "submit_control_count": 1,
        "captcha_visible": True,
        "captcha_token_present": True,
    }

    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == [
        "visible_captcha"
    ]

    snapshot["resume_uploaded"] = False
    snapshot["full_name_values"] = ["Ryan Yu"]
    snapshot["current_location_values"] = [""]
    snapshot["radio_questions"][0]["selected"] = "Yes"
    issues = launcher._validate_pre_submit_snapshot(snapshot, profile, job)

    assert "resume_not_uploaded" in issues
    assert "legal_name_mismatch" in issues
    assert "current_location_not_singapore" in issues


def test_pre_submit_snapshot_ignores_optional_blank_answers_and_accepts_location_aliases() -> None:
    profile = _application_profile()
    job = {"url": "https://jobs.example.test/intern/apply"}
    snapshot = {
        "url": job["url"],
        "current_location_fields": [
            {"text": "Current location", "value": "", "required": False},
            {"text": "Current city", "value": "SG", "required": True},
        ],
        "radio_questions": [{
            "text": "Are you available for a full-time internship starting September?",
            "selected": "",
            "required": False,
        }],
        "select_fields": [{
            "text": "Where are you currently based, and do you have the legal right to work?",
            "selected": "Singapore, Singapore",
            "required": True,
        }],
        "submit_control_count": 1,
    }

    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []
    mappings = page_observation._collect_lossy_answer_mappings(snapshot, profile, job)
    assert any(item["field_semantic"] == "current_location" for item in mappings)


def test_pre_submit_snapshot_validates_reusable_legal_answers() -> None:
    profile = _application_profile()
    profile["application_facts"].extend(
        [
            {"key": "united_states_person_status", "value": "No"},
            {"key": "f1_student_status", "value": "No"},
        ]
    )
    job = {
        "url": "https://jobs.ashbyhq.com/simular/example",
        "application_url": "https://jobs.ashbyhq.com/simular/example/application",
        "title": "Data Analyst Intern",
        "company_name": "Simular",
        "full_description": "Full-time internship in Singapore.",
        "application_readiness_reason": "Confirmed full-time internship.",
    }
    snapshot = {
        "url": job["application_url"],
        "required_unfilled": [],
        "sensitive_required_unknown": [],
        "resume_field_present": True,
        "resume_uploaded": True,
        "full_name_values": ["Taylor Chen"],
        "email_values": ["applicant@example.com.extra"],
        "current_location_values": ["Singapore"],
        "select_fields": [],
        "text_fields": [],
        "radio_questions": [
            {"text": "Are you a U.S. Person?", "selected": "Yes"},
            {"text": "Are you currently in F-1, CPT, or OPT status?", "selected": "Yes"},
            {"text": "Will you require visa sponsorship?", "selected": "Yes"},
            {"text": "Are you legally authorized to work?", "selected": "No"},
            {"text": "Have you ever worked for Simular?", "selected": "Yes"},
            {"text": "Are you subject to contractual or non-compete restrictions?", "selected": "Yes"},
            {"text": "Do you have criminal convictions?", "selected": "Yes"},
            {"text": "Are you willing to complete a background check?", "selected": "No"},
        ],
        "submit_control_count": 1,
        "assessment_visible": False,
        "captcha_visible": False,
    }

    issues = launcher._validate_pre_submit_snapshot(snapshot, profile, job)

    assert {
        "email_mismatch",
        "hard_answer_mismatch:united_states_person_status",
        "hard_answer_mismatch:f1_student_status",
        "hard_answer_mismatch:requires_sponsorship",
        "hard_answer_mismatch:legally_authorized_to_work",
        "hard_answer_mismatch:previously_worked_for_target_employer",
        "hard_answer_mismatch:employment_or_non_compete_restrictions",
        "hard_answer_mismatch:criminal_convictions_to_disclose",
        "hard_answer_mismatch:background_check",
    } <= set(issues)

    snapshot["email_values"] = [profile["personal"]["email"]]
    for question in snapshot["radio_questions"]:
        question["selected"] = (
            "Yes"
            if "legally authorized" in question["text"].casefold()
            or "background check" in question["text"].casefold()
            else "No"
        )

    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []


def test_pre_submit_snapshot_accepts_closest_non_contradictory_visa_category() -> None:
    profile = _application_profile()
    job = {
        "url": "https://jobs.example.test/intern",
        "title": "Data Analyst Intern",
        "company_name": "Example",
        "full_description": "Credit-bearing full-time internship in Singapore.",
        "application_readiness_reason": "Confirmed programme-credit-bearing internship.",
    }
    snapshot = {
        "url": job["url"],
        "required_unfilled": [],
        "sensitive_required_unknown": [],
        "select_fields": [{
            "text": "Citizenship/Visa Status",
            "selected": "Possess relevant work visa",
        }],
        "radio_questions": [],
        "submit_control_count": 1,
        "assessment_visible": False,
        "captcha_visible": False,
    }

    issues = launcher._validate_pre_submit_snapshot(snapshot, profile, job)

    assert issues == []

    profile["application_facts"].append({
        "key": "citizenship_visa_status_option",
        "value": "Possess relevant work visa",
    })
    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []


def test_pre_submit_snapshot_accepts_same_level_degree_taxonomy_but_not_another_level() -> None:
    profile = _application_profile()
    profile["education"] = [{
        "institution": "Nanyang Technological University",
        "degree": "Master of Computing in Applied Artificial Intelligence",
    }]
    job = {
        "url": "https://example.wd3.myworkdayjobs.com/job/intern/apply",
        "title": "AI Intern",
        "company_name": "Example",
    }
    snapshot = {
        "url": job["url"],
        "required_unfilled": [],
        "sensitive_required_unknown": [],
        "select_fields": [],
        "radio_questions": [],
        "education_entries": [{
            "institution": "Nanyang Technological University",
            "degree": "Masters of Science",
        }],
        "submit_control_count": 1,
        "assessment_visible": False,
        "captcha_visible": False,
    }

    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []

    snapshot["education_entries"][0]["degree"] = "Bachelor of Science"
    assert "education_degree_mismatch" in launcher._validate_pre_submit_snapshot(
        snapshot, profile, job
    )

    snapshot["education_entries"][0]["degree"] = "Master's Degree"
    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []

    snapshot["education_entries"][0]["degree"] = "Masters of Science"
    profile["application_facts"].append({
        "key": "education_degree_option",
        "context": "Nanyang Technological University",
        "value": "Masters of Science",
    })
    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []


def test_pre_submit_snapshot_blocks_unverifiable_or_partial_education_state() -> None:
    profile = _application_profile()
    profile["education"] = [
        {
            "institution": "Nanyang Technological University",
            "degree": "Master of Computing in Applied Artificial Intelligence",
        },
        {
            "institution": "University of Example",
            "degree": "Bachelor of Engineering",
        },
    ]
    job = {
        "url": "https://example.wd3.myworkdayjobs.com/job/intern/apply",
        "title": "AI Intern",
        "company_name": "Example",
    }
    snapshot = {
        "url": job["url"],
        "required_unfilled": [],
        "sensitive_required_unknown": [],
        "select_fields": [],
        "radio_questions": [],
        "education_field_present": True,
        "education_record_count": 1,
        "education_entries": [],
        "submit_control_count": 1,
        "assessment_visible": False,
        "captcha_visible": False,
    }

    assert "education_state_unconfirmed" in launcher._validate_pre_submit_snapshot(
        snapshot, profile, job
    )

    snapshot["education_record_count"] = 2
    snapshot["education_entries"] = [{
        "institution": "Nanyang Technological University",
        "degree": "Master's Degree",
    }]
    assert "education_state_unconfirmed" in launcher._validate_pre_submit_snapshot(
        snapshot, profile, job
    )

    snapshot["education_record_count"] = 1
    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []


def test_pre_submit_snapshot_validates_generic_highest_education_level() -> None:
    profile = _application_profile()
    profile["education"] = [
        {
            "institution": "Nanyang Technological University",
            "degree": "Master of Computing in Applied Artificial Intelligence",
        },
        {
            "institution": "University of Example",
            "degree": "Bachelor of Engineering",
        },
    ]
    job = {
        "url": "https://jobs.example.test/intern/apply",
        "title": "AI Intern",
        "company_name": "Example",
    }
    snapshot = {
        "url": job["url"],
        "required_unfilled": [],
        "sensitive_required_unknown": [],
        "select_fields": [{
            "text": "Highest Education Level",
            "selected": "Master's Degree",
        }],
        "radio_questions": [],
        "education_field_present": False,
        "education_record_count": 0,
        "education_entries": [],
        "submit_control_count": 1,
        "assessment_visible": False,
        "captcha_visible": False,
    }

    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []

    snapshot["select_fields"][0]["selected"] = "Bachelor's Degree"
    assert "education_level_mismatch" in launcher._validate_pre_submit_snapshot(
        snapshot, profile, job
    )

    snapshot["select_fields"][0]["selected"] = "Master of Science"
    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []
    mappings = page_observation._collect_lossy_answer_mappings(snapshot, profile, job)
    assert any(item["field_semantic"] == "highest_education_level" for item in mappings)

    profile["education"][0]["status"] = "Currently enrolled"
    profile["education"][1]["graduation"] = "May 2024"
    snapshot["select_fields"][0]["selected"] = "Bachelor's Degree"
    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []

    profile["application_facts"].append({
        "key": "highest_education_level_option",
        "value": "Postgraduate Degree",
    })
    snapshot["select_fields"][0]["selected"] = "Postgraduate Degree"
    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []

    snapshot["select_fields"][0]["selected"] = "Bachelor's Degree"
    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []


def test_built_prompt_does_not_reintroduce_location_or_unsupported_answer_stops(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _application_profile()
    resume_txt = tmp_path / "tailored.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_txt.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")
    job = {
        "url": "https://jobs.example.test/intern/apply",
        "title": "AI Intern",
        "company_name": "Example",
        "source_site": "generic",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
    }

    built = prompt.build_prompt(job, "Verified resume", dry_run=True)

    assert "If not eligible, output RESULT and stop" not in built
    assert "unsupported-answer gates" not in built
    assert "Only an explicit do_not_apply" in built


def test_email_route_prompt_requires_bound_prepare_and_sent_evidence() -> None:
    prepare = prompt._build_email_route_section(
        {
            "_available_tools": [
                "mailbox_search",
                "mailbox_get_message",
            ]
        },
        dry_run=False,
        submission_phase="prepare",
    )
    submit = prompt._build_email_route_section(
        {
            "_available_tools": [
                "mailbox_search",
                "mailbox_get_message",
                "direct_email_send",
            ],
            "_browser_observation": {
                "email_application": {"route": "direct_email"}
            },
        },
        dry_run=False,
        submission_phase="submit",
    )

    assert "recipient_source=official_listing" in prepare
    assert "email_route_capability_missing" not in prepare
    assert "direct_email_send" not in prepare
    assert "non-empty listing_evidence" in prepare
    assert "body_sha256" in prepare
    assert "duplicate_check={folder:'sent',completed:true,duplicate_found:false,provider_query_id:<nonempty>}" in prepare
    assert "Do not report attachment paths or digests" in prepare
    assert "only in submit phase after launcher reservation" in submit
    assert "folder=sent" in submit
    assert "provider_message_id" in submit
    assert "body_sha256" in submit


def test_email_route_prepare_prompt_makes_mailbox_the_primary_workflow(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _application_profile()
    resume_txt = tmp_path / "tailored.txt"
    resume_txt.write_text("Verified resume", encoding="utf-8")
    resume_txt.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    monkeypatch.setattr(config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")
    job = {
        "url": "https://www.internsg.com/job/data-ai-intern/",
        "title": "Data Engineering and AI Intern",
        "company_name": "Example",
        "source_site": "InternSG",
        "full_description": "Apply by emailing a text CV to jobs@example.test.",
        "tailored_resume_path": str(resume_txt),
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
        "_available_tools": ["mailbox_search", "mailbox_get_message"],
        "_agent_reporting_enabled": True,
    }

    built = prompt.build_prompt(
        job,
        "Verified resume",
        dry_run=False,
        submission_phase="prepare",
    )

    assert "Follow EMAIL-ONLY APPLICATION ROUTE as the primary workflow" in built
    assert "Do not look for, fill, or audit a browser application form" in built
    assert "Call report_agent_turn exactly once" in built
    assert "Do not call any send tool" in built
    assert "== MAILBOX-ONLY CONTROL ==" in built
    assert "REQUIRED BROWSER CONTROL" not in built
    assert "browser_mcp_unavailable" not in built


def test_email_route_submit_keeps_send_capability_gate() -> None:
    submit = prompt._build_email_route_section(
        {
            "_available_tools": ["mailbox_search", "mailbox_get_message"],
            "_browser_observation": {
                "email_application": {"route": "direct_email"}
            },
        },
        dry_run=False,
        submission_phase="submit",
    )

    assert "RESULT:FAILED:email_route_capability_missing" in submit
    assert "missing_capability=direct_email_send" in submit
    assert "Call the authorized direct-email send tool exactly once" not in submit

@pytest.mark.parametrize(
    ("changes", "expected_signal"),
    [
        ({"captcha_visible": True, "captcha_token_present": False}, "visible_captcha"),
        ({"submit_control_count": 0}, "submit_control_missing"),
        ({"url": "https://unexpected.example/application"}, "unexpected_application_url"),
    ],
)
def test_browser_snapshot_states_are_reported_as_agent_attention_signals(
    changes: dict,
    expected_signal: str,
) -> None:
    profile = _application_profile()
    job = {"url": "https://jobs.lever.co/example/role"}
    snapshot = {
        "url": "https://jobs.lever.co/example/role/apply",
        "required_unfilled": [],
        "resume_field_present": True,
        "resume_uploaded": True,
        "full_name_values": ["Taylor Chen"],
        "current_location_values": ["Singapore"],
        "select_fields": [],
        "radio_questions": [],
        "submit_control_count": 1,
        "captcha_visible": False,
        "captcha_token_present": False,
    }
    snapshot.update(changes)

    issues = launcher._validate_pre_submit_snapshot(snapshot, profile, job)

    assert expected_signal in issues


def test_workday_review_url_is_bound_by_tenant_and_review_state() -> None:
    profile = _application_profile()
    job = {
        "url": (
            "https://tenant.wd103.myworkdayjobs.com/en-US/careers/job/"
            "Singapore/AI-Intern_JR123"
        )
    }
    snapshot = {
        "url": "https://tenant.wd103.myworkdayjobs.com/en-US/careers/application/review",
        "job_reference_urls": [job["url"]],
        "required_unfilled": [],
        "resume_field_present": True,
        "resume_uploaded": True,
        "full_name_values": ["Taylor Chen"],
        "current_location_values": ["Singapore"],
        "select_fields": [],
        "radio_questions": [],
        "submit_control_count": 1,
        "captcha_visible": False,
        "workday_observation": {
            "page_kind": "review",
            "has_submit": True,
            "has_manual_gate": False,
        },
    }

    issues = launcher._validate_pre_submit_snapshot(snapshot, profile, job)

    assert "unexpected_application_url" not in issues


def test_workday_review_url_rejects_another_job_on_the_same_tenant() -> None:
    profile = _application_profile()
    job = {
        "url": (
            "https://tenant.wd103.myworkdayjobs.com/en-US/careers/job/"
            "Singapore/AI-Intern_JR123"
        )
    }
    snapshot = {
        "url": "https://tenant.wd103.myworkdayjobs.com/en-US/careers/application/review",
        "job_reference_urls": [
            (
                "https://tenant.wd103.myworkdayjobs.com/en-US/careers/job/"
                "Singapore/Other-Intern_JR999"
            )
        ],
        "required_unfilled": [],
        "resume_field_present": True,
        "resume_uploaded": True,
        "full_name_values": ["Taylor Chen"],
        "current_location_values": ["Singapore"],
        "select_fields": [],
        "radio_questions": [],
        "submit_control_count": 1,
        "captcha_visible": False,
        "workday_observation": {
            "page_kind": "review",
            "has_submit": True,
            "has_manual_gate": False,
        },
    }

    issues = launcher._validate_pre_submit_snapshot(snapshot, profile, job)

    assert "unexpected_application_url" in issues


def test_bound_job_path_normalizes_percent_encoded_punctuation() -> None:
    profile = _application_profile()
    job = {
        "url": (
            "https://tenant.wd103.myworkdayjobs.com/en-US/careers/job/"
            "Singapore-Singapore/AI-Intern_JR123"
        )
    }
    snapshot = {
        "url": (
            "https://tenant.wd103.myworkdayjobs.com/en-US/careers/job/"
            "Singapore%2C-Singapore/AI-Intern_JR123/apply"
        ),
        "required_unfilled": [],
        "resume_field_present": False,
        "resume_uploaded": False,
        "full_name_values": ["Taylor Chen"],
        "current_location_values": ["Singapore"],
        "select_fields": [],
        "radio_questions": [],
        "submit_control_count": 1,
        "captcha_visible": False,
        "workday_observation": {
            "page_kind": "form",
            "has_submit": True,
            "has_manual_gate": False,
        },
    }

    issues = launcher._validate_pre_submit_snapshot(snapshot, profile, job)

    assert "unexpected_application_url" not in issues


@pytest.mark.parametrize(
    ("actual_url", "expected_issue"),
    [
        (
            (
                "https://jobs.smartrecruiters.com/oneclick-ui/company/Grab/publication/"
                "4df5dd16-4fc7-48b4-a943-492fbc508b62?dcr_ci=Grab"
            ),
            False,
        ),
        (
            (
                "https://jobs.smartrecruiters.com/oneclick-ui/company/Other/publication/"
                "4df5dd16-4fc7-48b4-a943-492fbc508b62?dcr_ci=Other"
            ),
            True,
        ),
        (
            (
                "https://jobs.smartrecruiters.com/oneclick-ui/company/Grab/publication/"
                "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee?dcr_ci=Grab"
            ),
            True,
        ),
    ],
)
def test_smartrecruiters_oneclick_url_is_bound_to_same_tenant_and_ready_form(
    actual_url: str,
    expected_issue: bool,
) -> None:
    profile = _application_profile()
    job = {
        "url": (
            "https://jobs.smartrecruiters.com/Grab/"
            "744000145885499-intern-strategy-insights"
        ),
        "_ats_application_binding": {
            "provider": "smartrecruiters",
            "tenant": "Grab",
            "posting_id": "744000145885499",
            "publication_id": "4df5dd16-4fc7-48b4-a943-492fbc508b62",
            "resolved": True,
        },
    }
    snapshot = {
        "url": actual_url,
        "required_unfilled": [],
        "resume_field_present": True,
        "resume_uploaded": True,
        "full_name_values": ["Taylor Chen"],
        "current_location_values": ["Singapore"],
        "select_fields": [],
        "radio_questions": [],
        "submit_control_count": 1,
        "captcha_visible": False,
    }

    issues = launcher._validate_pre_submit_snapshot(snapshot, profile, job)

    assert ("unexpected_application_url" in issues) is expected_issue


def test_smartrecruiters_route_identity_is_separate_from_form_readiness() -> None:
    expected_url = (
        "https://jobs.smartrecruiters.com/Grab/744000145885499-role"
    )
    actual_url = (
        "https://jobs.smartrecruiters.com/oneclick-ui/company/Grab/publication/"
        "4df5dd16-4fc7-48b4-a943-492fbc508b62/screening?dcr_ci=Grab"
    )
    binding = {
        "provider": "smartrecruiters",
        "tenant": "Grab",
        "posting_id": "744000145885499",
        "publication_id": "4df5dd16-4fc7-48b4-a943-492fbc508b62",
        "resolved": True,
    }
    snapshot = {
        "url": actual_url,
        "required_unfilled": [],
        "resume_field_present": True,
        "resume_uploaded": True,
        "full_name_values": ["Taylor Chen"],
        "current_location_values": ["Singapore"],
        "select_fields": [],
        "radio_questions": [],
        "submit_control_count": 0,
        "captcha_visible": False,
    }

    assert page_observation._same_bound_application_flow(
        expected_url,
        actual_url,
        snapshot,
        binding,
    )
    issues = launcher._validate_pre_submit_snapshot(
        snapshot,
        _application_profile(),
        {"url": expected_url, "_ats_application_binding": binding},
    )
    assert "unexpected_application_url" not in issues
    assert "submit_control_missing" in issues


def test_smartrecruiters_final_page_uses_same_turn_resume_upload_proof() -> None:
    profile = _application_profile()
    job = {
        "url": "https://jobs.smartrecruiters.com/Grab/744000145885499-role",
        "_ats_application_binding": {
            "provider": "smartrecruiters",
            "tenant": "Grab",
            "posting_id": "744000145885499",
            "publication_id": "4df5dd16-4fc7-48b4-a943-492fbc508b62",
            "resolved": True,
        },
        "_agent_observations": {
            "resume_upload": {
                "verified": True,
                "field_label": "Resume *",
                "visible_filename": "candidate-resume.pdf",
            }
        },
    }
    snapshot = {
        "url": (
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Grab/publication/"
            "4df5dd16-4fc7-48b4-a943-492fbc508b62/screening?dcr_ci=Grab"
        ),
        "required_unfilled": [],
        "resume_field_present": False,
        "resume_uploaded": False,
        "full_name_values": ["Taylor Chen"],
        "current_location_values": ["Singapore"],
        "select_fields": [],
        "radio_questions": [],
        "submit_control_count": 1,
        "captcha_visible": False,
    }

    issues = launcher._validate_pre_submit_snapshot(snapshot, profile, job)

    assert "unexpected_application_url" not in issues
    assert "resume_state_unconfirmed" not in issues


@pytest.mark.parametrize(
    ("actual_url", "expected_match"),
    [
        (
            (
                "https://jobs.smartrecruiters.com/oneclick-ui/company/NCS3/publication/"
                "9a04b9a9-424f-4afc-a4c9-5eedc493b048/screening?dcr_ci=NCS3"
            ),
            True,
        ),
        (
            (
                "https://jobs.smartrecruiters.com/oneclick-ui/company/Other/publication/"
                "9a04b9a9-424f-4afc-a4c9-5eedc493b048/screening?dcr_ci=Other"
            ),
            False,
        ),
        (
            (
                "https://jobs.smartrecruiters.com/oneclick-ui/company/NCS3/publication/"
                "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee/screening?dcr_ci=NCS3"
            ),
            False,
        ),
        (
            (
                "https://jobs.smartrecruiters.com/oneclick-ui/company/NCS3/publication/"
                "9a04b9a9-424f-4afc-a4c9-5eedc493b048/screening?dcr_ci=Other"
            ),
            False,
        ),
        (
            (
                "https://jobs.smartrecruiters.com/oneclick-ui/company/NCS3/publication/"
                "9a04b9a9-424f-4afc-a4c9-5eedc493b048/review?dcr_ci=NCS3"
            ),
            False,
        ),
        (
            (
                "https://jobs.smartrecruiters.com/oneclick-ui/company/NCS3/publication/"
                "9a04b9a9-424f-4afc-a4c9-5eedc493b048/screening/deep?dcr_ci=NCS3"
            ),
            False,
        ),
    ],
)
def test_smartrecruiters_oneclick_root_only_allows_bound_application_subroutes(
    actual_url: str,
    expected_match: bool,
) -> None:
    expected_url = (
        "https://jobs.smartrecruiters.com/oneclick-ui/company/NCS3/publication/"
        "9a04b9a9-424f-4afc-a4c9-5eedc493b048?dcr_ci=NCS3"
    )

    assert page_observation._same_bound_application_flow(
        expected_url,
        actual_url,
        {},
    ) is expected_match


def test_pre_submit_snapshot_only_combines_main_and_selected_application_surface() -> None:
    class Surface:
        def __init__(self, url: str, **signals: bool) -> None:
            self.url = url
            self.signals = signals
            self.evaluate_calls = 0

        def evaluate(self, script: str) -> dict:
            assert script == page_observation._APPLICATION_SURFACE_SIGNALS
            self.evaluate_calls += 1
            return {
                "receipt": False,
                "final_submit": False,
                "review": False,
                "dialog": False,
                "form_controls": 1,
                "text_length": 100,
                "captcha_visible": False,
                "assessment_visible": False,
                "verification_visible": False,
                **self.signals,
            }

    application_root = (
        "https://jobs.smartrecruiters.com/oneclick-ui/company/NCS3/publication/"
        "9a04b9a9-424f-4afc-a4c9-5eedc493b048"
    )
    main = Surface(application_root, final_submit=True, form_controls=1)
    application_child = Surface(
        f"{application_root}/screening",
        form_controls=9,
    )
    unrelated_sibling = Surface(
        "https://support.example.test/subscription",
        final_submit=True,
        form_controls=20,
        captcha_visible=True,
        assessment_visible=True,
        verification_visible=True,
    )
    selected_page = SimpleNamespace(
        url=application_root,
        main_frame=main,
        frames=[main, application_child, unrelated_sibling],
    )
    snapshot = {
        "resume_field_present": False,
        "submit_control_count": 0,
        "captcha_visible": False,
        "assessment_visible": False,
        "verification_visible": False,
    }

    selected_surface, _score = page_observation._score_application_page(selected_page)
    page_observation._merge_same_page_submit_evidence(
        snapshot,
        selected_page,
        selected_surface,
    )

    assert selected_surface is application_child
    assert snapshot["submit_control_count"] == 1
    assert snapshot["captcha_visible"] is False
    assert snapshot["assessment_visible"] is False
    assert snapshot["verification_visible"] is False
    assert unrelated_sibling.evaluate_calls == 0
    issues = launcher._validate_pre_submit_snapshot(
        snapshot,
        _application_profile(),
        {
            "_agent_observations": {
                "resume_upload": {
                    "verified": True,
                    "field_label": "Resume *",
                    "visible_filename": "candidate-resume.pdf",
                }
            }
        },
    )
    assert "resume_state_unconfirmed" not in issues
    assert "submit_control_missing" not in issues


@pytest.mark.parametrize(
    ("gate_surface", "gate_signal", "expected_issue"),
    [
        ("main", "captcha_visible", "visible_captcha"),
        ("selected", "assessment_visible", "assessment_present"),
        ("selected", "verification_visible", "verification_required"),
    ],
)
def test_pre_submit_snapshot_propagates_manual_gates_from_allowed_surfaces(
    gate_surface: str,
    gate_signal: str,
    expected_issue: str,
) -> None:
    class Surface:
        def __init__(self, name: str, url: str) -> None:
            self.name = name
            self.url = url

        def evaluate(self, script: str) -> dict:
            assert script == page_observation._APPLICATION_SURFACE_SIGNALS
            return {
                "receipt": False,
                "final_submit": self.name == "main",
                "review": False,
                "dialog": False,
                "form_controls": 1,
                "text_length": 100,
                "captcha_visible": (
                    self.name == gate_surface and gate_signal == "captcha_visible"
                ),
                "assessment_visible": (
                    self.name == gate_surface and gate_signal == "assessment_visible"
                ),
                "verification_visible": (
                    self.name == gate_surface and gate_signal == "verification_visible"
                ),
            }

    application_root = "https://jobs.smartrecruiters.com/oneclick-ui/company/NCS3"
    main = Surface("main", application_root)
    selected = Surface("selected", f"{application_root}/screening")
    page = SimpleNamespace(
        url=application_root,
        main_frame=main,
        frames=[main, selected],
    )
    snapshot = {
        "resume_field_present": True,
        "resume_uploaded": True,
        "submit_control_count": 0,
        "captcha_visible": False,
        "assessment_visible": False,
        "verification_visible": False,
    }

    page_observation._merge_same_page_submit_evidence(snapshot, page, selected)
    issues = launcher._validate_pre_submit_snapshot(
        snapshot,
        _application_profile(),
        {},
    )

    assert expected_issue in issues


def test_manual_captcha_response_token_overrides_stale_visible_iframe() -> None:
    class ResponseField:
        def __init__(self, value: str) -> None:
            self.value = value

        def input_value(self, timeout: int) -> str:
            assert timeout == 500
            return self.value

    class Locator:
        def __init__(self, fields: list[ResponseField]) -> None:
            self.fields = fields

        def all(self) -> list[ResponseField]:
            return self.fields

    class Page:
        def __init__(self, value: str) -> None:
            self.value = value

        def locator(self, selector: str) -> Locator:
            assert 'name*="captcha"' in selector
            return Locator([ResponseField(self.value)])

    assert launcher._captcha_response_present(Page("verified-token")) is True
    assert launcher._captcha_response_present(Page("")) is False


def test_legacy_retry_sentinel_migrates_to_explicit_retry_state(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, apply_status TEXT, "
        "apply_error TEXT, apply_attempts INTEGER DEFAULT 0)"
    )
    legacy.execute(
        "INSERT INTO jobs VALUES ('https://example.com/applied', 'applied', NULL, 101)"
    )
    legacy.execute(
        "INSERT INTO jobs VALUES ('https://example.com/blocked', 'failed', 'expired', 99)"
    )
    legacy.commit()
    legacy.close()

    conn = init_db(db_path)
    applied = conn.execute(
        "SELECT apply_attempts, apply_retry_blocked, apply_retry_reason FROM jobs "
        "WHERE url='https://example.com/applied'"
    ).fetchone()
    blocked = conn.execute(
        "SELECT apply_attempts, apply_retry_blocked, apply_retry_reason FROM jobs "
        "WHERE url='https://example.com/blocked'"
    ).fetchone()

    assert tuple(applied) == (2, 0, None)
    assert tuple(blocked) == (0, 1, "expired")


def test_permanent_failure_uses_retry_flag_without_corrupting_attempt_count(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/permanent"
    conn.execute("INSERT INTO jobs (url) VALUES (?)", (url,))
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    launcher.mark_result(url, "failed", "expired", permanent=True)
    row = conn.execute(
        "SELECT apply_attempts, apply_retry_blocked, apply_retry_reason FROM jobs WHERE url=?",
        (url,),
    ).fetchone()

    assert tuple(row) == (1, 1, "expired")


def test_worker_treats_browser_observation_as_hard_submission_gate(monkeypatch) -> None:
    job = {
        "url": "https://example.com/job",
        "title": "Data Intern",
        "company_name": "Example",
    }
    calls: list[tuple[dict, dict]] = []
    marked: list[tuple] = []

    def fake_run_job(*args, **kwargs):
        calls.append((args[0], kwargs))
        if kwargs["submission_phase"] == "prepare":
            return "ready_to_submit", 100
        return "applied", 50

    launcher._stop_event.clear()
    monkeypatch.setattr(config, "load_profile", _application_profile)
    monkeypatch.setattr(launcher, "acquire_job", lambda **kwargs: job)
    monkeypatch.setattr(launcher, "launch_chrome", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        launcher,
        "_open_bound_application_target",
        lambda _port, _url: {"application-root"},
    )
    monkeypatch.setattr(
        launcher,
        "_close_bound_application_targets",
        lambda _port, _targets: None,
    )
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "run_job", fake_run_job)
    monkeypatch.setattr(
        launcher,
        "_audit_live_pre_submit_page",
        lambda *args: (
            "pre_submit_audit:resume_not_uploaded",
            {"status": "attention", "issues": ["resume_not_uploaded"]},
        ),
    )
    monkeypatch.setattr(launcher, "mark_result", lambda *args, **kwargs: marked.append(args))
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)

    result = launcher.worker_loop(
        worker_id=0,
        limit=1,
        target_url=job["url"],
        dry_run=False,
        manual_captcha_relay=True,
    )

    assert result == (0, 1)
    assert [kwargs["submission_phase"] for _, kwargs in calls] == ["prepare"]
    assert marked[0][:2] == (job["url"], "failed")
    assert marked[0][2].startswith(
        "manual_review_required:pre_submit_audit:resume_not_uploaded; "
        "category=resume_upload_failed; recoverability=retry_same_application"
    )


def test_exact_submission_acquisition_accepts_verified_no_cover(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/no-cover"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, application_url, full_description, "
        "tailored_resume_path, tailor_status, cover_letter_status, eligibility_status, fit_score) "
        "VALUES (?, 'Data Intern', 'Example', 'lever', 'lever', ?, 'Verified JD', 'resume.txt', "
        "'machine_validated', 'not_required', 'eligible', 8)",
        (url, url),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    acquired = launcher.acquire_job(target_url=url, preview_only=False)

    assert acquired is not None
    assert acquired["cover_letter_status"] == "not_required"
    acquisition = acquired["_acquisition_performance"]
    assert acquisition["version"] == 1
    assert acquisition["candidate_rows"] == 1
    assert acquisition["admission_rows_scanned"] == 1
    assert all(
        acquisition[key] >= 0
        for key in (
            "stale_recovery_ms",
            "eligibility_refresh_ms",
            "transaction_wait_ms",
            "candidate_fetch_ms",
            "admission_scan_ms",
            "total_ms",
        )
    )
    empty_acquisition: dict[str, object] = {}
    assert launcher.acquire_job(
        target_url="https://example.com/missing",
        preview_only=False,
        performance_sink=empty_acquisition,
    ) is None
    assert empty_acquisition["outcome"] == "empty"
    assert empty_acquisition["candidate_rows"] == 0
    assert empty_acquisition["admission_rows_scanned"] == 0
    assert empty_acquisition["total_ms"] >= 0


def test_application_acquisition_retires_resume_with_stale_profile_gpa(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    resume = tmp_path / "stale-resume.txt"
    resume.write_text(
        "University of Pennsylvania, Master of City Planning, GPA: 3.6",
        encoding="utf-8",
    )
    resume_pdf = resume.with_suffix(".pdf")
    resume_pdf.write_bytes(b"%PDF-stale-gpa-sidecar-binding")
    url = "https://example.com/stale-gpa"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, application_url, "
        "full_description, tailored_resume_path, tailor_status, cover_letter_status, "
        "eligibility_status, fit_score) VALUES (?, 'Data Intern', 'Example', 'lever', "
        "'lever', ?, 'Verified JD', ?, 'machine_validated', 'not_required', 'eligible', 8)",
        (url, url, str(resume_pdf)),
    )
    conn.commit()
    profile = _application_profile()
    profile["education"] = [
        {
            "institution": "University of Pennsylvania",
            "gpa": "3.46/4.0",
            "gpa_may_be_disclosed": True,
        }
    ]
    profile["submission_policy"]["trusted_external_application_hosts"] = [
        "example.com"
    ]
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(config, "load_profile", lambda: profile)

    acquired = launcher.acquire_job(target_url=url, preview_only=False)

    assert acquired is None
    stored = conn.execute(
        "SELECT tailored_resume_path, tailor_status, tailor_error FROM jobs WHERE url=?",
        (url,),
    ).fetchone()
    assert stored["tailored_resume_path"] is None
    assert stored["tailor_status"] == "stale_profile_fact"
    assert "current profile records 3.46" in stored["tailor_error"]


def test_submission_uncertain_requires_manual_review_and_is_not_reacquired(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/uncertain"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, application_url, "
        "tailored_resume_path, tailor_status, cover_letter_status, eligibility_status, "
        "apply_status, apply_retry_blocked) VALUES (?, 'Data Intern', 'Example', 'lever', "
        "'lever', ?, 'resume.txt', 'machine_validated', 'not_required', 'eligible', "
        "'submission_uncertain', 0)",
        (url, url),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher.reset_failed() == 0
    acquired = launcher.acquire_job(target_url=url, preview_only=False)

    assert acquired is None
    assert conn.execute(
        "SELECT apply_retry_blocked FROM jobs WHERE url = ?", (url,)
    ).fetchone()[0] == 0


def test_reset_failed_can_be_scoped_to_one_exact_job(monkeypatch, tmp_path: Path) -> None:
    conn = init_db(tmp_path / "jobs.db")
    first = "https://example.com/failed-one"
    second = "https://example.com/failed-two"
    for url in (first, second):
        conn.execute(
            "INSERT INTO jobs (url, application_url, title, apply_status, apply_error, "
            "apply_attempts) VALUES (?, ?, 'Intern', 'failed', 'prepare failed', 3)",
            (url, url),
        )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher.reset_failed(first) == 1
    states = {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT url, apply_status, apply_attempts FROM jobs")
    }
    assert states[first] == (None, 0)
    assert states[second] == ("failed", 3)


def test_automatic_exact_submission_enforces_policy_minimum(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/auto-submit"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, full_description, "
        "tailored_resume_path, tailor_status, cover_letter_status, eligibility_status, "
        "fit_score) VALUES (?, 'AI Intern', 'Example', 'lever', 'lever', 'Verified JD', "
        "'resume.txt', 'machine_validated', 'not_required', 'eligible', 7)",
        (url,),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setenv("APPLYPILOT_AUTO_SUBMIT", "1")
    monkeypatch.setenv("APPLYPILOT_AUTO_SUBMIT_MIN_SCORE", "8")

    assert launcher.acquire_job(target_url=url, preview_only=False) is None

    conn.execute("UPDATE jobs SET fit_score=8 WHERE url=?", (url,))
    conn.commit()
    acquired = launcher.acquire_job(target_url=url, preview_only=False)
    assert acquired is not None
    assert acquired["fit_score"] == 8


def test_exact_acquisition_enforces_cli_floor_and_stricter_legacy_auto_floor(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/exact-score-floor"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, application_url, "
        "full_description, tailored_resume_path, tailor_status, cover_letter_status, "
        "eligibility_status, fit_score) VALUES (?, 'AI Intern', 'Example', 'lever', 'lever', "
        "?, 'Verified JD', 'resume.txt', 'machine_validated', 'not_required', 'eligible', 8)",
        (url, url),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher.acquire_job(target_url=url, min_score=9, preview_only=True) is None
    monkeypatch.setenv("APPLYPILOT_AUTO_SUBMIT", "1")
    monkeypatch.setenv("APPLYPILOT_AUTO_SUBMIT_MIN_SCORE", "7")
    assert launcher.acquire_job(target_url=url, min_score=9, preview_only=False) is None


def test_ready_stage_accepts_previewed_linkedin_job_with_verified_no_cover(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://www.linkedin.com/jobs/view/123456"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, "
        "tailored_resume_path, tailor_status, cover_letter_status, eligibility_status, "
        "apply_status, fit_score) VALUES (?, 'AI Intern', 'Example', 'linkedin', "
        "'linkedin', 'resume.txt', 'machine_validated', 'not_required', 'eligible', "
        "'previewed', 9)",
        (url,),
    )
    conn.commit()

    assert get_stats(conn)["ready_to_apply"] == 1
    pending = get_jobs_by_stage(conn=conn, stage="pending_apply", min_score=8)
    assert [job["url"] for job in pending] == [url]


def test_exact_url_acquisition_requires_human_approval_and_accepts_null_status(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/approved"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, application_url, full_description, "
        "tailored_resume_path, tailor_status, cover_letter_path, cover_letter_status, "
        "eligibility_status, fit_score) "
        "VALUES (?, 'AI Intern', 'Example', 'linkedin', 'linkedin', ?, 'Verified JD', 'resume.txt', "
        "'machine_validated', 'letter.txt', 'human_approved', 'eligible', 8)",
        (url, url),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(
        config,
        "load_profile",
        lambda: {"submission_policy": {"trusted_external_application_hosts": ["example.com"]}},
    )

    acquired = launcher.acquire_job(target_url=url, worker_id=2)

    assert acquired is not None
    assert acquired["company_name"] == "Example"
    assert conn.execute("SELECT apply_status FROM jobs WHERE url=?", (url,)).fetchone()[0] == "in_progress"


def test_unapproved_cover_letter_cannot_enter_apply_queue(monkeypatch, tmp_path: Path) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/unapproved"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, application_url, "
        "tailored_resume_path, tailor_status, cover_letter_path, cover_letter_status, "
        "eligibility_status) "
        "VALUES (?, 'AI Intern', 'Example', 'linkedin', 'linkedin', ?, 'resume.txt', "
        "'machine_validated', 'letter.txt', 'machine_validated', 'eligible')",
        (url, url),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher.acquire_job(target_url=url) is None
    assert conn.execute("SELECT apply_status FROM jobs WHERE url=?", (url,)).fetchone()[0] is None


def test_preview_acquisition_accepts_validated_resume_without_cover(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/preview"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, application_url, full_description, "
        "tailored_resume_path, tailor_status, eligibility_status, fit_score) "
        "VALUES (?, 'Data Analyst Intern', 'Example', 'lever', 'lever', ?, 'Verified JD', "
        "'resume.txt', 'machine_validated', 'eligible', 8)",
        (url, url),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    acquired = launcher.acquire_job(target_url=url, preview_only=True)

    assert acquired is not None
    assert acquired["cover_letter_path"] is None
    assert conn.execute("SELECT apply_status FROM jobs WHERE url=?", (url,)).fetchone()[0] == "in_progress"


def test_batch_cover_uses_each_jobs_selected_resume_and_sets_review_gate(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    selected_resume = tmp_path / "selected.txt"
    selected_resume.write_text("SELECTED RESUME FACTS", encoding="utf-8")
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, full_description, "
        "fit_score, tailored_resume_path, tailor_status, eligibility_status) VALUES "
        "('https://example.com/job', 'AI Intern', 'Example', 'linkedin', 'linkedin', "
        "'Python machine learning validation requirements', 9, ?, 'machine_validated', 'eligible')",
        (str(selected_resume),),
    )
    conn.commit()
    captured = {}

    def fake_generate(resume_text, job, profile, evidence_sources, **kwargs):
        captured["resume_text"] = resume_text
        captured["evidence_sources"] = evidence_sources
        return {
            "text": "Dear Hiring Manager,\n\nFirst specific paragraph with verified facts and outcomes.\n\nSecond specific paragraph with verified facts and outcomes.\n\nThird specific paragraph for Example and its machine learning requirements.\n\nSincerely,\n\nRyan Yu",
            "validation": {"passed": True, "errors": [], "warnings": []},
            "evidence_plan": {"requirements": []},
            "surface": "formal",
        }

    monkeypatch.setattr(cover_letter, "get_connection", lambda: conn)
    monkeypatch.setattr(cover_letter, "load_profile", lambda: {"cover_letter": {"evidence_sources": []}})
    monkeypatch.setattr(cover_letter, "COVER_LETTER_DIR", tmp_path / "letters")
    monkeypatch.setattr(cover_letter, "generate_cover_letter_document", fake_generate)

    result = cover_letter.run_cover_letters(min_score=7, limit=1, validation_mode="strict")

    row = conn.execute(
        "SELECT cover_letter_status, cover_letter_source_resume_path, cover_letter_path "
        "FROM jobs WHERE url='https://example.com/job'"
    ).fetchone()
    assert result["generated"] == 1
    assert captured["resume_text"] == "SELECTED RESUME FACTS"
    assert row[0] == "machine_validated"
    assert row[1] == str(selected_resume.resolve())
    assert Path(row[2]).exists()


def test_batch_cover_does_not_persist_failed_generation(monkeypatch, tmp_path: Path) -> None:
    conn = init_db(tmp_path / "jobs.db")
    selected_resume = tmp_path / "selected.txt"
    selected_resume.write_text("SELECTED RESUME FACTS", encoding="utf-8")
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, full_description, "
        "fit_score, tailored_resume_path, tailor_status, eligibility_status) VALUES "
        "('https://example.com/fail', 'AI Intern', 'Example', 'linkedin', 'linkedin', "
        "'Python machine learning validation requirements', 9, ?, 'machine_validated', 'eligible')",
        (str(selected_resume),),
    )
    conn.commit()
    monkeypatch.setattr(cover_letter, "get_connection", lambda: conn)
    monkeypatch.setattr(cover_letter, "load_profile", lambda: {"cover_letter": {"evidence_sources": []}})
    monkeypatch.setattr(cover_letter, "COVER_LETTER_DIR", tmp_path / "letters")

    def fail_generation(*args, **kwargs):
        raise cover_letter.CoverLetterValidationError("repetition detected")

    monkeypatch.setattr(cover_letter, "generate_cover_letter_document", fail_generation)

    result = cover_letter.run_cover_letters(min_score=7, limit=1, validation_mode="strict")

    row = conn.execute(
        "SELECT cover_letter_status, cover_letter_path, cover_letter_error FROM jobs "
        "WHERE url='https://example.com/fail'"
    ).fetchone()
    assert result["generated"] == 0
    assert result["errors"] == 1
    assert tuple(row) == ("failed_validation", None, "repetition detected")
    assert not (tmp_path / "letters").exists() or not list((tmp_path / "letters").glob("*.txt"))


def _grounded_tailor_payload() -> tuple[str, dict, dict]:
    source = (
        "Ryan Yu\nWORK EXPERIENCE\nExample Company\nData Analyst\n"
        "Built Python automation for verified public data workflows.\n"
        "Validated SQL outputs through repeatable review checks.\n"
        "TECHNICAL SKILLS\nPython, SQL\nEDUCATION\nExample University"
    )
    job = {
        "title": "Data Analyst Intern",
        "company_name": "Target Company",
        "source_site": "indeed",
        "full_description": "Build Python automation and validate SQL outputs for analytics workflows.",
    }
    data = {
        "title": "Data Analyst",
        "summary": "Data analyst focused on Python automation and validated SQL outputs.",
        "skills": {"Data": "Python, SQL"},
        "experience": [{
            "header": "Example Company",
            "subtitle": "Data Analyst",
            "bullets": [
                "Built Python automation for verified public data workflows.",
                "Validated SQL outputs through repeatable review checks.",
            ],
        }],
        "projects": [],
        "education": "Example University",
        "evidence_map": [
            {
                "requirement": "Python automation",
                "support_level": "direct",
                "source_quote": "Built Python automation for verified public data workflows.",
            },
            {
                "requirement": "SQL outputs",
                "support_level": "direct",
                "source_quote": "Validated SQL outputs through repeatable review checks.",
            },
        ],
    }
    return source, job, data


def test_tailoring_prompts_forbid_related_tools_and_minor_stretches() -> None:
    profile = {"skills_boundary": {"languages": ["Python"]}, "resume_facts": {}}

    generation_prompt = tailor._build_tailor_prompt(profile, source_has_projects=False)
    judge_prompt = tailor._build_judge_prompt(profile)

    assert "Do not add even a closely related" in generation_prompt
    assert "Allow up to 3 minor stretches" not in judge_prompt
    assert "There is no allowance" in judge_prompt
    assert "Audit every complete sentence" in judge_prompt
    assert "Do not turn a JD responsibility into candidate history" in generation_prompt


def test_cover_letter_email_policy_uses_confirmed_full_time_dates(tmp_path: Path) -> None:
    profile = {
        "personal": {"full_name": "Taylor Chen"},
        "skills_boundary": {"languages": ["Python"]},
        "resume_facts": {},
        "availability": {
            "credit_bearing_internship_start": "2026-11-10",
            "generic_application_availability_date": "2026-11-10",
            "internship_end_date": "2027-06-30",
        },
        "work_authorization": {
            "require_sponsorship": "No for a programme-credit-bearing internship",
        },
        "contact_preferences": {
            "email_application_availability_policy": (
                "When relevant, state full-time availability from 2026-11-10 through "
                "2027-06-30."
            ),
            "email_application_work_authorization_statement": (
                "I can undertake the internship on a programme-credit-bearing basis and "
                "therefore do not require employment sponsorship."
            ),
        },
    }

    prompt_text = cover_letter._build_cover_letter_prompt(profile, surface="formal")
    primary_path = tmp_path / "resume.txt"
    evidence = cover_letter.load_evidence_sources(profile, primary_path, "Verified resume")
    profile_evidence = evidence[-1]["text"]

    assert "Use the flexible confirmed month range" in prompt_text
    assert "programme-credit-bearing basis" in prompt_text
    assert "Never invent an earlier date, a weekly-hours cap" in prompt_text
    assert "2026-11-10" in prompt_text
    assert "2027-06-30" in prompt_text
    assert "2026-11-10" in profile_evidence
    assert "2027-06-30" in profile_evidence
    assert "Do not disclose a specific internship start date" not in profile_evidence
    assert "Approved email work-authorization statement" in profile_evidence


def test_tailoring_judge_rejects_pass_without_exact_summary_evidence(monkeypatch) -> None:
    source, job, _ = _grounded_tailor_payload()
    tailored_text = (
        "Ryan Yu\nData Analyst\n\nSUMMARY\n"
        "Data analyst who analyzed engagement data. "
        "Built Python automation for verified public data workflows.\n\n"
        "TECHNICAL SKILLS\nData: Python, SQL\n"
    )

    class FakeJudgeClient:
        last_response_meta: ClassVar[dict] = {}

        def chat(self, *args, **kwargs):
            import json
            return json.dumps({
                "verdict": "PASS",
                "issues": [],
                "summary_claims": [{
                    "claim": "Built Python automation for verified public data workflows.",
                    "source_quote": "Built Python automation for verified public data workflows.",
                    "supported": True,
                }],
            })

    monkeypatch.setattr(tailor, "get_client", lambda: FakeJudgeClient())

    result = tailor.judge_tailored_resume(
        source,
        tailored_text,
        job["title"],
        {"resume_facts": {}, "skills_boundary": {}},
        job_description=job["full_description"],
    )

    assert result["passed"] is False
    assert result["summary_evidence_complete"] is False
    assert "analyzed engagement data" in result["issues"]


def test_tailoring_judge_accepts_exact_clause_with_semicolon_boundary(monkeypatch) -> None:
    source, job, _ = _grounded_tailor_payload()
    source += (
        "\nProcessed public data in Python/PostgreSQL and scheduled refreshes in Python; "
        "combined weighted sums and routing constraints."
    )
    summary_sentence = "Developed Python data pipelines with PostgreSQL."
    tailored_text = (
        f"Ryan Yu\nAI Engineer\n\nSUMMARY\n{summary_sentence}\n\n"
        "TECHNICAL SKILLS\nData: Python, PostgreSQL\n"
    )

    class FakeJudgeClient:
        last_response_meta: ClassVar[dict] = {}

        def chat(self, *args, **kwargs):
            import json
            return json.dumps({
                "verdict": "PASS",
                "issues": [],
                "summary_claims": [{
                    "claim": summary_sentence,
                    "source_quotes": [
                        "Processed public data in Python/PostgreSQL and scheduled refreshes in Python."
                    ],
                    "supported": True,
                }],
            })

    monkeypatch.setattr(tailor, "get_client", lambda: FakeJudgeClient())

    result = tailor.judge_tailored_resume(
        source,
        tailored_text,
        job["title"],
        {"resume_facts": {}, "skills_boundary": {"data": ["Python", "PostgreSQL"]}},
        job_description=job["full_description"],
    )

    assert result["passed"] is True
    assert result["summary_evidence_complete"] is True


def test_tailoring_judge_ignores_extra_non_exact_quote_when_exact_evidence_exists(
    monkeypatch,
) -> None:
    source, job, _ = _grounded_tailor_payload()
    exact_quote = "Built Python automation for verified public data workflows."
    summary_sentence = "Built Python automation for verified public data workflows."
    tailored_text = (
        f"Ryan Yu\nAI Engineer\n\nSUMMARY\n{summary_sentence}\n\n"
        "TECHNICAL SKILLS\nData: Python\n"
    )

    class FakeJudgeClient:
        last_response_meta: ClassVar[dict] = {}

        def chat(self, *args, **kwargs):
            return json.dumps(
                {
                    "verdict": "PASS",
                    "issues": [],
                    "summary_claims": [
                        {
                            "claim": summary_sentence,
                            "source_quotes": [
                                exact_quote,
                                "Recomposed explanatory wording not present in the source.",
                            ],
                            "supported": True,
                        }
                    ],
                }
            )

    monkeypatch.setattr(tailor, "get_client", lambda: FakeJudgeClient())

    result = tailor.judge_tailored_resume(
        source,
        tailored_text,
        job["title"],
        {"resume_facts": {}, "skills_boundary": {"data": ["Python"]}},
        job_description=job["full_description"],
    )

    assert result["passed"] is True
    assert result["summary_evidence_complete"] is True


def test_tailoring_judge_stops_summary_before_internship_education_section(
    monkeypatch,
) -> None:
    source, job, _ = _grounded_tailor_payload()
    sentence = "Built Python automation for verified public data workflows."
    tailored_text = (
        f"Ryan Yu\n\nSUMMARY\n{sentence}\n\n"
        "EDUCATION\nExample University, Example Degree\n\n"
        "TECHNICAL SKILLS\nData: Python\n"
    )

    class FakeJudgeClient:
        last_response_meta: ClassVar[dict] = {}

        def chat(self, *args, **kwargs):
            return json.dumps(
                {
                    "verdict": "PASS",
                    "issues": [],
                    "summary_claims": [
                        {
                            "claim": sentence,
                            "source_quotes": [sentence],
                            "supported": True,
                        }
                    ],
                }
            )

    monkeypatch.setattr(tailor, "get_client", lambda: FakeJudgeClient())

    result = tailor.judge_tailored_resume(
        source + "\n" + sentence,
        tailored_text,
        job["title"],
        {"resume_facts": {}, "skills_boundary": {"data": ["Python"]}},
        job_description=job["full_description"],
    )

    assert result["passed"] is True
    assert result["summary_sentences"] == [sentence]


def test_tailoring_judge_treats_urban_planning_as_one_sector_phrase(monkeypatch) -> None:
    sentence = "Data analyst with experience across urban planning and legal products."
    source = (
        "Master of City Planning (Transportation and Infrastructure Planning)\n"
        "Built the legal data and retrieval layer for Chongli Law Firm."
    )
    tailored_text = (
        f"Ryan Yu\n\nSUMMARY\n{sentence}\n\n"
        "TECHNICAL SKILLS\nData: Python\n"
    )

    class FakeJudgeClient:
        last_response_meta: ClassVar[dict] = {}

        def chat(self, *args, **kwargs):
            return json.dumps(
                {
                    "verdict": "PASS",
                    "issues": [],
                    "summary_claims": [
                        {
                            "claim": sentence,
                            "source_quotes": [
                                "Master of City Planning (Transportation and Infrastructure Planning)",
                                "Built the legal data and retrieval layer for Chongli Law Firm.",
                            ],
                            "supported": True,
                        }
                    ],
                }
            )

    monkeypatch.setattr(tailor, "get_client", lambda: FakeJudgeClient())

    result = tailor.judge_tailored_resume(
        source,
        tailored_text,
        "Data Analyst Intern",
        {"resume_facts": {}, "skills_boundary": {"data": ["Python"]}},
    )

    assert result["passed"] is True


def test_tailoring_judge_still_rejects_changed_clause_wording(monkeypatch) -> None:
    source, job, _ = _grounded_tailor_payload()
    source += "\nProcessed public data in Python and scheduled refreshes; documented results."
    summary_sentence = "Developed Python data pipelines."
    tailored_text = (
        f"Ryan Yu\nAI Engineer\n\nSUMMARY\n{summary_sentence}\n\n"
        "TECHNICAL SKILLS\nData: Python\n"
    )

    class FakeJudgeClient:
        last_response_meta: ClassVar[dict] = {}

        def chat(self, *args, **kwargs):
            import json
            return json.dumps({
                "verdict": "PASS",
                "issues": [],
                "summary_claims": [{
                    "claim": summary_sentence,
                    "source_quotes": [
                        "Processed private data in Python and scheduled refreshes."
                    ],
                    "supported": True,
                }],
            })

    monkeypatch.setattr(tailor, "get_client", lambda: FakeJudgeClient())

    result = tailor.judge_tailored_resume(
        source,
        tailored_text,
        job["title"],
        {"resume_facts": {}, "skills_boundary": {"data": ["Python"]}},
        job_description=job["full_description"],
    )

    assert result["passed"] is False
    assert result["summary_evidence_complete"] is False


def test_tailoring_judge_rejects_cross_domain_fact_splicing(monkeypatch) -> None:
    source, job, _ = _grounded_tailor_payload()
    source += "\nBuilt a hybrid RAG assistant for a legal client.\nWorked on urban planning data."
    summary_sentence = "Built a hybrid RAG assistant for legal and planning domains."
    tailored_text = (
        f"Ryan Yu\nAI Engineer\n\nSUMMARY\n{summary_sentence}\n\n"
        "TECHNICAL SKILLS\nApplied AI: hybrid RAG\n"
    )

    class FakeJudgeClient:
        last_response_meta: ClassVar[dict] = {}

        def chat(self, *args, **kwargs):
            import json
            return json.dumps({
                "verdict": "PASS",
                "issues": [],
                "summary_claims": [{
                    "claim": summary_sentence,
                    "source_quote": "Built a hybrid RAG assistant for a legal client.",
                    "supported": True,
                }],
            })

    monkeypatch.setattr(tailor, "get_client", lambda: FakeJudgeClient())

    result = tailor.judge_tailored_resume(
        source,
        tailored_text,
        job["title"],
        {"resume_facts": {}, "skills_boundary": {}},
        job_description=job["full_description"],
    )

    assert result["passed"] is False
    assert "claimed sector(s): planning" in result["issues"]


def test_tailoring_judge_rejects_metric_transfer_between_source_bullets(monkeypatch) -> None:
    source, job, _ = _grounded_tailor_payload()
    source += (
        "\nDesigned three role-specific workflows."
        "\nSupported a controlled trial involving ~10 users."
    )
    summary_sentence = "Designed three workflows that served approximately 10 users."
    tailored_text = (
        f"Ryan Yu\nAI Engineer\n\nSUMMARY\n{summary_sentence}\n\n"
        "TECHNICAL SKILLS\nApplied AI: Python\n"
    )

    class FakeJudgeClient:
        last_response_meta: ClassVar[dict] = {}

        def chat(self, *args, **kwargs):
            import json
            return json.dumps({
                "verdict": "PASS",
                "issues": [],
                "summary_claims": [{
                    "claim": summary_sentence,
                    "source_quotes": ["Designed three role-specific workflows."],
                    "supported": True,
                }],
            })

    monkeypatch.setattr(tailor, "get_client", lambda: FakeJudgeClient())

    result = tailor.judge_tailored_resume(
        source,
        tailored_text,
        job["title"],
        {"resume_facts": {}, "skills_boundary": {}},
        job_description=job["full_description"],
    )

    assert result["passed"] is False
    assert "numeric claim(s): 10" in result["issues"]


def test_tailoring_router_selects_different_registered_resume_tracks(tmp_path: Path) -> None:
    ai_resume = tmp_path / "ai.txt"
    data_resume = tmp_path / "data.txt"
    ai_resume.write_text("AI source", encoding="utf-8")
    data_resume.write_text("Data source", encoding="utf-8")
    profile = {
        "tailoring": {
            "resume_variants": [
                {"track": "ai", "path": str(ai_resume), "keywords": ["llm", "rag"]},
                {"track": "data", "path": str(data_resume), "keywords": ["data analyst", "sql"]},
            ]
        }
    }

    ai_path, ai_route = tailor.select_resume_source(
        {"title": "LLM Intern", "full_description": "Build RAG workflows."}, profile
    )
    data_path, data_route = tailor.select_resume_source(
        {"title": "Data Analyst Intern", "full_description": "Write SQL queries."}, profile
    )

    assert ai_path == ai_resume.resolve()
    assert ai_route["track"] == "ai"
    assert data_path == data_resume.resolve()
    assert data_route["track"] == "data"


def test_tailoring_validator_rejects_new_skill_and_numeric_claim() -> None:
    source, job, data = _grounded_tailor_payload()
    data["skills"] = {"Data": "Python, SQL, Kubernetes"}
    data["summary"] += " Improved accuracy by 99%."

    validation = validate_json_fields(
        data,
        {"resume_facts": {}},
        mode="strict",
        original_text=source,
        job_description=job["full_description"],
    )

    assert validation["passed"] is False
    assert any("Skills section adds tokens" in error for error in validation["errors"])
    assert any("numeric claims" in error for error in validation["errors"])


def test_tailoring_validator_preserves_role_before_compact_month_range() -> None:
    source, job, data = _grounded_tailor_payload()
    company = "Jiangxi Province Architecture Design Research Institution"
    source = source.replace(
        "Example Company\nData Analyst\n",
        f"{company}\nPlanning Analyst\tJun–Dec 2021 (Intern); May–Aug 2022 (Full-time)\n",
    )
    data["experience"][0]["header"] = company
    data["experience"][0]["subtitle"] = "Planning Analyst"

    validation = validate_json_fields(
        data,
        {"resume_facts": {"preserved_companies": [company]}},
        mode="strict",
        original_text=source,
        job_description=job["full_description"],
        job_title=job["title"],
        target_company=job["company_name"],
    )

    assert validation["passed"] is True
    assert not any("source role exactly" in error for error in validation["errors"])


def test_tailoring_validator_rejects_invented_seniority_and_target_history() -> None:
    source, job, data = _grounded_tailor_payload()
    data["title"] = "Senior Data Analyst"
    data["experience"][0]["bullets"].append(
        "Built Target Company engagement analytics for product decisions."
    )

    validation = validate_json_fields(
        data,
        {"resume_facts": {}},
        mode="strict",
        original_text=source,
        job_description=job["full_description"],
        job_title=job["title"],
        target_company=job["company_name"],
    )

    assert validation["passed"] is False
    assert any("adds seniority" in error for error in validation["errors"])
    assert any("copied into candidate history" in error for error in validation["errors"])


def test_tailoring_validator_rejects_jd_only_summary_claims() -> None:
    source, job, data = _grounded_tailor_payload()
    data["summary"] = "Data analyst who analyzed engagement behavior for product growth."
    job["full_description"] += " Analyze engagement behavior to drive product growth."

    validation = validate_json_fields(
        data,
        {"resume_facts": {}},
        mode="strict",
        original_text=source,
        job_description=job["full_description"],
        job_title=job["title"],
        target_company=job["company_name"],
    )

    assert validation["passed"] is False
    assert any("JD-only claim terms" in error for error in validation["errors"])


def test_tailoring_validator_rejects_pluralized_single_trial() -> None:
    source, job, data = _grounded_tailor_payload()
    source += "\nCompleted a controlled trial with law-firm users."
    data["summary"] = "AI-powered analyst who ran controlled trials with users."
    job["full_description"] += " Run controlled trials for AI-powered products."

    validation = validate_json_fields(
        data,
        {"resume_facts": {}},
        mode="strict",
        original_text=source,
        job_description=job["full_description"],
        job_title=job["title"],
        target_company=job["company_name"],
    )

    assert validation["passed"] is False
    assert any("pluralizes a single source event" in error for error in validation["errors"])
    assert not any("power" in error for error in validation["errors"])


def test_tailoring_evidence_map_keeps_honest_gaps_but_requires_two_matches() -> None:
    source, job, data = _grounded_tailor_payload()
    data["evidence_map"][1] = {
        "requirement": "SQL outputs",
        "support_level": "gap",
        "source_quote": "",
    }

    validation = validate_json_fields(
        data,
        {"resume_facts": {}},
        mode="strict",
        original_text=source,
        job_description=job["full_description"],
        job_title=job["title"],
        target_company=job["company_name"],
    )

    assert validation["passed"] is False
    assert any("fewer than 2" in error for error in validation["errors"])
    assert not any("empty source_quote" in error for error in validation["errors"])


def test_strict_tailoring_never_approves_failed_judge(monkeypatch) -> None:
    source, job, data = _grounded_tailor_payload()

    class FakeClient:
        def chat(self, *args, **kwargs):
            import json
            return json.dumps(data)

    monkeypatch.setattr(tailor, "get_client", lambda: FakeClient())
    monkeypatch.setattr(
        tailor,
        "judge_tailored_resume",
        lambda *args, **kwargs: {
            "passed": False,
            "verdict": "FAIL",
            "issues": "unsupported ownership wording",
            "raw": "VERDICT: FAIL",
        },
    )

    text, report = tailor.tailor_resume(
        source,
        job,
        {"personal": {}, "resume_facts": {}, "skills_boundary": {}},
        max_retries=0,
        validation_mode="strict",
    )

    assert text
    assert report["status"] == "failed_judge"


def test_no_project_source_does_not_gain_project_section() -> None:
    _, _, data = _grounded_tailor_payload()

    assembled = tailor.assemble_resume_text(data, {"personal": {}})

    assert "\nPROJECTS\n" not in assembled


def test_tailoring_failure_clears_db_path_and_quarantines_stale_output(
    tmp_path: Path, monkeypatch
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, tailored_resume_path TEXT, "
        "tailored_at TEXT, tailor_status TEXT, tailor_error TEXT, "
        "tailor_source_resume_path TEXT, tailor_report_path TEXT, "
        "tailor_attempts INTEGER)"
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://example.com/product",
            "old.txt",
            "2026-01-01",
            "machine_validated",
            None,
            "source.docx",
            "old-report.json",
            1,
        ),
    )
    output_dir = tmp_path / "tailored"
    output_dir.mkdir()
    old_output = output_dir / "Target_Co_Product_Analyst.txt"
    old_output.write_text("previously accepted", encoding="utf-8")
    source_path = tmp_path / "source.txt"
    source_path.write_text("source resume", encoding="utf-8")
    job = {
        "url": "https://example.com/product",
        "title": "Product Analyst",
        "company_name": "Target Co",
        "full_description": "Product analytics",
        "fit_score": 8,
    }

    monkeypatch.setattr(tailor, "TAILORED_DIR", output_dir)
    monkeypatch.setattr(tailor, "load_profile", dict)
    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "get_jobs_by_stage", lambda **kwargs: [job])
    monkeypatch.setattr(
        tailor,
        "select_resume_source",
        lambda *_: (source_path, {"method": "test", "track": "test"}),
    )
    monkeypatch.setattr(tailor, "read_resume_source", lambda _: "source resume")
    monkeypatch.setattr(
        tailor,
        "tailor_resume",
        lambda *args, **kwargs: (
            "rejected draft",
            {
                "status": "failed_judge",
                "attempts": 1,
                "validator": {"errors": []},
                "full_validator": {"errors": []},
                "judge": {"passed": False, "issues": "unsupported claim"},
            },
        ),
    )

    result = tailor.run_tailoring(min_score=7, limit=1, validation_mode="strict")

    row = conn.execute(
        "SELECT tailored_resume_path, tailored_at, tailor_status, tailor_error "
        "FROM jobs WHERE url=?",
        (job["url"],),
    ).fetchone()
    quarantined = list((output_dir / "rejected").glob("*_PREVIOUSLY_VALIDATED_*.txt"))
    assert result["machine_validated"] == 0
    assert row == (None, None, "failed_judge", "Judge: unsupported claim")
    assert not old_output.exists()
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "previously accepted"


def test_tailoring_render_failure_never_becomes_machine_validated(
    tmp_path: Path, monkeypatch
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, tailored_resume_path TEXT, "
        "tailored_at TEXT, tailor_status TEXT, tailor_error TEXT, "
        "tailor_source_resume_path TEXT, tailor_report_path TEXT, "
        "tailor_attempts INTEGER)"
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, NULL, NULL, NULL, NULL, NULL, NULL, 0)",
        ("https://example.com/data",),
    )
    output_dir = tmp_path / "tailored"
    source_path = tmp_path / "source.txt"
    source_path.write_text("source resume", encoding="utf-8")
    job = {
        "url": "https://example.com/data",
        "title": "Data Analyst Intern",
        "company_name": "Target Co",
        "full_description": "Product analytics",
        "fit_score": 8,
    }

    monkeypatch.setattr(tailor, "TAILORED_DIR", output_dir)
    monkeypatch.setattr(tailor, "load_profile", dict)
    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "get_jobs_by_stage", lambda **kwargs: [job])
    monkeypatch.setattr(
        tailor,
        "select_resume_source",
        lambda *_: (source_path, {"method": "test", "track": "data"}),
    )
    monkeypatch.setattr(tailor, "read_resume_source", lambda _: "source resume")
    monkeypatch.setattr(
        tailor,
        "tailor_resume",
        lambda *args, **kwargs: (
            "validated content",
            {
                "status": "machine_validated",
                "attempts": 1,
                "validator": {"errors": []},
                "full_validator": {"errors": []},
                "judge": {"passed": True, "issues": "none"},
            },
        ),
    )
    monkeypatch.setattr(
        pdf_renderer,
        "convert_to_pdf",
        lambda path: (_ for _ in ()).throw(ValueError("underfilled summary tail")),
    )

    result = tailor.run_tailoring(min_score=7, limit=1, validation_mode="strict")
    row = conn.execute(
        "SELECT tailored_resume_path, tailor_status, tailor_error FROM jobs WHERE url=?",
        (job["url"],),
    ).fetchone()

    assert result["machine_validated"] == 0
    assert result["failed"] == 1
    assert row == (None, "failed_render", "Render: underfilled summary tail")
    assert (output_dir / "rejected" / "Target_Co_Data_Analyst_Intern_REJECTED.txt").exists()


@pytest.mark.parametrize(
    ("failure_stage", "expected_status"),
    [
        ("judge", "failed_revalidation"),
        ("render", "failed_render"),
        ("report", "failed_revalidation"),
    ],
)
def test_revalidation_revokes_old_machine_validated_state_on_any_failure(
    tmp_path: Path, monkeypatch, failure_stage: str, expected_status: str
) -> None:
    from applypilot import single_job
    from applypilot.scoring import tailor as tailor_module
    from applypilot.scoring import validator as validator_module

    database_path = tmp_path / "jobs.db"
    conn = init_db(database_path)
    tailored_path = tmp_path / "tailored.txt"
    source_path = tmp_path / "source.txt"
    report_path = tmp_path / "tailored_REPORT.json"
    old_pdf = tailored_path.with_suffix(".pdf")
    tailored_path.write_text("validated tailored resume", encoding="utf-8")
    source_path.write_text("source resume", encoding="utf-8")
    old_pdf.write_bytes(b"%PDF-previously-authorized")
    url = "https://careers.example.test/jobs/revalidate"
    conn.execute(
        "INSERT INTO jobs (url, application_url, title, company_name, "
        "full_description, tailored_resume_path, tailor_source_resume_path, "
        "tailor_report_path, tailor_status, tailored_at, tailor_attempts, "
        "eligibility_status) VALUES (?, ?, 'Data Analyst', 'Example', "
        "'Use Python and SQL.', ?, ?, ?, 'machine_validated', ?, 1, 'eligible')",
        (
            url,
            "https://ats.example.test/apply/revalidate",
            str(tailored_path),
            str(source_path),
            str(report_path),
            "2026-08-24T00:00:00+00:00",
        ),
    )
    conn.commit()

    monkeypatch.setattr(single_job, "get_connection", lambda: conn)
    monkeypatch.setattr(single_job, "load_profile", dict)
    monkeypatch.setattr(single_job, "read_resume_source", lambda path: "source resume")
    monkeypatch.setattr(
        validator_module,
        "validate_tailored_resume",
        lambda *args, **kwargs: {"passed": True, "errors": []},
    )

    if failure_stage == "judge":
        monkeypatch.setattr(
            tailor_module,
            "judge_tailored_resume",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("judge unavailable")
            ),
        )
    else:
        monkeypatch.setattr(
            tailor_module,
            "judge_tailored_resume",
            lambda *args, **kwargs: {"passed": True, "issues": []},
        )

    if failure_stage == "render":
        monkeypatch.setattr(
            pdf_renderer,
            "convert_to_pdf",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError("render unavailable")
            ),
        )
    else:
        def render_to_requested_path(path, output_path=None):
            output = Path(output_path)
            output.write_bytes(b"%PDF-newly-rendered")
            return output

        monkeypatch.setattr(pdf_renderer, "convert_to_pdf", render_to_requested_path)

    if failure_stage == "report":
        monkeypatch.setattr(
            single_job,
            "_write_json_atomic",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("report unavailable")
            ),
        )

    result = single_job.revalidate_tailored_resume_for_url(url)
    verify = sqlite3.connect(database_path)
    row = verify.execute(
        "SELECT tailor_status, tailor_error, tailored_at, tailor_attempts "
        "FROM jobs WHERE url=?",
        (url,),
    ).fetchone()
    verify.close()

    assert result["status"] == expected_status
    assert row[0] == expected_status
    assert row[0] != "machine_validated"
    assert row[2] is None
    assert row[3] == 2
    assert not old_pdf.exists()
    assert list((tmp_path / "rejected").glob("tailored_PRE_REVALIDATION_*.pdf"))


def test_revalidation_recovers_edited_rejected_text_after_render_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from applypilot import single_job
    from applypilot.scoring import tailor as tailor_module
    from applypilot.scoring import validator as validator_module

    database_path = tmp_path / "jobs.db"
    conn = init_db(database_path)
    source_path = tmp_path / "source.txt"
    source_path.write_text("source resume", encoding="utf-8")
    report_path = tmp_path / "Target_Resume_REPORT.json"
    rejected_dir = tmp_path / "rejected"
    rejected_dir.mkdir()
    rejected_path = rejected_dir / "Target_Resume_REJECTED.txt"
    rejected_path.write_text("edited concise resume", encoding="utf-8")
    url = "https://careers.example.test/jobs/recover-rejected"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, full_description, "
        "tailor_source_resume_path, tailor_report_path, tailor_status, "
        "eligibility_status) VALUES (?, 'Data Analyst', 'Example', "
        "'Use Python and SQL.', ?, ?, 'failed_render', 'eligible')",
        (url, str(source_path), str(report_path)),
    )
    conn.commit()

    monkeypatch.setattr(single_job, "get_connection", lambda: conn)
    monkeypatch.setattr(single_job, "load_profile", dict)
    monkeypatch.setattr(single_job, "read_resume_source", lambda path: "source resume")
    monkeypatch.setattr(
        validator_module,
        "validate_tailored_resume",
        lambda *args, **kwargs: {"passed": True, "errors": []},
    )
    monkeypatch.setattr(
        tailor_module,
        "judge_tailored_resume",
        lambda *args, **kwargs: {"passed": True, "issues": []},
    )

    def render_to_requested_path(path, output_path=None):
        output = Path(output_path)
        output.write_bytes(b"%PDF-recovered")
        return output

    monkeypatch.setattr(pdf_renderer, "convert_to_pdf", render_to_requested_path)

    result = single_job.revalidate_tailored_resume_for_url(url)
    recovered_path = tmp_path / "Target_Resume.txt"
    verify = sqlite3.connect(database_path)
    row = verify.execute(
        "SELECT tailored_resume_path, tailor_status FROM jobs WHERE url=?", (url,)
    ).fetchone()
    verify.close()

    assert result["status"] == "machine_validated"
    assert recovered_path.read_text(encoding="utf-8") == "edited concise resume"
    assert row == (str(recovered_path), "machine_validated")
    assert recovered_path.with_suffix(".pdf").read_bytes() == b"%PDF-recovered"
