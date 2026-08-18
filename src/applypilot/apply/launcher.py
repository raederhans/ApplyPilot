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
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    _kill_process_tree,
    cleanup_on_exit,
    cleanup_worker,
    kill_all_chrome,
    launch_chrome,
    reset_worker_dir,
)
from applypilot.apply.dashboard import (
    add_event,
    get_state,
    get_totals,
    init_worker,
    render_full,
    update_state,
)
from applypilot.database import get_connection

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()
AGENT_TIMEOUT_SECONDS = int(os.environ.get("APPLYPILOT_AGENT_TIMEOUT_SECONDS", "300"))

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


def _resolve_claude_command() -> list[str]:
    """Resolve the Claude CLI to a directly executable command.

    npm exposes extensionless and .cmd shims on Windows. ``CreateProcess``
    cannot execute the extensionless POSIX shim, even though ``shutil.which``
    reports it as available. Current Claude Code npm installs include a native
    executable beside the shim; prefer it and fall back to ``cmd /c`` only for
    older layouts.
    """
    candidate = shutil.which("claude")
    if platform.system() != "Windows":
        if not candidate:
            raise FileNotFoundError("Claude Code CLI was not found on PATH.")
        return [candidate]

    cmd_shim = shutil.which("claude.cmd")
    shim = Path(cmd_shim or candidate or "")
    if shim:
        native = shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.is_file():
            return [str(native)]

    if cmd_shim:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", cmd_shim]
    raise FileNotFoundError("Claude Code CLI was not found on PATH.")


def _resolve_codex_command() -> list[str]:
    """Resolve Codex to a native executable suitable for ``Popen``."""
    if platform.system() == "Windows":
        cmd_shim = shutil.which("codex.cmd")
        if cmd_shim:
            npm_root = Path(cmd_shim).parent / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
            native_candidates = sorted(npm_root.glob("codex-win32-*/vendor/*/bin/codex.exe"))
            if native_candidates:
                return [str(native_candidates[0])]
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", cmd_shim]
        native = shutil.which("codex.exe")
        if native:
            return [native]
    else:
        native = shutil.which("codex")
        if native:
            return [native]
    raise FileNotFoundError("Codex CLI was not found on PATH.")


def _toml_value(value: object) -> str:
    """Encode JSON-compatible values accepted by Codex's TOML overrides."""
    return json.dumps(value, ensure_ascii=False)


def _toml_skill_config(paths: list[Path]) -> str:
    """Encode Codex's array-of-tables skill override without stringifying it."""
    entries = ",".join(
        f"{{path={_toml_value(str(path))},enabled=false}}" for path in paths
    )
    return f"[{entries}]"


def _start_timeout_watchdog(
    proc: subprocess.Popen, timeout_seconds: float
) -> tuple[threading.Event, threading.Timer]:
    """Kill an agent that does not reach EOF before its wall-clock deadline."""
    timed_out = threading.Event()

    def terminate_if_running() -> None:
        if proc.poll() is None:
            timed_out.set()
            _kill_process_tree(proc.pid)

    timer = threading.Timer(timeout_seconds, terminate_if_running)
    timer.daemon = True
    timer.start()
    return timed_out, timer


def _validate_preview_audit(output: str) -> str | None:
    """Return a failure reason when a PREVIEWED result lacks safety evidence."""
    marker = re.search(r"PREVIEW_AUDIT\s*:?\s*", output)
    if marker:
        payload = output[marker.end():]
    else:
        result_marker = re.search(r"RESULT:PREVIEWED\b", output)
        if not result_marker:
            return "preview_audit_missing"
        payload = output[result_marker.end():]
    object_start = payload.find("{")
    if object_start < 0:
        return "preview_audit_missing" if not marker else "preview_audit_invalid_json"
    try:
        audit, _ = json.JSONDecoder().raw_decode(payload[object_start:])
    except (json.JSONDecodeError, TypeError):
        return "preview_audit_invalid_json"
    if not isinstance(audit, dict):
        return "preview_audit_not_object"
    if audit.get("submission_attempted") is not False:
        return "preview_submission_state_unsafe"
    if audit.get("resume_uploaded") is not True:
        return "preview_resume_not_verified"
    if not isinstance(audit.get("filled_fields"), (list, dict)):
        return "preview_filled_fields_missing"
    if not isinstance(audit.get("manual_review_fields"), list):
        return "preview_manual_review_fields_missing"
    if not str(audit.get("final_control_label", "")).strip():
        return "preview_final_control_missing"
    return None


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


