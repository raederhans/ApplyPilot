"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.apply import agent_output as agent_output_mod
from applypilot.apply import agent_runtime as agent_runtime_mod
from applypilot.apply import application_jobs as application_jobs_mod
from applypilot.apply import page_observation as page_observation_mod
from applypilot.apply import prompt as prompt_mod
from applypilot.apply import worker_orchestration as worker_orchestration_mod
from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    _kill_process_tree,
    allocate_cdp_port,  # noqa: F401 - injected worker port
    capture_browser_session,  # noqa: F401 - injected worker port
    cleanup_on_exit,
    cleanup_worker,  # noqa: F401 - injected worker port
    kill_all_chrome,
    launch_chrome,  # noqa: F401 - injected worker port
    release_cdp_port,  # noqa: F401 - injected worker port
    reset_worker_dir,
    resolve_browser_backend,
    restore_browser_session,  # noqa: F401 - injected worker port
)
from applypilot.apply.dashboard import (
    add_event,
    get_state,
    get_totals,
    init_worker,
    render_full,
    update_state,
)
from applypilot.apply.router import (
    ControlRoute,
    cloak_fallback_route,
    computer_use_handoff_allowed,  # noqa: F401 - injected worker port
    initial_route,  # noqa: F401 - injected worker port
    prompt_control_contract,
    resolve_interaction_mode,
)
from applypilot.database import get_connection
from applypilot.runtime_settings import load_runtime_settings

# Document the compatibility surface consumed by the extracted worker. Tests
# and callers may still replace these ports before ``worker_loop``.
_WORKER_RUNTIME_EXPORTS = worker_orchestration_mod.WORKER_RUNTIME_PORTS

_format_failure_error = agent_output_mod.format_failure_error
_interpret_agent_output = agent_output_mod.interpret_agent_output
_parse_failure_context = agent_output_mod.parse_failure_context
_parse_result_line = agent_output_mod.parse_result_line
_parse_unanswered_questions = agent_output_mod.parse_unanswered_questions
_result_status = agent_output_mod.result_status
_validate_preview_audit = agent_output_mod.validate_preview_audit
_validate_submission_evidence = agent_output_mod.validate_submission_evidence
_application_fact_value = page_observation_mod._application_fact_value
_audit_live_pre_submit_page = page_observation_mod._audit_live_pre_submit_page
_bound_application_pages = page_observation_mod._bound_application_pages
_captcha_response_present = page_observation_mod._captcha_response_present
_classify_post_submit_observation = page_observation_mod._classify_post_submit_observation
_expected_screening_answer = page_observation_mod._expected_screening_answer
_observe_post_submit_page = page_observation_mod._observe_post_submit_page
_selected_matches_boolean = page_observation_mod._selected_matches_boolean
_select_application_page = page_observation_mod._select_application_page
_submission_evidence_consistent = page_observation_mod._submission_evidence_consistent
_validate_pre_submit_snapshot = page_observation_mod._validate_pre_submit_snapshot
_verification_clear_state_stable = page_observation_mod._verification_clear_state_stable
_visible_captcha_overlay = page_observation_mod._visible_captcha_overlay
_visible_verification_gate = page_observation_mod._visible_verification_gate
_work_authorization_answers = page_observation_mod._work_authorization_answers
_yes_no_value = page_observation_mod._yes_no_value


def _make_mcp_config(cdp_port: int) -> dict:
    return agent_runtime_mod.make_mcp_config(cdp_port)


def _resolve_claude_command() -> list[str]:
    return agent_runtime_mod.resolve_claude_command()


def _resolve_codex_command() -> list[str]:
    return agent_runtime_mod.resolve_codex_command()


_toml_value = agent_runtime_mod._toml_value
_toml_skill_config = agent_runtime_mod._toml_skill_config


def _start_timeout_watchdog(
    proc: subprocess.Popen, timeout_seconds: float
) -> tuple[threading.Event, threading.Timer]:
    return agent_runtime_mod.start_timeout_watchdog(
        proc,
        timeout_seconds,
        kill_process_tree=_kill_process_tree,
    )


def _build_agent_command(
    backend: str,
    model: str,
    port: int,
    worker_dir: Path,
    mcp_config_path: Path,
    *,
    credential_relay_authorized: bool = False,
) -> tuple[list[str], Path | None]:
    return agent_runtime_mod.build_agent_command(
        backend,
        model,
        port,
        worker_dir,
        mcp_config_path,
        resolve_claude=_resolve_claude_command,
        resolve_codex=_resolve_codex_command,
        python_executable=sys.executable,
        credential_relay_authorized=credential_relay_authorized,
    )


def acquire_job(
    target_url: str | None = None,
    min_score: int = 6,
    worker_id: int = 0,
    preview_only: bool = False,
    authorization_manifest: dict | None = None,
    exclude_urls: set[str] | None = None,
    application_lease_minutes: int | None = None,
) -> dict | None:
    if application_lease_minutes is None:
        application_lease_minutes = load_runtime_settings().application_lease_minutes
    return application_jobs_mod.acquire_job(
        get_connection(),
        target_url=target_url,
        min_score=min_score,
        worker_id=worker_id,
        preview_only=preview_only,
        authorization_manifest=authorization_manifest,
        exclude_urls=exclude_urls,
        load_blocked=_load_blocked,
        application_lease_minutes=application_lease_minutes,
    )


def mark_result(
    url: str,
    status: str,
    error: str | None = None,
    permanent: bool = False,
    duration_ms: int | None = None,
    task_id: str | None = None,
    evidence: dict | None = None,
) -> None:
    application_jobs_mod.mark_result(
        get_connection(),
        url,
        status,
        error,
        permanent,
        duration_ms,
        task_id,
        evidence,
    )


def release_lock(url: str, task_id: str | None = None) -> None:
    application_jobs_mod.release_lock(get_connection(), url, task_id)


def restore_preview_state(job: dict) -> None:
    application_jobs_mod.restore_preview_state(get_connection(), job)


def _mark_runtime_cover_not_required(job: dict) -> dict:
    return application_jobs_mod.mark_runtime_cover_not_required(get_connection(), job)


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    application_jobs_mod.mark_job(get_connection(), url, status, reason)


def reset_failed() -> int:
    return application_jobs_mod.reset_failed(get_connection())


def worker_loop(
    worker_id: int = 0,
    limit: int = 1,
    target_url: str | None = None,
    min_score: int = 6,
    headless: bool = False,
    model: str = "sonnet",
    dry_run: bool = False,
    agent_backend: str = "codex",
    manual_captcha_relay: bool = False,
    browser_backend: str = "edge",
    interaction_mode: str = "auto",
    authorization_manifest: dict | None = None,
    attempted_urls: set[str] | None = None,
) -> tuple[int, int]:
    return worker_orchestration_mod.worker_loop(
        sys.modules[__name__],
        worker_id=worker_id,
        limit=limit,
        target_url=target_url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        agent_backend=agent_backend,
        manual_captcha_relay=manual_captcha_relay,
        browser_backend=browser_backend,
        interaction_mode=interaction_mode,
        authorization_manifest=authorization_manifest,
        attempted_urls=attempted_urls,
    )

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()