def _visible_captcha_overlay(page) -> bool:
    """Detect a user-visible CAPTCHA challenge without reading its contents."""
    for iframe in page.locator("iframe").all():
        try:
            title = (iframe.get_attribute("title") or "").casefold()
            source = (iframe.get_attribute("src") or "").casefold()
            box = iframe.bounding_box()
            if (
                box
                and box["width"] >= 200
                and box["height"] >= 150
                and ("captcha" in title or "captcha" in source)
            ):
                return True
        except Exception:
            logger.debug("Unable to inspect a CAPTCHA iframe", exc_info=True)
            continue
    return False


def _captcha_response_present(page) -> bool:
    """Return true after the applicant has produced a CAPTCHA response token."""
    selector = (
        'textarea[name*="captcha" i], textarea[name*="recaptcha" i], '
        'input[name*="captcha" i], input[name*="recaptcha" i]'
    )
    try:
        response_fields = page.locator(selector).all()
    except Exception:
        logger.debug("Unable to enumerate CAPTCHA response fields", exc_info=True)
        return False
    for field in response_fields:
        try:
            if (field.input_value(timeout=500) or "").strip():
                return True
        except Exception:
            logger.debug("Unable to read a CAPTCHA response field", exc_info=True)
            continue
    return False


def _validate_pre_submit_snapshot(snapshot: dict, profile: dict, job: dict) -> list[str]:
    """Validate a browser snapshot before any final application submission."""
    issues: list[str] = []
    expected_url = job.get("application_url") or job.get("url") or ""
    actual_url = snapshot.get("url", "")
    if expected_url and actual_url:
        expected = urlparse(expected_url)
        actual = urlparse(actual_url)
        expected_path = expected.path.rstrip("/").removesuffix("/apply")
        actual_path = actual.path.rstrip("/").removesuffix("/apply")
        if expected.netloc.casefold() != actual.netloc.casefold() or expected_path != actual_path:
            issues.append("unexpected_application_url")

    if snapshot.get("captcha_visible") and not snapshot.get("captcha_token_present"):
        issues.append("visible_captcha")

    issues.extend(
        f"required_field_empty:{label[:80]}"
        for label in snapshot.get("required_unfilled", [])
    )

    if snapshot.get("resume_field_present") and not snapshot.get("resume_uploaded"):
        issues.append("resume_not_uploaded")

    personal = profile.get("personal", {})
    legal_name = personal.get("full_name", "").strip().casefold()
    for value in snapshot.get("full_name_values", []):
        if legal_name and value.strip().casefold() != legal_name:
            issues.append("legal_name_mismatch")
            break

    for value in snapshot.get("current_location_values", []):
        if "singapore" not in value.strip().casefold():
            issues.append("current_location_not_singapore")
            break

    screening = profile.get("screening", {})
    hard_answers = {
        "starting_september": screening.get(
            "available_for_full_time_3_6_month_internship_starting_september"
        ),
        "startup_internship": screening.get(
            "prior_internship_product_startup_logistics_ecommerce_b2b_saas"
        ),
    }
    for question in snapshot.get("radio_questions", []):
        text = question.get("text", "").casefold()
        selected = question.get("selected", "").strip().casefold()
        expected: bool | None = None
        key = ""
        if "starting september" in text and "full-time" in text:
            key = "starting_september"
            expected = hard_answers[key]
        elif (
            "prior internship" in text
            and "product-based startup" in text
            and any(term in text for term in ("logistics", "ecommerce", "b2b saas"))
        ):
            key = "startup_internship"
            expected = hard_answers[key]
        if expected is not None and selected != ("yes" if expected else "no"):
            issues.append(f"hard_answer_mismatch:{key}")

    for field in snapshot.get("select_fields", []):
        text = field.get("text", "").casefold()
        selected = field.get("selected", "").strip().casefold()
        if "currently based" in text and "legal right to work" in text and selected != "singapore":
            issues.append("work_location_selection_not_singapore")

    if snapshot.get("submit_control_count", 0) < 1:
        issues.append("submit_control_missing")
    return list(dict.fromkeys(issues))


def _audit_live_pre_submit_page(
    port: int, worker_id: int, job: dict
) -> tuple[str | None, dict]:
    """Read and validate the visible application form without changing it."""
    from playwright.sync_api import sync_playwright

    profile = config.load_profile()
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        if not pages:
            return "pre_submit_audit:no_page", {}
        page = pages[-1]
        snapshot = page.evaluate(
            r"""() => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  el.getClientRects().length > 0 && !el.disabled;
              };
              const context = (el) => el.closest(
                'li, fieldset, [data-qa*="field"], [class*="application-field"], [class*="question"]'
              ) || el.parentElement;
              const labelText = (el) => {
                const node = context(el);
                return ((node && node.innerText) || el.getAttribute('aria-label') || el.name || '')
                  .replace(/\s+/g, ' ').trim().slice(0, 500);
              };
              const required = (el) => el.required || /[✱*]/.test(labelText(el));
              const responseSelector =
                'textarea[name*="captcha" i],textarea[name*="recaptcha" i],input[name*="captcha" i],input[name*="recaptcha" i]';
              const responseFields = [...document.querySelectorAll(responseSelector)];
              const inputs = [...document.querySelectorAll(
                'input:not([type=hidden]):not([type=radio]):not([type=checkbox]):not([type=file]):not([type=submit]):not([type=button]), textarea, select'
              )].filter((el) => visible(el) && !el.matches(responseSelector));
              const requiredUnfilled = [];
              const fullNameValues = [];
              const currentLocationValues = [];
              const selectFields = [];
              for (const el of inputs) {
                const text = labelText(el);
                const value = el.tagName === 'SELECT'
                  ? (el.selectedOptions[0] ? el.selectedOptions[0].textContent.trim() : '')
                  : (el.value || '').trim();
                if (required(el) && (!value || /^(select|choose)(\.\.\.)?$/i.test(value))) {
                  requiredUnfilled.push(text);
                }
                if (/\b(full|legal) name\b/i.test(text) && !/preferred|display/i.test(text)) {
                  fullNameValues.push(value);
                }
                if (/current location/i.test(text)) currentLocationValues.push(value);
                if (el.tagName === 'SELECT') selectFields.push({text, selected: value});
              }
              const fileFields = [...document.querySelectorAll('input[type=file]')]
                .map((el) => ({
                  text: labelText(el),
                  count: el.files ? el.files.length : 0
                }));
              const resumeFields = fileFields.filter((f) => /resume|cv/i.test(f.text));
              const resumeUploaded = resumeFields.some((f) =>
                f.count > 0 || /success|uploaded|replace|remove|\.pdf/i.test(f.text)
              );
              const radios = [...document.querySelectorAll('input[type=radio]')].filter(visible);
              const seen = new Set();
              const radioQuestions = [];
              for (const radio of radios) {
                const node = context(radio);
                const key = radio.name || (node ? node.innerText : '') || String(radioQuestions.length);
                if (seen.has(key)) continue;
                seen.add(key);
                const group = node ? [...node.querySelectorAll('input[type=radio]')] : [radio];
                const checked = group.find((item) => item.checked);
                let selected = '';
                if (checked) {
                  const checkedLabel = checked.closest('label') || checked.parentElement;
                  selected = ((checkedLabel && checkedLabel.innerText) || checked.value || '').trim();
                }
                const text = labelText(radio);
                if (required(radio) && !checked) requiredUnfilled.push(text);
                radioQuestions.push({text, selected});
              }
              const submitControls = [...document.querySelectorAll('button,input[type=submit]')]
                .filter((el) => visible(el) && /submit|send application|finish|complete application/i.test(
                  (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
                ));
              const captchaVisible = [...document.querySelectorAll('iframe')].some((el) => {
                const rect = el.getBoundingClientRect();
                const marker = `${el.title || ''} ${el.src || ''}`.toLowerCase();
                return rect.width >= 200 && rect.height >= 150 && marker.includes('captcha');
              });
              return {
                url: location.href,
                required_unfilled: requiredUnfilled,
                resume_field_present: resumeFields.length > 0,
                resume_uploaded: resumeUploaded,
                full_name_values: fullNameValues,
                current_location_values: currentLocationValues,
                select_fields: selectFields,
                radio_questions: radioQuestions,
                submit_control_count: submitControls.length,
                captcha_visible: captchaVisible,
                captcha_token_present: responseFields.some((el) => (el.value || '').trim().length > 0)
              };
            }"""
        )
        issues = _validate_pre_submit_snapshot(snapshot, profile, job)
        report = {
            "status": "passed" if not issues else "failed",
            "issues": issues,
            "required_unfilled_count": len(snapshot.get("required_unfilled", [])),
            "resume_field_present": snapshot.get("resume_field_present", False),
            "resume_uploaded": snapshot.get("resume_uploaded", False),
            "submit_control_count": snapshot.get("submit_control_count", 0),
            "captcha_token_present": snapshot.get("captcha_token_present", False),
        }
        report_path = (
            config.APPLY_WORKER_DIR / f"worker-{worker_id}" / "pre-submit-audit.json"
        )
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if "visible_captcha" in issues:
            return "visible_captcha", report
        if issues:
            return "pre_submit_audit:" + ",".join(issues[:5]), report
        return None, report
    except Exception as exc:
        logger.exception("Pre-submit browser audit failed")
        return f"pre_submit_audit_error:{type(exc).__name__}", {}
    finally:
        playwright.stop()