def _open_bound_application_target(port: int, start_url: str) -> set[str]:
    """Create and navigate the exact CDP target owned by this application."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        if not browser.contexts:
            raise RuntimeError("Browser exposed no default context for application binding")
        page = browser.contexts[0].new_page()
        info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
            "targetInfo"
        ]
        target_id = str(info.get("targetId") or "")
        if not target_id:
            raise RuntimeError("Browser did not expose the new application target id")
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        return {target_id}

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()
_cloak_lane = threading.Semaphore(1)

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()


def _acquire_cloak_lane(worker_id: int) -> bool:
    """Acquire the single licensed Cloak lane while leaving Edge workers free."""
    if os.environ.get("APPLYPILOT_CLOAK_ALLOW_CONCURRENCY") == "1":
        return False
    update_state(worker_id, status="waiting", last_action="waiting for CloakBrowser lane")
    while not _stop_event.is_set():
        if _cloak_lane.acquire(timeout=0.5):
            return True
    raise InterruptedError("CloakBrowser lane wait interrupted")


def _route_for_phase(route: ControlRoute, phase: str, reason_code: str) -> ControlRoute:
    return ControlRoute(
        interaction_driver=route.interaction_driver,
        browser_runtime=route.browser_runtime,
        phase=phase,
        reason_code=reason_code,
    )


def _attach_control_contract(
    job: dict,
    route: ControlRoute,
    *,
    interaction_mode: str,
    resume_existing_page: bool,
) -> None:
    job["_browser_backend"] = route.browser_runtime
    job["_control_contract"] = prompt_control_contract(
        route,
        interaction_mode=interaction_mode,
        resume_existing_page=resume_existing_page,
    )


def _resolve_worker_count(
    requested: int,
    profile_cap: int,
    browser_backend: str,
    *,
    cloak_concurrency_allowed: bool,
) -> tuple[int, bool]:
    """Keep Edge/auto parallel while constraining explicit Cloak runs."""
    workers = min(max(1, requested), max(1, profile_cap), 3)
    reduced_for_cloak = (
        browser_backend == "cloak" and workers > 1 and not cloak_concurrency_allowed
    )
    return (1 if reduced_for_cloak else workers), reduced_for_cloak

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------













def _record_worker_action(worker_id: int, description: str) -> None:
    """Keep interactive and piped CLI runs visibly moving during browser work."""
    ws = get_state(worker_id)
    cur_actions = ws.actions if ws else 0
    update_state(
        worker_id,
        actions=cur_actions + 1,
        last_action=description[:35],
    )
    add_event(f"[W{worker_id}] {description[:80]}")
    if not sys.stderr.isatty():
        logger.info("[worker-%d] %s", worker_id, description)


def _submission_rate_status(
    conn, profile: dict, now: datetime | None = None
) -> tuple[bool, float, str]:
    """Return whether another submission may start and any short cooldown."""
    policy = profile.get("submission_policy", {})
    hourly_max = int(policy.get("maximum_verified_submissions_per_rolling_hour", 15))
    minimum_gap = float(policy.get("minimum_seconds_between_verified_submissions", 20))
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = (current - timedelta(hours=1)).isoformat()
    rows = conn.execute(
        "SELECT applied_at FROM jobs WHERE applied_at IS NOT NULL AND applied_at >= ? "
        "ORDER BY applied_at DESC",
        (cutoff,),
    ).fetchall()
    if hourly_max > 0 and len(rows) >= hourly_max:
        return False, 0.0, "rolling_hour_submission_cap"
    if rows and minimum_gap > 0:
        try:
            latest = datetime.fromisoformat(rows[0]["applied_at"])
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=UTC)
            remaining = minimum_gap - (current - latest).total_seconds()
            if remaining > 0:
                return True, remaining, "minimum_submission_gap"
        except (TypeError, ValueError):
            logger.warning("Ignoring an invalid applied_at timestamp for rate limiting")
    return True, 0.0, "ready"












def _archive_worker_evidence(
    worker_dir: Path,
    job: dict,
    worker_id: int,
    timestamp: str,
) -> list[Path]:
    """Preserve browser evidence before the next run resets the worker directory."""
    evidence_names = (
        "final-preview.png",
        "pre-submit-review.png",
        "submission-confirmation.png",
        "submission-confirmation-observer.png",
        "submission-confirmation-observer-attempt-2.png",
        "captcha-blocked.png",
    )
    sources = [worker_dir / name for name in evidence_names if (worker_dir / name).is_file()]
    if not sources:
        return []

    company = re.sub(r"[^\w.-]+", "_", str(job.get("company_name") or "unknown"))[:40]
    title = re.sub(r"[^\w.-]+", "_", str(job.get("title") or "job"))[:60]
    destination = (
        config.LOG_DIR
        / "application-evidence"
        / f"{timestamp}_w{worker_id}_{company}_{title}"
    )
    destination.mkdir(parents=True, exist_ok=True)
    archived: list[Path] = []
    for source in sources:
        target = destination / source.name
        shutil.copy2(source, target)
        archived.append(target)
    return archived
























def _reserve_manifest_submission(manifest: dict | None, job: dict) -> tuple[bool, str]:
    """Re-authorize current job bytes and atomically reserve the batch slot."""
    if manifest is None:
        return False, "authorization_manifest_required"
    try:
        expires_at = datetime.fromisoformat(str(manifest.get("expires_at") or ""))
        if expires_at.tzinfo is None or datetime.now(UTC) >= expires_at:
            return False, "authorization_manifest_expired"
        from applypilot.apply.authorization import authorize_job, freeze_submission_materials
        from applypilot.database import reserve_batch_submission

        if authorize_job(manifest, job) is None:
            return False, "authorization_manifest_job_mismatch"
        material_binding = freeze_submission_materials(job, config.load_profile())
        job["_bound_submission_materials"] = material_binding
        reserved = reserve_batch_submission(
            str(manifest.get("batch_id") or ""),
            str(job.get("url") or ""),
            int(manifest.get("max_submissions") or 0),
        )
        if reserved is not True:
            return False, "authorization_batch_reservation_denied"
        return True, "reserved"
    except Exception as exc:
        logger.exception("Batch submission reservation failed")
        return False, f"authorization_batch_reservation_error:{type(exc).__name__}"


def _update_submission_ledger(
    manifest: dict | None,
    job: dict,
    status: str,
    evidence: dict | None = None,
) -> bool:
    if manifest is None:
        return True
    try:
        from applypilot.database import update_batch_submission_status

        ledger_evidence = dict(evidence or {})
        if job.get("_bound_submission_materials"):
            ledger_evidence["material_binding"] = job["_bound_submission_materials"]
        update_batch_submission_status(
            str(manifest.get("batch_id") or ""),
            str(job.get("url") or ""),
            status,
            evidence=ledger_evidence,
        )
        return True
    except Exception:
        logger.exception("Batch submission ledger update failed")
        return False


def _wait_for_manual_captcha(
    port: int,
    worker_id: int,
    timeout_seconds: int | None = None,
    *,
    attempt_id: str | None = None,
    submit_started: bool = False,
    root_target_ids: set[str] | None = None,
    application_lease_minutes: int | None = None,
) -> bool:
    """Keep Edge alive until the applicant clears a visible verification gate."""
    from playwright.sync_api import sync_playwright

    if timeout_seconds is None:
        timeout_seconds = int(
            config.load_profile().get("submission_policy", {}).get(
                "manual_intervention_timeout_seconds", 1800
            )
        )
    if application_lease_minutes is None:
        application_lease_minutes = load_runtime_settings().application_lease_minutes
    timeout_seconds = max(60, min(timeout_seconds, 3600))
    grace_seconds = max(
        4,
        min(
            int(
                config.load_profile().get("submission_policy", {}).get(
                    "automatic_verification_grace_seconds", 12
                )
            ),
            30,
        ),
    )
    marker = config.LOG_DIR / f"manual-captcha-relay-worker-{worker_id}.json"
    marker.write_text(
        json.dumps(
            {
                "status": "observing_transient_verification",
                "port": port,
                "timeout_seconds": timeout_seconds,
                "automatic_grace_seconds": grace_seconds,
            }
        ),
        encoding="utf-8",
    )
    add_event(f"[W{worker_id}] Re-observing verification gate for {grace_seconds}s")
    update_state(worker_id, status="observing", last_action="checking transient verification")

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        browser_session = browser.new_browser_cdp_session()
        deadline = time.monotonic() + timeout_seconds
        started_waiting = time.monotonic()
        applicant_alerted = False
        clear_polls = 0
        last_lease_renewal = 0.0
        while time.monotonic() < deadline and not _stop_event.is_set():
            if attempt_id and time.monotonic() - last_lease_renewal >= 20:
                from applypilot.database import update_application_attempt

                if not update_application_attempt(
                    attempt_id,
                    phase="manual_verification",
                    submit_started=submit_started,
                    lease_minutes=application_lease_minutes,
                ):
                    marker.write_text(
                        json.dumps({"status": "attempt_lease_lost", "port": port}),
                        encoding="utf-8",
                    )
                    return False
                last_lease_renewal = time.monotonic()
            pages = [page for context in browser.contexts for page in context.pages]
            bound_pages = []
            target_infos = {
                str(info.get("targetId") or ""): info
                for info in browser_session.send("Target.getTargets").get("targetInfos", [])
                if info.get("targetId")
            }
            from applypilot.apply.credential_relay import _target_descends_from

            for page in pages:
                try:
                    info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
                        "targetInfo"
                    ]
                except Exception:  # noqa: BLE001, S112 - page can detach during navigation
                    continue
                target_id = str(info.get("targetId") or "")
                target_infos[target_id] = info
                if root_target_ids and _target_descends_from(
                    target_id, root_target_ids, target_infos
                ):
                    bound_pages.append(page)
            if not bound_pages:
                clear_polls = 0
                if _stop_event.wait(2):
                    break
                continue
            visible = any(_visible_verification_gate(page) for page in bound_pages)
            stable_clear_state = any(
                _verification_clear_state_stable(page) for page in bound_pages
            )
            if visible or not stable_clear_state:
                clear_polls = 0
                if (
                    not applicant_alerted
                    and time.monotonic() - started_waiting >= grace_seconds
                ):
                    applicant_alerted = True
                    marker.write_text(
                        json.dumps(
                            {
                                "status": "waiting_for_applicant",
                                "port": port,
                                "timeout_seconds": timeout_seconds,
                            }
                        ),
                        encoding="utf-8",
                    )
                    add_event(
                        f"[W{worker_id}] MANUAL VERIFICATION: Edge is waiting for the applicant"
                    )
                    update_state(
                        worker_id,
                        status="captcha",
                        last_action="waiting for manual verification",
                    )
                    try:
                        if platform.system() == "Windows":
                            import winsound

                            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                        else:
                            Console().print("\a", end="")
                    except Exception:
                        logger.debug(
                            "Could not emit manual-intervention alert", exc_info=True
                        )
            else:
                clear_polls += 1
                if clear_polls >= 2:
                    marker.write_text(
                        json.dumps(
                            {
                                "status": (
                                    "cleared_after_applicant_intervention"
                                    if applicant_alerted
                                    else "transient_gate_cleared"
                                ),
                                "port": port,
                            }
                        ),
                        encoding="utf-8",
                    )
                    add_event(f"[W{worker_id}] Manual verification cleared; resuming agent")
                    return True
            if _stop_event.wait(2):
                break
    except Exception as exc:
        logger.exception("Manual CAPTCHA relay failed")
        marker.write_text(
            json.dumps({"status": "relay_error", "error": str(exc)[:200]}),
            encoding="utf-8",
        )
        return False
    finally:
        playwright.stop()

    marker.write_text(
        json.dumps({"status": "timeout", "port": port}),
        encoding="utf-8",
    )
    return False













def _prepare_runtime_cover_letter(job: dict) -> dict:
    """Generate, validate, render, and approve one cover letter under standing policy."""
    policy = config.load_profile().get("submission_policy", {})
    if not policy.get("allow_agent_validated_cover_letter", False):
        raise PermissionError("Standing policy does not allow agent-validated cover letters")

    from applypilot.scoring.pdf import convert_to_pdf
    from applypilot.single_job import prepare_cover_letter_for_url

    text_path = Path(str(job.get("cover_letter_path") or ""))
    if job.get("cover_letter_status") != "machine_validated" or not text_path.is_file():
        report = prepare_cover_letter_for_url(
            str(job["url"]),
            str(job.get("company_name") or "").strip(),
            validation_mode="strict",
            resume_path=str(job.get("tailored_resume_path") or ""),
        )
        text_path = Path(str(report["text_path"]))
    pdf_path = text_path.with_suffix(".pdf")
    if not pdf_path.is_file():
        convert_to_pdf(text_path, output_path=pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"Validated cover-letter PDF was not rendered: {pdf_path}")

    conn = get_connection()
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE jobs SET cover_letter_path=?, cover_letter_status='agent_validated', "
        "cover_letter_error=NULL, cover_letter_approved_at=?, "
        "cover_letter_approved_by='standing_policy_agent' WHERE url=?",
        (str(text_path), now, job["url"]),
    )
    conn.commit()
    refreshed = conn.execute("SELECT * FROM jobs WHERE url=?", (job["url"],)).fetchone()
    if refreshed is None:
        raise ValueError("Exact job disappeared while preparing its cover letter")
    refreshed_job = dict(refreshed)
    refreshed_job.update({key: value for key, value in job.items() if key.startswith("_")})
    return refreshed_job


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 6,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(
        target_url=target_url,
        min_score=min_score,
        worker_id=worker_id,
        preview_only=True,
    )
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    worker_dir = reset_worker_dir(worker_id)
    prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=True,
        worker_id=worker_id,
        worker_dir=worker_dir,
    )

    # Release the lock so the job stays available
    release_lock(job["url"], job.get("_attempt_id"))

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("company_name") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def _credential_relay_allowed(profile: dict, job: dict) -> bool:
    authentication = profile.get("authentication", {})
    return bool(
        isinstance(authentication, dict)
        and authentication.get("ats_account_creation_authorized", False)
        and job.get("_browser_backend") != "cloak"
    )





def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            agent_backend: str = "codex",
            manual_captcha_relay: bool = False,
            resume_existing_page: bool = False,
            submission_phase: str = "submit") -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'submission_uncertain', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    agent_timeout_seconds = load_runtime_settings().agent_timeout_seconds
    profile = config.load_profile()
    authentication = profile.get("authentication", {})
    credential_relay_authorized = _credential_relay_allowed(profile, job)
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    if resume_existing_page:
        worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
        worker_dir.mkdir(parents=True, exist_ok=True)
    else:
        worker_dir = reset_worker_dir(worker_id)

    workspace_root = config.APP_DIR.parent
    secure_fill_script = workspace_root / "fill-ats-credentials.ps1"
    if credential_relay_authorized and secure_fill_script.is_file():
        shutil.copy2(secure_fill_script, worker_dir / secure_fill_script.name)

    # Build the prompt and stage attachments only inside this worker directory.
    job["_agent_backend"] = agent_backend
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        worker_id=worker_id,
        worker_dir=worker_dir,
        manual_captcha_relay=manual_captcha_relay,
        resume_existing_page=resume_existing_page,
        submission_phase=submission_phase,
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    cmd, final_message_path = _build_agent_command(
        backend=agent_backend,
        model=model,
        port=port,
        worker_dir=worker_dir,
        mcp_config_path=mcp_config_path,
        credential_relay_authorized=credential_relay_authorized,
    )
    if final_message_path and final_message_path.exists():
        final_message_path.unlink()

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env["APPLYPILOT_CDP_PORT"] = str(port)
    env["APPLYPILOT_WORKSPACE_ROOT"] = str(workspace_root)
    for key in (
        "APPLYPILOT_ATS_CREDENTIAL_FILE",
        "APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS",
        "APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS",
        "APPLYPILOT_CREDENTIAL_ALLOW_KNOWN_ATS_REDIRECT",
        "APPLYPILOT_CREDENTIAL_ROOT_TARGET_IDS",
        "APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED",
    ):
        env.pop(key, None)
    allowed_hosts = {
        (urlparse(str(url)).hostname or "").lower()
        for url in (job.get("url"), job.get("application_url"))
        if url
    }
    if credential_relay_authorized:
        configured_password_hosts = authentication.get(
            "ats_credential_allowed_hosts", []
        )
        if not isinstance(configured_password_hosts, list):
            configured_password_hosts = []
        env["APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED"] = "1"
        env["APPLYPILOT_ATS_CREDENTIAL_FILE"] = str(
            config.APP_DIR / "credentials" / "ats-signup.json"
        )
        env["APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS"] = ",".join(
            sorted(host for host in allowed_hosts if host)
        )
        env["APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS"] = ",".join(
            sorted(
                str(host).strip().casefold()
                for host in configured_password_hosts
                if str(host).strip()
            )
        )
        # Permit a legitimate employer-page -> known ATS redirect, but the relay
        # still requires exactly one eligible browser tab before filling anything.
        env["APPLYPILOT_CREDENTIAL_ALLOW_KNOWN_ATS_REDIRECT"] = "1"
        current_runtime = str(job.get("_browser_backend") or "")
        credential_root_ids = set(job.get("_browser_root_target_ids") or [])
        if job.get("_browser_root_runtime") != current_runtime:
            credential_root_ids = set()
        env["APPLYPILOT_CREDENTIAL_ROOT_TARGET_IDS"] = ",".join(
            sorted(credential_root_ids)
        )

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("company_name", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('company_name', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None
    watchdog: threading.Timer | None = None
    timed_out = threading.Event()

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        timed_out, watchdog = _start_timeout_watchdog(proc, agent_timeout_seconds)

        try:
            proc.stdin.write(agent_prompt)
            proc.stdin.close()
        except BrokenPipeError as exc:
            startup_output = proc.stdout.read() if proc.stdout else ""
            proc.wait(timeout=5)
            raise RuntimeError(
                "Agent exited before accepting the prompt: "
                + startup_output.strip()[:500]
            ) from exc

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                _record_worker_action(worker_id, desc)
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                    elif msg_type == "item.completed":
                        item = msg.get("item", {})
                        item_type = item.get("type")
                        if item_type == "agent_message":
                            text = item.get("text", "")
                            if text:
                                text_parts.append(text)
                                lf.write(text + "\n")
                        elif item_type in {"mcp_tool_call", "tool_call"}:
                            server = item.get("server", "playwright")
                            tool = item.get("tool", item.get("name", "tool"))
                            desc = f"{server}:{tool}"
                            lf.write(f"  >> {desc}\n")
                            _record_worker_action(worker_id, desc)
                    elif msg_type == "turn.completed":
                        usage = msg.get("usage", {})
                        stats = {
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                            "cache_read": usage.get("cached_input_tokens", 0),
                            "cache_create": usage.get("cache_write_input_tokens", 0),
                            "cost_usd": 0,
                            "turns": 1,
                        }
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        if watchdog is not None:
            watchdog.cancel()
        proc.wait(timeout=5)
        returncode = proc.returncode
        proc = None

        if timed_out.is_set():
            duration_ms = int((time.time() - start) * 1000)
            elapsed = int(time.time() - start)
            add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
            uncertain = submission_phase == "submit" and not dry_run
            status = "submission_uncertain" if uncertain else "failed"
            update_state(worker_id, status=status, last_action=f"TIMEOUT ({elapsed}s)")
            return ("submission_uncertain" if uncertain else "failed:timeout"), duration_ms

        if returncode and returncode < 0:
            status = "submission_uncertain" if submission_phase == "submit" and not dry_run else "skipped"
            return status, int((time.time() - start) * 1000)

        if final_message_path and final_message_path.exists():
            final_text = final_message_path.read_text(encoding="utf-8").strip()
            if final_text and final_text not in text_parts:
                text_parts.append(final_text)
        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        unanswered = _parse_unanswered_questions(output)
        if unanswered is not None:
            from applypilot.database import record_unanswered_questions
            record_unanswered_questions(job["url"], unanswered)

        ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"agent_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")
        archived_evidence = _archive_worker_evidence(worker_dir, job, worker_id, ts)
        if archived_evidence:
            logger.info(
                "[worker-%d] Archived %d browser evidence file(s) to %s",
                worker_id,
                len(archived_evidence),
                archived_evidence[0].parent,
            )

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        auth_markers = (
            "failed to authenticate",
            "oauth access token has been revoked",
            "not logged in",
            "unauthorized",
        )
        if any(marker in output.casefold() for marker in auth_markers):
            add_event(f"[W{worker_id}] AUTHENTICATION FAILED ({elapsed}s)")
            uncertain = submission_phase == "submit" and not dry_run
            status = "submission_uncertain" if uncertain else "failed:authentication"
            update_state(
                worker_id,
                status="submission_uncertain" if uncertain else "failed",
                last_action="authentication failed",
            )
            return status, duration_ms

        status, evidence = _interpret_agent_output(
            output,
            dry_run=dry_run,
            submission_phase=submission_phase,
        )
        failure_context = _parse_failure_context(output)
        if failure_context is not None:
            job["_failure_context"] = failure_context
        if evidence is not None:
            job["_agent_submission_evidence"] = evidence
        display_status = status.split(":", 1)[0]
        add_event(f"[W{worker_id}] {display_status.upper()} ({elapsed}s): {job['title'][:30]}")
        update_state(
            worker_id,
            status=display_status,
            last_action=f"{display_status.upper()} ({elapsed}s)",
        )
        return status, duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        uncertain = submission_phase == "submit" and not dry_run
        update_state(
            worker_id,
            status="submission_uncertain" if uncertain else "failed",
            last_action=f"TIMEOUT ({elapsed}s)",
        )
        return ("submission_uncertain" if uncertain else "failed:timeout"), duration_ms
    except Exception as e:  # noqa: BLE001
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        uncertain = submission_phase == "submit" and not dry_run
        update_state(
            worker_id,
            status="submission_uncertain" if uncertain else "failed",
            last_action=f"ERROR: {str(e)[:25]}",
        )
        return (
            "submission_uncertain" if uncertain else f"failed:{str(e)[:100]}"
        ), duration_ms
    finally:
        if watchdog is not None:
            watchdog.cancel()
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired",
    "not_eligible_location", "not_eligible_salary",
    "already_applied",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
    "assessment", "assessment_required",
}

PERMANENT_PREFIXES: tuple[str, ...] = (
    "site_blocked", "cloudflare", "blocked_by",
    "manual_review_required:submission_validation",
    "assessment",
    "unsafe_verification",
)

def _should_retry_with_cloak(result: str, requested_backend: str) -> bool:
    """Compatibility wrapper for the structured pre-submit route policy."""
    return cloak_fallback_route(
        result,
        requested_browser_backend=requested_backend,
        phase="prepare",
        current_runtime="edge",
        fallback_already_used=False,
    ) is not None


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 6, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         agent_backend: str = "codex",
         manual_captcha_relay: bool = False,
         browser_backend: str = "edge",
         interaction_mode: str = "auto",
         authorization_manifest: dict | None = None) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Confirmed submissions to achieve (preview jobs for dry-run); 0 or continuous=True runs forever.
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()
    requested_browser_backend = resolve_browser_backend(browser_backend)
    requested_interaction_mode = resolve_interaction_mode(interaction_mode)

    if not dry_run and authorization_manifest is None:
        raise ValueError("Every real submission requires an authorization manifest.")

    submission_policy = config.load_profile().get("submission_policy", {})
    if (
        not dry_run
        and submission_policy.get("batch_final_authorization_required", False)
        and not authorization_manifest.get("_final_submission_authorized", False)
    ):
        raise ValueError(
            "One final batch authorization is required before browser submission."
        )

    profile_worker_cap = int(submission_policy.get("maximum_workers", 1))
    workers, reduced_for_cloak = _resolve_worker_count(
        workers,
        profile_worker_cap,
        requested_browser_backend,
        cloak_concurrency_allowed=(
            os.environ.get("APPLYPILOT_CLOAK_ALLOW_CONCURRENCY") == "1"
        ),
    )
    if reduced_for_cloak:
        console.print(
            "[yellow]CloakBrowser defaults to one worker; set "
            "APPLYPILOT_CLOAK_ALLOW_CONCURRENCY=1 only when the license permits it.[/yellow]"
        )
        workers = 1

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        run_cap = int(
            config.load_profile().get("submission_policy", {}).get(
                "maximum_verified_submissions_per_run", 12
            )
        )
        effective_limit = min(limit, run_cap) if run_cap > 0 else limit
        mode_label = f"{limit} confirmed submissions"
        if effective_limit != limit:
            mode_label = f"{effective_limit} confirmed submissions (profile cap)"

    if authorization_manifest is not None:
        manifest_cap = int(authorization_manifest.get("max_submissions", 0))
        if manifest_cap <= 0:
            raise ValueError("Authorization manifest has no positive submission allowance.")
        if effective_limit == 0:
            effective_limit = manifest_cap
        else:
            effective_limit = min(effective_limit, manifest_cap)
        mode_label = f"{effective_limit} manifest-authorized confirmed submissions"

    # Initialize dashboard for all workers
    attempted_urls: set[str] = set()
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(
        f"Launching apply pipeline ({mode_label}, {worker_label}, "
        f"browser={requested_browser_backend}, interaction={requested_interaction_mode}, "
        f"poll every {POLL_INTERVAL}s)..."
    )
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    agent_backend=agent_backend,
                    manual_captcha_relay=manual_captcha_relay,
                    browser_backend=requested_browser_backend,
                    interaction_mode=requested_interaction_mode,
                    authorization_manifest=authorization_manifest,
                    attempted_urls=attempted_urls,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            browser_backend=requested_browser_backend,
                            interaction_mode=requested_interaction_mode,
                            authorization_manifest=authorization_manifest,
                            attempted_urls=attempted_urls,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