def _wait_for_manual_captcha(
    port: int, worker_id: int, timeout_seconds: int = 600
) -> bool:
    """Keep Edge alive until the applicant clears the visible CAPTCHA."""
    from playwright.sync_api import sync_playwright

    marker = config.LOG_DIR / f"manual-captcha-relay-worker-{worker_id}.json"
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
    add_event(f"[W{worker_id}] MANUAL CAPTCHA: Edge is waiting for the applicant")
    update_state(worker_id, status="captcha", last_action="waiting for manual CAPTCHA")

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        deadline = time.monotonic() + timeout_seconds
        clear_polls = 0
        while time.monotonic() < deadline and not _stop_event.is_set():
            pages = [page for context in browser.contexts for page in context.pages]
            solved = any(_captcha_response_present(page) for page in pages)
            visible = not solved and any(_visible_captcha_overlay(page) for page in pages)
            if visible:
                clear_polls = 0
            else:
                clear_polls += 1
                if clear_polls >= 2:
                    marker.write_text(
                        json.dumps({"status": "solved_by_applicant", "port": port}),
                        encoding="utf-8",
                    )
                    add_event(f"[W{worker_id}] Manual CAPTCHA cleared; resuming agent")
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


def _build_agent_command(
    backend: str,
    model: str,
    port: int,
    worker_dir: Path,
    mcp_config_path: Path,
) -> tuple[list[str], Path | None]:
    """Build an isolated browser-agent command for Claude or Codex."""
    if backend == "claude":
        return (
            _resolve_claude_command()
            + [
                "--model", model,
                "-p",
                "--mcp-config", str(mcp_config_path),
                "--permission-mode", "bypassPermissions",
                "--no-session-persistence",
                "--disallowedTools",
                (
                    "mcp__gmail__draft_email,mcp__gmail__modify_email,"
                    "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
                    "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
                    "mcp__gmail__create_label,mcp__gmail__update_label,"
                    "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
                    "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
                    "mcp__gmail__list_filters,mcp__gmail__get_filter,"
                    "mcp__gmail__delete_filter"
                ),
                "--output-format", "stream-json",
                "--verbose", "-",
            ],
            None,
        )

    if backend != "codex":
        raise ValueError("agent backend must be 'codex' or 'claude'.")

    final_message_path = worker_dir / "codex-final-message.txt"
    enabled_tools = [
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_fill_form",
        "browser_file_upload",
        "browser_tabs",
        "browser_take_screenshot",
        "browser_select_option",
        "browser_type",
        "browser_wait_for",
    ]
    playwright_args = [
        "/d",
        "/s",
        "/c",
        "npx",
        "-y",
        "@playwright/mcp@latest",
        f"--cdp-endpoint=http://localhost:{port}",
        f"--viewport-size={config.DEFAULTS['viewport']}",
    ]
    disabled_skills = [
        Path.home() / ".codex" / "skills" / "gstack-browse",
        Path.home() / ".codex" / "skills" / "playwright",
    ]
    command = _resolve_codex_command() + [
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--model", model,
        "-c", 'model_reasoning_effort="high"',
        "-c", "features.shell_tool=false",
        "-c", "features.skill_mcp_dependency_install=false",
        "-c", 'web_search="disabled"',
        "-c", f"skills.config={_toml_skill_config(disabled_skills)}",
        "-c", f"mcp_servers.playwright.command={_toml_value('cmd.exe')}",
        "-c", f"mcp_servers.playwright.args={_toml_value(playwright_args)}",
        "-c", "mcp_servers.playwright.required=true",
        "-c", "mcp_servers.playwright.startup_timeout_sec=60",
        "-c", "mcp_servers.playwright.tool_timeout_sec=90",
        "-c", f"mcp_servers.playwright.enabled_tools={_toml_value(enabled_tools)}",
        "-c", 'mcp_servers.playwright.default_tools_approval_mode="approve"',
        "--json",
        "--output-last-message", str(final_message_path),
        "-C", str(worker_dir),
        "-",
    ]
    return command, final_message_path


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, preview_only: bool = False) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility
    refresh_job_eligibility(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            material_clause = """
                  AND tailored_resume_path IS NOT NULL
                  AND tailor_status = 'machine_validated'
            """
            if not preview_only:
                material_clause += """
                  AND (
                    (cover_letter_path IS NOT NULL AND cover_letter_status = 'human_approved')
                    OR cover_letter_status = 'not_required'
                  )
                """
            row = conn.execute(f"""
                SELECT url, title, company_name, source_site, site, application_url,
                       tailored_resume_path, tailor_status, fit_score, location, full_description,
                       cover_letter_path, cover_letter_status
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  {material_clause}
                  AND (apply_status IS NULL OR apply_status IN ('failed', 'previewed'))
                  AND eligibility_status != 'ineligible'
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = [min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            row = conn.execute(f"""
                SELECT url, title, company_name, source_site, site, application_url,
                       tailored_resume_path, tailor_status, fit_score, location, full_description,
                       cover_letter_path, cover_letter_status
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND tailor_status = 'machine_validated'
                  AND (
                    (cover_letter_path IS NOT NULL AND cover_letter_status = 'human_approved')
                    OR cover_letter_status = 'not_required'
                  )
                  AND (apply_status IS NULL OR apply_status IN ('failed', 'previewed'))
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND COALESCE(apply_retry_blocked, 0) = 0
                  AND fit_score >= ?
                  AND {ELIGIBLE_SQL}
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, url
                LIMIT 1
            """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchone()

        if not row:
            conn.rollback()
            return None

        apply_url = row["application_url"] or row["url"]
        portal_gate = config.portal_application_gate(
            apply_url,
            source_site=row["source_site"],
            site=row["site"],
            preview_only=preview_only,
        )
        if portal_gate:
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = ? WHERE url = ?",
                (portal_gate, row["url"]),
            )
            conn.commit()
            logger.info("Portal policy paused browser application: %s", row["url"][:80])
            return None

        if (
            target_url
            and not preview_only
            and os.environ.get("APPLYPILOT_AUTO_SUBMIT") == "1"
        ):
            policy_min_score = int(
                os.environ.get("APPLYPILOT_AUTO_SUBMIT_MIN_SCORE", "8")
            )
            auto_issue = None
            if not str(row["company_name"] or "").strip():
                auto_issue = "missing verified company"
            elif not str(row["full_description"] or "").strip():
                auto_issue = "missing enriched job description"
            elif row["fit_score"] is None or int(row["fit_score"]) < policy_min_score:
                auto_issue = (
                    f"fit score {row['fit_score']} is below automatic minimum "
                    f"{policy_min_score}"
                )
            if auto_issue:
                conn.rollback()
                logger.warning("Automatic submission paused for %s: %s", row["url"], auto_issue)
                return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(UTC).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(UTC).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_retry_blocked = 0, apply_retry_reason = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    elif status == "previewed":
        conn.execute("""
            UPDATE jobs SET apply_status = 'previewed', applied_at = NULL,
                           apply_error = NULL, agent_id = NULL,
                           apply_retry_blocked = 0, apply_retry_reason = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (duration_ms, task_id, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = COALESCE(apply_attempts, 0) + 1,
                           apply_retry_blocked = ?, apply_retry_reason = ?,
                           agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (
            status,
            error or "unknown",
            1 if permanent else 0,
            (error or "unknown") if permanent else None,
            duration_ms,
            task_id,
            url,
        ))
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
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
    release_lock(job["url"])

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


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(UTC).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_retry_blocked = 0, apply_retry_reason = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_retry_blocked = 1, apply_retry_reason = ?,
                           agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, apply_retry_blocked = 0,
                       apply_retry_reason = NULL, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            agent_backend: str = "claude",
            manual_captcha_relay: bool = False,
            resume_existing_page: bool = False,
            submission_phase: str = "submit") -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
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
    if secure_fill_script.is_file():
        shutil.copy2(secure_fill_script, worker_dir / secure_fill_script.name)

    # Build the prompt and stage attachments only inside this worker directory.
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
    )
    if final_message_path and final_message_path.exists():
        final_message_path.unlink()

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env["APPLYPILOT_CDP_PORT"] = str(port)
    env["APPLYPILOT_WORKSPACE_ROOT"] = str(workspace_root)
    env["APPLYPILOT_ATS_CREDENTIAL_FILE"] = str(
        config.APP_DIR / "credentials" / "ats-signup.json"
    )
    allowed_hosts = {
        (urlparse(str(url)).hostname or "").lower()
        for url in (job.get("url"), job.get("application_url"))
        if url
    }
    env["APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS"] = ",".join(
        sorted(host for host in allowed_hosts if host)
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

        timed_out, watchdog = _start_timeout_watchdog(proc, AGENT_TIMEOUT_SECONDS)

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
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
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
                            ws = get_state(worker_id)
                            cur_actions = ws.actions if ws else 0
                            update_state(
                                worker_id,
                                actions=cur_actions + 1,
                                last_action=desc[:35],
                            )
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
            update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
            return "failed:timeout", duration_ms

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000)

        if final_message_path and final_message_path.exists():
            final_text = final_message_path.read_text(encoding="utf-8").strip()
            if final_text and final_text not in text_parts:
                text_parts.append(final_text)
        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

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

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        auth_markers = (
            "failed to authenticate",
            "oauth access token has been revoked",
            "not logged in",
            "unauthorized",
        )
        if any(marker in output.casefold() for marker in auth_markers):
            add_event(f"[W{worker_id}] AUTHENTICATION FAILED ({elapsed}s)")
            update_state(worker_id, status="failed", last_action="authentication failed")
            return "failed:authentication", duration_ms

        for result_status in [
            "READY_TO_SUBMIT",
            "PREVIEWED",
            "APPLIED",
            "EXPIRED",
            "CAPTCHA",
            "LOGIN_ISSUE",
        ]:
            if f"RESULT:{result_status}" in output:
                if result_status == "PREVIEWED" and dry_run:
                    audit_error = _validate_preview_audit(output)
                    if audit_error:
                        add_event(f"[W{worker_id}] FAILED ({elapsed}s): {audit_error[:30]}")
                        update_state(
                            worker_id,
                            status="failed",
                            last_action=f"FAILED: {audit_error[:25]}",
                        )
                        return f"failed:{audit_error}", duration_ms
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(),
                             last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms

        if "RESULT:FAILED" in output:
            for out_line in output.split("\n"):
                if "RESULT:FAILED" in out_line:
                    reason = (
                        out_line.split("RESULT:FAILED:")[-1].strip()
                        if ":" in out_line[out_line.index("FAILED") + 6:]
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms
            return "failed:unknown", duration_ms

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms
    except Exception as e:  # noqa: BLE001
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms
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
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


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

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                agent_backend: str = "claude",
                manual_captcha_relay: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(
            target_url=target_url,
            min_score=min_score,
            worker_id=worker_id,
            preview_only=dry_run,
        )
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            submission_phase = "submit" if dry_run else "prepare"
            result, duration_ms = run_job(
                job,
                port=port,
                worker_id=worker_id,
                model=model,
                dry_run=dry_run,
                agent_backend=agent_backend,
                manual_captcha_relay=manual_captcha_relay,
                submission_phase=submission_phase,
            )

            relay_round = 0
            while True:
                if result == "captcha" and manual_captcha_relay and relay_round < 3:
                    relay_round += 1
                    evidence_dir = (
                        config.LOG_DIR
                        / f"captcha-relay-{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}-r{relay_round}"
                    )
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    worker_artifacts = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
                    for artifact in worker_artifacts.glob("*.png"):
                        shutil.copy2(artifact, evidence_dir / artifact.name)
                    if not _wait_for_manual_captcha(port, worker_id):
                        result = "failed:manual_captcha_timeout"
                        break
                    result, resumed_duration = run_job(
                        job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=dry_run,
                        agent_backend=agent_backend,
                        manual_captcha_relay=True,
                        resume_existing_page=True,
                        submission_phase=submission_phase,
                    )
                    duration_ms += resumed_duration
                    continue

                if result == "captcha" and relay_round >= 3:
                    result = "failed:manual_captcha_relay_limit"
                    break

                if result == "ready_to_submit" and not dry_run:
                    audit_error, _audit_report = _audit_live_pre_submit_page(
                        port, worker_id, job
                    )
                    if audit_error == "visible_captcha":
                        result = "captcha"
                        continue
                    if audit_error:
                        result = f"failed:{audit_error}"
                        break
                    add_event(f"[W{worker_id}] PRE-SUBMIT AUDIT PASSED")
                    update_state(
                        worker_id,
                        status="audited",
                        last_action="pre-submit audit passed",
                    )
                    submission_phase = "submit"
                    result, submit_duration = run_job(
                        job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=False,
                        agent_backend=agent_backend,
                        manual_captcha_relay=manual_captcha_relay,
                        resume_existing_page=True,
                        submission_phase="submit",
                    )
                    duration_ms += submit_duration
                    continue
                break

            if dry_run and result == "applied":
                mark_result(
                    job["url"],
                    "failed",
                    "preview_agent_reported_submission_manual_verification_required",
                    duration_ms=duration_ms,
                )
                failed += 1
                update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed)
            elif result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            elif result == "previewed":
                mark_result(job["url"], "previewed", duration_ms=duration_ms)
                update_state(worker_id, jobs_done=applied + failed + 1)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         agent_backend: str = "claude",
         manual_captcha_relay: bool = False) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
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

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
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
