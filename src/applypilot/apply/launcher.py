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
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    _kill_process_tree,
    cleanup_on_exit,
    cleanup_worker,
    kill_all_chrome,
    launch_chrome,
    reset_worker_dir,
    resolve_browser_backend,
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


def _validate_submission_evidence(output: str) -> dict | None:
    """Return structured visible-confirmation evidence, or fail closed."""
    marker = re.search(r"SUBMISSION_EVIDENCE\s*:?\s*", output)
    if not marker:
        return None
    payload = output[marker.end():].lstrip()
    try:
        evidence, _ = json.JSONDecoder().raw_decode(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(evidence, dict):
        return None
    receipt_visible = evidence.get("receipt_visible") is True
    applied_badge_visible = evidence.get("applied_badge_visible") is True
    confirmation_text = str(evidence.get("confirmation_text") or "").strip()
    confirmation_url = evidence.get("confirmation_url")
    if not (receipt_visible or applied_badge_visible) or not confirmation_text:
        return None
    if confirmation_url is not None and not isinstance(confirmation_url, str):
        return None
    return {
        "receipt_visible": receipt_visible,
        "applied_badge_visible": applied_badge_visible,
        "confirmation_text": confirmation_text,
        "confirmation_url": str(confirmation_url or "").strip(),
    }


_RESULT_LINE = re.compile(
    r"^RESULT:(READY_TO_SUBMIT|PREVIEWED|APPLIED|SUBMISSION_UNCERTAIN|"
    r"COVER_NOT_REQUIRED|COVER_LETTER_REQUIRED|EXPIRED|CAPTCHA|LOGIN_ISSUE|FAILED)(?::([^\r\n]+))?$"
)


def _parse_result_line(output: str) -> tuple[str, str | None] | None:
    """Parse exactly one standalone RESULT line, rejecting prose and duplicates."""
    if len(re.findall(r"RESULT:", output)) != 1:
        return None
    result_lines = [line.strip() for line in output.splitlines() if "RESULT:" in line]
    if len(result_lines) != 1:
        return None
    match = _RESULT_LINE.fullmatch(result_lines[0])
    if match is None:
        return None
    return match.group(1), (match.group(2) or "").strip() or None


def _result_status(marker: str, reason: str | None) -> str:
    if marker == "FAILED":
        return f"failed:{reason or 'unknown'}"
    return marker.lower()


def _interpret_agent_output(
    output: str,
    *,
    dry_run: bool,
    submission_phase: str,
) -> tuple[str, dict | None]:
    """Fail closed on phase-inappropriate, duplicated, or malformed results."""
    parsed = _parse_result_line(output)
    if parsed is None:
        status = (
            "submission_uncertain"
            if submission_phase == "submit" and not dry_run
            else "failed:invalid_result_marker"
        )
        return status, None

    marker, reason = parsed
    blockers = {"EXPIRED", "CAPTCHA", "LOGIN_ISSUE", "FAILED"}
    if dry_run:
        if marker == "PREVIEWED":
            audit_error = _validate_preview_audit(output)
            return (
                (f"failed:{audit_error}", None)
                if audit_error
                else ("previewed", None)
            )
        if marker in blockers:
            return _result_status(marker, reason), None
        if marker in {"APPLIED", "SUBMISSION_UNCERTAIN"}:
            return "submission_uncertain", None
        return "failed:invalid_preview_result", None

    if submission_phase == "prepare":
        if marker == "READY_TO_SUBMIT":
            return "ready_to_submit", None
        if marker == "COVER_NOT_REQUIRED":
            return "cover_not_required", None
        if marker == "COVER_LETTER_REQUIRED":
            return "cover_letter_required", None
        if marker in blockers:
            return _result_status(marker, reason), None
        # A claimed submission during prepare may already have caused the
        # external side effect, so never classify it as an ordinary retry.
        if marker in {"APPLIED", "SUBMISSION_UNCERTAIN"}:
            return "submission_uncertain", None
        return "failed:invalid_prepare_result", None

    if submission_phase == "submit":
        if marker == "APPLIED":
            evidence = _validate_submission_evidence(output)
            return ("applied", evidence) if evidence else ("submission_uncertain", None)
        if marker == "SUBMISSION_UNCERTAIN":
            return "submission_uncertain", None
        # Once the submit turn starts, READY, blockers, and any other legal but
        # phase-inappropriate marker cannot prove whether the click happened.
        return "submission_uncertain", None

    return "failed:invalid_submission_phase", None


def _parse_unanswered_questions(output: str) -> list[dict] | None:
    """Parse the compact unresolved-question record emitted by the browser agent."""
    marker = re.search(r"UNANSWERED_QUESTIONS\s*:\s*", output)
    if not marker:
        return None
    payload = output[marker.end():].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, dict)]


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


def _application_fact_value(profile: dict, key: str) -> object | None:
    """Return the newest confirmed profile fact for one stable key."""
    for fact in reversed(profile.get("application_facts", [])):
        if isinstance(fact, dict) and str(fact.get("key") or "").strip() == key:
            return fact.get("value")
    return None


def _yes_no_value(value: object) -> bool | None:
    text = str(value or "").strip().casefold()
    if re.match(r"^(?:yes|true)\b", text):
        return True
    if re.match(r"^(?:no|false|none|not applicable|n/?a)\b", text):
        return False
    return None


def _selected_matches_boolean(selected: object, expected: bool) -> bool:
    text = " ".join(str(selected or "").strip().casefold().split())
    if expected:
        return bool(re.match(r"^(?:yes|true)\b", text))
    return bool(
        re.match(r"^(?:no|false|none|neither|not applicable|n/?a)\b", text)
        or "none of the above" in text
        or "citizen of a different country" in text
    )


def _work_authorization_answers(profile: dict, job: dict) -> tuple[bool, bool] | None:
    """Return (authorized, sponsorship-needed) for a clearly classified role."""
    policy = profile.get("work_authorization", {}).get("form_answer_policy", {})
    job_text = " ".join(
        str(job.get(field) or "").casefold()
        for field in ("title", "full_description", "application_readiness_reason")
    )
    branch = None
    if "intern" in job_text:
        if "non-credit" in job_text or "part-time" in job_text:
            branch = policy.get("non_credit_internship")
        branch = branch or policy.get("programme_credit_bearing_internship")
    elif any(term in job_text for term in ("full-time", "full time", "permanent")):
        branch = policy.get("post_graduation_full_time")
    if not isinstance(branch, dict):
        return None
    authorized = _yes_no_value(branch.get("legally_authorized"))
    sponsorship = _yes_no_value(branch.get("requires_sponsorship"))
    if authorized is None or sponsorship is None:
        return None
    return authorized, sponsorship


def _expected_screening_answer(
    question: object, profile: dict, job: dict
) -> tuple[str, bool] | None:
    """Map common legal/screening questions to confirmed, contextual facts."""
    text = " ".join(str(question or "").casefold().split())
    if not text:
        return None

    if re.search(r"\bf[\s-]?1\b|\bcpt\b|\bopt\b", text):
        expected = _yes_no_value(_application_fact_value(profile, "f1_student_status"))
        return ("f1_student_status", expected) if expected is not None else None
    if re.search(r"\bu\.?s\.? person\b|\bunited states person\b", text):
        expected = _yes_no_value(
            _application_fact_value(profile, "united_states_person_status")
        )
        return ("united_states_person_status", expected) if expected is not None else None

    work_answers = _work_authorization_answers(profile, job)
    if re.search(r"sponsor|sponsorship", text) and work_answers is not None:
        return "requires_sponsorship", work_answers[1]
    if re.search(
        r"(?:authori[sz]ed|legal(?:ly)? (?:eligible|entitled)|right) to work",
        text,
    ) and work_answers is not None:
        return "legally_authorized_to_work", work_answers[0]

    company = re.sub(r"[^a-z0-9]+", " ", str(job.get("company_name") or "").casefold()).strip()
    employer_question = re.search(r"\b(previously|ever)\b.*\b(worked|employed)\b", text)
    if employer_question and (not company or company in re.sub(r"[^a-z0-9]+", " ", text)):
        preserved = {
            re.sub(r"[^a-z0-9]+", " ", str(name).casefold()).strip()
            for name in profile.get("resume_facts", {}).get("preserved_companies", [])
        }
        return "previously_worked_for_target_employer", company in preserved

    if re.search(r"non[ -]?compete|non[ -]?solicitation|contractual .*restrict|legal .*restrict", text):
        value = _application_fact_value(
            profile, "employment_or_non_compete_restrictions"
        ) or profile.get("screening", {}).get("employment_or_non_compete_restrictions")
        expected = _yes_no_value(value)
        return ("employment_or_non_compete_restrictions", expected) if expected is not None else None
    if re.search(r"criminal|convict", text):
        value = _application_fact_value(
            profile, "criminal_convictions_to_disclose"
        ) or profile.get("screening", {}).get("criminal_convictions_to_disclose")
        expected = _yes_no_value(value)
        return ("criminal_convictions_to_disclose", expected) if expected is not None else None
    if "background check" in text:
        expected = _yes_no_value(
            profile.get("screening", {}).get("willing_to_complete_background_check")
        )
        return ("background_check", expected) if expected is not None else None
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


def _visible_captcha_overlay(page) -> bool:
    """Detect a user-visible CAPTCHA challenge without reading its contents."""
    for iframe in page.locator("iframe").all():
        try:
            title = (iframe.get_attribute("title") or "").casefold()
            source = (iframe.get_attribute("src") or "").casefold()
            if not iframe.is_visible():
                continue
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


def _visible_verification_gate(page) -> bool:
    """Detect a visible CAPTCHA or email/OTP gate without reading its value."""
    if _visible_captcha_overlay(page):
        return True
    try:
        return bool(page.evaluate(
            r"""() => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  el.getClientRects().length > 0;
              };
              const verification = /security code|verification code|one[- ]time (?:code|password)|\botp\b|verify (?:your )?email|email verification|验证码|校验码|验证邮箱/i;
              const inputs = [...document.querySelectorAll('input')].filter(visible);
              const codeInputs = inputs.filter((el) => {
                const maxLength = Number(el.maxLength || 0);
                return maxLength === 1 || /otp|verification|security.?code/i.test(
                  `${el.name || ''} ${el.id || ''} ${el.autocomplete || ''}`
                );
              });
              if (codeInputs.length < 4) return false;
              return [...document.querySelectorAll('form,section,dialog,[role="dialog"]')]
                .filter(visible).some((el) => verification.test(el.innerText || ''));
            }"""
        ))
    except Exception:
        logger.debug("Unable to inspect a verification gate", exc_info=True)
        return False


def _validate_pre_submit_snapshot(snapshot: dict, profile: dict, job: dict) -> list[str]:
    """Return browser-observed attention signals for the next agent turn."""
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

    if snapshot.get("captcha_visible"):
        issues.append("visible_captcha")

    issues.extend(
        f"required_field_empty:{label[:80]}"
        for label in snapshot.get("required_unfilled", [])
    )

    issues.extend(
        f"sensitive_required_unknown:{label[:80]}"
        for label in snapshot.get("sensitive_required_unknown", [])
    )

    if snapshot.get("assessment_visible"):
        issues.append("assessment_present")

    if "resume_field_present" in snapshot:
        if not snapshot.get("resume_field_present"):
            issues.append("resume_state_unconfirmed")
        elif not snapshot.get("resume_uploaded"):
            issues.append("resume_not_uploaded")

    personal = profile.get("personal", {})
    legal_name = personal.get("full_name", "").strip().casefold()
    for value in snapshot.get("full_name_values", []):
        if legal_name and value.strip().casefold() != legal_name:
            issues.append("legal_name_mismatch")
            break

    expected_email = personal.get("email", "").strip().casefold()
    for value in snapshot.get("email_values", []):
        if expected_email and value.strip().casefold() != expected_email:
            issues.append("email_mismatch")
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

        generic_expected = _expected_screening_answer(text, profile, job)
        if generic_expected is not None:
            generic_key, generic_value = generic_expected
            if not _selected_matches_boolean(selected, generic_value):
                issues.append(f"hard_answer_mismatch:{generic_key}")

    for field in snapshot.get("select_fields", []):
        text = field.get("text", "").casefold()
        selected = field.get("selected", "").strip().casefold()
        if "currently based" in text and "legal right to work" in text and selected != "singapore":
            issues.append("work_location_selection_not_singapore")
        generic_expected = _expected_screening_answer(text, profile, job)
        if generic_expected is not None:
            generic_key, generic_value = generic_expected
            if not _selected_matches_boolean(selected, generic_value):
                issues.append(f"hard_answer_mismatch:{generic_key}")

    readiness_text = str(job.get("application_readiness_reason") or "").casefold()
    non_credit_part_time = "non-credit" in readiness_text or "part-time" in readiness_text
    weekly_limit = profile.get("availability", {}).get(
        "non_credit_internship_hours_per_week_max"
    )
    if non_credit_part_time and isinstance(weekly_limit, (int, float)):
        for field in snapshot.get("text_fields", []):
            text = str(field.get("text") or "").casefold()
            if not re.search(r"hours? (?:per|a) week|weekly hours?", text):
                continue
            match = re.search(r"\d+(?:\.\d+)?", str(field.get("value") or ""))
            if match and float(match.group()) > float(weekly_limit):
                issues.append("non_credit_hours_exceed_confirmed_limit")

    if snapshot.get("submit_control_count", 0) < 1:
        issues.append("submit_control_missing")
    return list(dict.fromkeys(issues))


def _audit_live_pre_submit_page(
    port: int, worker_id: int, job: dict
) -> tuple[str | None, dict]:
    """Observe the visible form without changing it or deciding whether to proceed."""
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
              const required = (el) => el.required || el.getAttribute('aria-required') === 'true' || /[✱*]/.test(labelText(el));
              const responseSelector =
                'textarea[name*="captcha" i],textarea[name*="recaptcha" i],input[name*="captcha" i],input[name*="recaptcha" i]';
              const responseFields = [...document.querySelectorAll(responseSelector)];
              const inputs = [...document.querySelectorAll(
                'input:not([type=hidden]):not([type=radio]):not([type=checkbox]):not([type=file]):not([type=submit]):not([type=button]), textarea, select'
              )].filter((el) => visible(el) && !el.matches(responseSelector));
              const requiredUnfilled = [];
              const sensitiveRequiredUnknown = [];
              const fullNameValues = [];
              const emailValues = [];
              const currentLocationValues = [];
              const selectFields = [];
              const textFields = [];
              for (const el of inputs) {
                const text = labelText(el);
                const value = el.tagName === 'SELECT'
                  ? (el.selectedOptions[0] ? el.selectedOptions[0].textContent.trim() : '')
                  : (el.value || '').trim();
                if (required(el) && (!value || /^(select|choose)(\.\.\.)?$/i.test(value))) {
                  requiredUnfilled.push(text);
                }
                if (
                  required(el) &&
                  /work (authorization|authorisation)|right to work|visa|sponsorship|citizenship|legal identity|passport|national id/i.test(text) &&
                  (!value || /^(select|choose|unknown|not sure|prefer not)(\.\.\.)?$/i.test(value))
                ) sensitiveRequiredUnknown.push(text);
                if (/\b(full|legal) name\b/i.test(text) && !/preferred|display/i.test(text)) {
                  fullNameValues.push(value);
                }
                if (el.type === 'email' || /\bemail(?: address)?\b/i.test(text)) emailValues.push(value);
                if (/current location/i.test(text)) currentLocationValues.push(value);
                if (el.tagName === 'SELECT') selectFields.push({text, selected: value});
                else textFields.push({text, value});
              }
              const nearbyUploadText = (el) => {
                let node = el;
                for (let depth = 0; node && node !== document.body && depth < 7; depth += 1) {
                  const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
                  if (/\.pdf\b|uploaded|replace|remove|download/i.test(text)) return text;
                  node = node.parentElement;
                }
                return '';
              };
              const fileFields = [...document.querySelectorAll('input[type=file]')]
                .map((el) => ({
                  text: labelText(el),
                  nearby_text: nearbyUploadText(el),
                  count: el.files ? el.files.length : 0
                }));
              const resumeFields = fileFields.filter((f) => /\bresume\b|\bcv\b/i.test(f.text));
              const resumeCards = [...document.querySelectorAll(
                '[data-qa*="resume" i],[data-testid*="resume" i],[class*="resume" i],[aria-label*="resume" i],[aria-label*="cv" i]'
              )].filter(visible).map((el) => (el.innerText || el.getAttribute('aria-label') || '').trim());
              const resumeUploaded = resumeFields.some((f) =>
                f.count > 0 || /success|uploaded|replace|remove|\.pdf/i.test(
                  `${f.text} ${f.nearby_text}`
                )
              ) || resumeCards.some((text) => /\b[^\s]+\.pdf\b|uploaded|replace|remove|download/i.test(text));
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
                if (
                  required(radio) && !checked &&
                  /work (authorization|authorisation)|right to work|visa|sponsorship|citizenship|legal identity|passport|national id/i.test(text)
                ) sensitiveRequiredUnknown.push(text);
                radioQuestions.push({text, selected});
              }
              const requiredChecks = [...document.querySelectorAll('input[type=checkbox]')]
                .filter((el) => visible(el) && required(el));
              for (const checkbox of requiredChecks) {
                if (!checkbox.checked) requiredUnfilled.push(labelText(checkbox));
              }
              const submitControls = [...document.querySelectorAll('button,input[type=submit]')]
                .filter((el) => visible(el) && /submit|send application|finish|complete application/i.test(
                  (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
                ));
              const captchaCandidates = [...document.querySelectorAll(
                'iframe,[class*="turnstile" i],[id*="turnstile" i],[class*="hcaptcha" i],[class*="recaptcha" i],[data-sitekey]'
              )].map((el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                const marker = `${el.title || ''} ${el.src || ''} ${el.id || ''} ${el.className || ''}`.toLowerCase();
                return {
                  marker: marker.slice(0, 240),
                  width: Math.round(rect.width),
                  height: Math.round(rect.height),
                  display: style.display,
                  visibility: style.visibility,
                  opacity: style.opacity,
                  aria_hidden: el.getAttribute('aria-hidden') || '',
                  visible: visible(el) && rect.width >= 80 && rect.height >= 40 &&
                    /captcha|turnstile|challenge/.test(marker)
                };
              });
              const captchaVisible = captchaCandidates.some((candidate) => candidate.visible);
              const visibleText = document.body ? document.body.innerText : '';
              const assessmentVisible = /\b(complete|take|start) (an? )?(online |coding |video )?assessment\b|\bcoding assessment\b|\bonline assessment\b/i.test(visibleText);
              return {
                url: location.href,
                required_unfilled: requiredUnfilled,
                sensitive_required_unknown: sensitiveRequiredUnknown,
                resume_field_present: resumeFields.length > 0 || resumeCards.length > 0,
                resume_uploaded: resumeUploaded,
                full_name_values: fullNameValues,
                email_values: emailValues,
                current_location_values: currentLocationValues,
                select_fields: selectFields,
                text_fields: textFields,
                radio_questions: radioQuestions,
                submit_control_count: submitControls.length,
                assessment_visible: assessmentVisible,
                captcha_visible: captchaVisible,
                captcha_candidates: captchaCandidates,
                captcha_token_present: responseFields.some((el) => (el.value || '').trim().length > 0)
              };
            }"""
        )
        issues = _validate_pre_submit_snapshot(snapshot, profile, job)
        report = {
            "status": "clear" if not issues else "attention",
            "issues": issues,
            "advisory_only": False,
            "submission_gate": True,
            "required_unfilled_count": len(snapshot.get("required_unfilled", [])),
            "resume_field_present": snapshot.get("resume_field_present", False),
            "resume_uploaded": snapshot.get("resume_uploaded", False),
            "submit_control_count": snapshot.get("submit_control_count", 0),
            "captcha_token_present": snapshot.get("captcha_token_present", False),
            "captcha_candidates": snapshot.get("captcha_candidates", []),
            "assessment_visible": snapshot.get("assessment_visible", False),
            "sensitive_required_unknown_count": len(
                snapshot.get("sensitive_required_unknown", [])
            ),
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


def _classify_post_submit_observation(observation: dict) -> str:
    """Classify the browser state after a final action without guessing success.

    A visible receipt is success. A visible verification gate or deterministic
    field validation rejection proves the application is not yet submitted and
    must not be collapsed into the retry-blocking ``submission_uncertain`` state.
    """
    if observation.get("confirmed") is True:
        return "confirmed"
    if (
        observation.get("verification_visible") is True
        or observation.get("captcha_visible") is True
    ):
        return "verification_required"
    if int(observation.get("validation_error_count") or 0) > 0:
        if int(observation.get("manual_validation_error_count") or 0) > 0:
            return "validation_blocked_manual"
        if int(observation.get("repairable_validation_error_count") or 0) > 0:
            return "validation_blocked_repairable"
        return "validation_blocked_manual"
    return "uncertain"


def _observe_post_submit_page(
    port: int, worker_id: int, job: dict, attempt: int = 1
) -> dict:
    """Independently observe visible post-submit state through the existing CDP browser."""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        if not pages:
            return {"confirmed": False, "reason": "post_submit_no_page"}
        page = pages[-1]
        observed = page.evaluate(
            r"""() => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  el.getClientRects().length > 0;
              };
              const strongReceipt = /your application has been submitted|application (?:was |has been )?(?:successfully )?submitted|thank you for (?:applying|submitting your application)|we (have )?received your application|申请已提交|投递成功|申请成功/i;
              const exactBadge = /^(applied|已申请|已投递)$/i;
              const submitLabel = /submit|send application|finish|complete application|提交申请|投递/i;
              const verificationText = /security code|verification code|one[- ]time (?:code|password)|\botp\b|verify (?:your )?email|email verification|验证码|校验码|验证邮箱/i;
              const unsafeRepairText = /video|audio|record(?:ing)?|camera|microphone|passport|national id|identity document|bank account|credit card|tax id|ssn|nric|身份证|护照|银行卡|录音|录像|摄像头|麦克风/i;
              const candidates = [...document.querySelectorAll(
                '[role="status"],[aria-live],[data-qa*="confirm" i],[data-testid*="confirm" i],[class*="confirmation" i],[class*="success" i]'
              )].filter(visible).map((el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
              const lines = (document.body ? document.body.innerText : '').split(/\n+/)
                .map((line) => line.replace(/\s+/g, ' ').trim()).filter(Boolean);
              const receiptText = [...candidates, ...lines].find((text) => strongReceipt.test(text)) || '';
              const badgeText = [...document.querySelectorAll('button,a,span,div')]
                .filter(visible).map((el) => (el.innerText || '').replace(/\s+/g, ' ').trim())
                .find((text) => exactBadge.test(text)) || '';
              const context = (el) => el.closest(
                'li,fieldset,[data-qa*="field" i],[data-testid*="field" i],[class*="application-field" i],[class*="question" i],[class*="field" i]'
              ) || el.parentElement;
              const labelText = (el) => {
                const node = context(el);
                return ((node && node.innerText) || el.getAttribute('aria-label') || el.name || '')
                  .replace(/\s+/g, ' ').trim().slice(0, 500);
              };
              const controls = [...document.querySelectorAll('input:not([type=hidden]),textarea,select')]
                .filter(visible);
              const validationErrors = [];
              const seenErrors = new Set();
              const seenMessages = new Set();
              for (const el of controls) {
                let described = '';
                const describedBy = (el.getAttribute('aria-describedby') || '').trim().split(/\s+/).filter(Boolean);
                if (describedBy.length) {
                  described = describedBy.map((id) => {
                    const node = document.getElementById(id);
                    return node ? (node.innerText || node.textContent || '') : '';
                  }).join(' ').replace(/\s+/g, ' ').trim();
                }
                const nativeInvalid = Boolean(el.willValidate && !el.validity.valid);
                const ariaInvalid = el.getAttribute('aria-invalid') === 'true';
                const message = (el.validationMessage || described || '').replace(/\s+/g, ' ').trim();
                if (!nativeInvalid && !ariaInvalid && !message) continue;
                const label = labelText(el);
                const key = `${el.name || el.id || label}|${message}`;
                if (seenErrors.has(key)) continue;
                seenErrors.add(key);
                if (message) seenMessages.add(message);
                const type = el.tagName === 'SELECT' ? 'select' : (el.type || el.tagName.toLowerCase());
                const optionalClaimed = /\boptional\b|可选|非必填/i.test(label);
                const repairable = !unsafeRepairText.test(`${label} ${message}`) &&
                  !['file', 'password'].includes(type);
                validationErrors.push({
                  label: label.slice(0, 240),
                  message: message.slice(0, 240),
                  field_type: type,
                  optional_claimed: optionalClaimed,
                  repairable
                });
              }
              for (const alert of [...document.querySelectorAll('[role="alert"],[aria-live="assertive"]')].filter(visible)) {
                const message = (alert.innerText || alert.textContent || '').replace(/\s+/g, ' ').trim();
                if (!message || !/required|invalid|error|please (?:enter|select|complete|provide|upload)|必填|无效|错误|请选择|请填写/i.test(message)) continue;
                if (seenMessages.has(message)) continue;
                const key = `alert|${message}`;
                if (seenErrors.has(key)) continue;
                seenErrors.add(key);
                validationErrors.push({
                  label: 'page validation alert',
                  message: message.slice(0, 240),
                  field_type: 'unknown',
                  optional_claimed: /\boptional\b|可选|非必填/i.test(message),
                  repairable: false
                });
              }
              const submitControls = [...document.querySelectorAll('button,input[type=submit],input[type=button]')]
                .filter((el) => visible(el) && submitLabel.test(
                  (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
                ));
              const captchaVisible = [...document.querySelectorAll(
                'iframe,[class*="turnstile" i],[id*="turnstile" i],[class*="hcaptcha" i],[class*="recaptcha" i],[data-sitekey]'
              )].filter(visible).some((el) => {
                const rect = el.getBoundingClientRect();
                const marker = `${el.title || ''} ${el.src || ''} ${el.id || ''} ${el.className || ''}`.toLowerCase();
                return rect.width >= 80 && rect.height >= 40 && /captcha|turnstile|challenge/.test(marker);
              });
              const codeInputs = controls.filter((el) => {
                const maxLength = Number(el.maxLength || 0);
                return maxLength === 1 || /otp|verification|security.?code/i.test(`${el.name || ''} ${el.id || ''} ${el.autocomplete || ''}`);
              });
              const verificationVisible = captchaVisible ||
                (codeInputs.length >= 4 && verificationText.test(document.body ? document.body.innerText : '')) ||
                [...document.querySelectorAll('form,section,dialog,[role="dialog"]')]
                  .filter(visible).some((el) => verificationText.test(el.innerText || ''));
              const repairableCount = validationErrors.filter((item) => item.repairable).length;
              const manualCount = validationErrors.length - repairableCount;
              return {
                current_url: location.href,
                page_title: document.title || '',
                receipt_visible: Boolean(receiptText),
                applied_badge_visible: Boolean(badgeText),
                confirmation_text: receiptText || badgeText,
                form_visible: [...document.querySelectorAll('form')].some(visible),
                submit_control_count: submitControls.length,
                validation_errors: validationErrors.slice(0, 12),
                validation_error_count: validationErrors.length,
                repairable_validation_error_count: repairableCount,
                manual_validation_error_count: manualCount,
                verification_visible: verificationVisible,
                captcha_visible: captchaVisible
              };
            }"""
        )
        screenshot = (
            config.APPLY_WORKER_DIR
            / f"worker-{worker_id}"
            / (
                "submission-confirmation-observer.png"
                if attempt == 1
                else f"submission-confirmation-observer-attempt-{attempt}.png"
            )
        )
        try:
            page.screenshot(path=str(screenshot), full_page=True)
            observed["screenshot_path"] = str(screenshot)
        except Exception:
            logger.exception("Post-submit screenshot capture failed")
            observed["screenshot_path"] = None
        observed["confirmed"] = bool(
            observed.get("receipt_visible") or observed.get("applied_badge_visible")
        )
        observed["disposition"] = _classify_post_submit_observation(observed)
        observed["job_url"] = job.get("url")
        return observed
    except Exception as exc:
        logger.exception("Post-submit browser observation failed")
        return {
            "confirmed": False,
            "reason": f"post_submit_observer_error:{type(exc).__name__}",
        }
    finally:
        playwright.stop()


def _submission_evidence_consistent(model: dict | None, observer: dict) -> bool:
    """Require independent visible confirmation that agrees with the model claim."""
    if not model or observer.get("confirmed") is not True:
        return False
    receipt_agrees = (
        model.get("receipt_visible") is True
        and observer.get("receipt_visible") is True
    )
    badge_agrees = (
        model.get("applied_badge_visible") is True
        and observer.get("applied_badge_visible") is True
    )
    if not (receipt_agrees or badge_agrees):
        return False

    model_text = " ".join(
        re.sub(
            r"[^\w]+", " ", str(model.get("confirmation_text") or "").casefold()
        ).split()
    )
    observed_text = " ".join(
        re.sub(
            r"[^\w]+", " ", str(observer.get("confirmation_text") or "").casefold()
        ).split()
    )
    if not model_text or model_text not in observed_text:
        return False

    claimed_url = str(model.get("confirmation_url") or "").strip().rstrip("/")
    current_url = str(observer.get("current_url") or "").strip().rstrip("/")
    return not claimed_url or claimed_url == current_url


def _reserve_manifest_submission(manifest: dict | None, job: dict) -> tuple[bool, str]:
    """Re-authorize current job bytes and atomically reserve the batch slot."""
    if manifest is None:
        return False, "authorization_manifest_required"
    try:
        expires_at = datetime.fromisoformat(str(manifest.get("expires_at") or ""))
        if expires_at.tzinfo is None or datetime.now(UTC) >= expires_at:
            return False, "authorization_manifest_expired"
        from applypilot.apply.authorization import authorize_job
        from applypilot.database import reserve_batch_submission

        if authorize_job(manifest, job) is None:
            return False, "authorization_manifest_job_mismatch"
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

        update_batch_submission_status(
            str(manifest.get("batch_id") or ""),
            str(job.get("url") or ""),
            status,
            evidence=evidence,
        )
        return True
    except Exception:
        logger.exception("Batch submission ledger update failed")
        return False


def _wait_for_manual_captcha(
    port: int, worker_id: int, timeout_seconds: int | None = None
) -> bool:
    """Keep Edge alive until the applicant clears a visible verification gate."""
    from playwright.sync_api import sync_playwright

    if timeout_seconds is None:
        timeout_seconds = int(
            config.load_profile().get("submission_policy", {}).get(
                "manual_intervention_timeout_seconds", 1800
            )
        )
    timeout_seconds = max(60, min(timeout_seconds, 3600))
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
    add_event(f"[W{worker_id}] MANUAL VERIFICATION: Edge is waiting for the applicant")
    update_state(worker_id, status="captcha", last_action="waiting for manual verification")
    try:
        if platform.system() == "Windows":
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        else:
            console = Console()
            console.print("\a", end="")
    except Exception:
        logger.debug("Could not emit manual-intervention alert", exc_info=True)

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        deadline = time.monotonic() + timeout_seconds
        clear_polls = 0
        while time.monotonic() < deadline and not _stop_event.is_set():
            pages = [page for context in browser.contexts for page in context.pages]
            solved = any(_captcha_response_present(page) for page in pages)
            visible = not solved and any(_visible_verification_gate(page) for page in pages)
            if visible:
                clear_polls = 0
            else:
                clear_polls += 1
                if clear_polls >= 2:
                    marker.write_text(
                        json.dumps({"status": "solved_by_applicant", "port": port}),
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

def acquire_job(target_url: str | None = None, min_score: int = 6,
                worker_id: int = 0, preview_only: bool = False,
                authorization_manifest: dict | None = None,
                exclude_urls: set[str] | None = None) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        exclude_urls: Exact job URLs already attempted in this command.

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    excluded = {str(url) for url in (exclude_urls or set()) if str(url)}
    from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility
    refresh_job_eligibility(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")

        try:
            submission_policy = config.load_profile().get("submission_policy", {})
        except FileNotFoundError:
            submission_policy = {}
        allow_runtime_cover = bool(
            submission_policy.get("allow_runtime_cover_letter_discovery", False)
        )
        allow_runtime_readiness = bool(
            submission_policy.get("allow_runtime_readiness_review", False)
        )

        if target_url:
            material_clause = """
                  AND tailored_resume_path IS NOT NULL
                  AND tailor_status = 'machine_validated'
            """
            if not preview_only and not allow_runtime_cover:
                material_clause += """
                  AND (
                    (cover_letter_path IS NOT NULL AND cover_letter_status IN ('human_approved', 'agent_validated'))
                    OR cover_letter_status = 'not_required'
                  )
                """
            target_match = "(url = ? OR application_url = ?)"
            target_params = (target_url, target_url)
            rows = conn.execute(f"""
                SELECT *
                FROM jobs
                WHERE {target_match}
                  {material_clause}
                  AND (apply_status IS NULL OR apply_status IN ('failed', 'previewed'))
                  AND COALESCE(apply_retry_blocked, 0) = 0
                  AND eligibility_status != 'ineligible'
            """, target_params).fetchall()
            if excluded:
                rows = [candidate for candidate in rows if candidate["url"] not in excluded]
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = [min_score]
            excluded_clause = ""
            if excluded:
                excluded_placeholders = ",".join("?" * len(excluded))
                excluded_clause = f"AND url NOT IN ({excluded_placeholders})"
                params.extend(sorted(excluded))
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            rows = conn.execute(f"""
                SELECT *
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND tailor_status = 'machine_validated'
                  {"" if allow_runtime_cover else "AND ((cover_letter_path IS NOT NULL AND cover_letter_status IN ('human_approved', 'agent_validated')) OR cover_letter_status = 'not_required')"}
                  AND (apply_status IS NULL OR apply_status IN ('failed', 'previewed'))
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND COALESCE(apply_retry_blocked, 0) = 0
                  AND fit_score >= ?
                  AND {ELIGIBLE_SQL}
                  {excluded_clause}
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, url
            """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchall()

        row = None
        if authorization_manifest is None:
            row = rows[0] if rows else None
        else:
            from applypilot.apply.authorization import authorize_job
            from applypilot.apply.decision import evaluate

            minimum_fit_score = max(1, min(int(min_score), 10))

            try:
                expires_at = datetime.fromisoformat(
                    str(authorization_manifest.get("expires_at") or "")
                )
            except ValueError:
                expires_at = datetime.min.replace(tzinfo=UTC)
            if expires_at.tzinfo is None or datetime.now(UTC) >= expires_at:
                rows = []

            for candidate in rows:
                candidate_job = dict(candidate)
                candidate_job["application_url"] = (
                    candidate_job.get("application_url") or candidate_job.get("url")
                )
                candidate_decision = evaluate(
                    candidate_job,
                    minimum_fit_score=minimum_fit_score,
                    allow_runtime_readiness=allow_runtime_readiness,
                    allow_runtime_cover_letter=allow_runtime_cover,
                )
                if candidate_decision.get("decision") != "ready_to_apply":
                    continue
                try:
                    authorized = authorize_job(authorization_manifest, candidate_job)
                except (KeyError, PermissionError, RuntimeError, ValueError):
                    continue
                if authorized is not None:
                    row = candidate
                    break

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
                task_id: str | None = None,
                evidence: dict | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(UTC).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_retry_blocked = 0, apply_retry_reason = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           verification_confidence = 'visible_confirmation',
                           application_evidence = ?, application_recorded_at = ?,
                           submission_observation_json = ?, submission_observed_at = ?
            WHERE url = ?
        """, (
            now,
            duration_ms,
            task_id,
            json.dumps(evidence or {}, ensure_ascii=False),
            now,
            json.dumps(evidence or {}, ensure_ascii=False),
            now,
            url,
        ))
    elif status == "submission_uncertain":
        observation = {
            "submit_clicked": True,
            "receipt_visible": False,
            "applied_badge_visible": False,
            "note": error or "final submission was attempted without visible confirmation",
        }
        if evidence:
            observation.update(evidence)
        conn.execute("""
            UPDATE jobs SET apply_status = 'submission_uncertain', applied_at = NULL,
                           apply_error = NULL, agent_id = NULL,
                           apply_retry_blocked = 1,
                           apply_retry_reason = 'submission_uncertain_requires_review',
                           apply_attempts = COALESCE(apply_attempts, 0) + 1,
                           apply_duration_ms = ?, apply_task_id = ?,
                           verification_confidence = 'browser_observation_pending',
                           application_evidence = 'submit_clicked_without_visible_confirmation',
                           application_recorded_at = ?,
                           submission_observation_json = ?, submission_observed_at = ?
            WHERE url = ?
        """, (
            duration_ms,
            task_id,
            now,
            json.dumps(observation, ensure_ascii=False),
            now,
            url,
        ))
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


def _mark_runtime_cover_not_required(job: dict) -> dict:
    """Persist an ATS observation that this exact form has no required cover letter."""
    conn = get_connection()
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE jobs SET cover_letter_status='not_required', cover_letter_error=NULL, "
        "cover_letter_approved_at=?, cover_letter_approved_by='runtime_form_observation' "
        "WHERE url=?",
        (now, job["url"]),
    )
    conn.commit()
    refreshed = conn.execute("SELECT * FROM jobs WHERE url=?", (job["url"],)).fetchone()
    if refreshed is None:
        raise ValueError("Exact job disappeared while recording cover-letter discovery")
    return dict(refreshed)


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
    return dict(refreshed)


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
                           apply_retry_blocked = 0, apply_retry_reason = NULL,
                           verification_confidence = 'manual_visual_confirmation',
                           application_evidence = 'manually_marked_applied',
                           application_recorded_at = ?
            WHERE url = ?
        """, (now, now, url))
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
          OR (apply_status IS NOT NULL AND apply_status NOT IN (
              'applied', 'in_progress', 'submission_uncertain'
          ))
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
        'applied', 'submission_uncertain', 'expired', 'captcha', 'login_issue',
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

BOT_BLOCK_PREFIXES: tuple[str, ...] = (
    "site_blocked",
    "cloudflare",
    "blocked_by_cloudflare",
    "automation_blocked",
    "bot_detected",
)


def _should_retry_with_cloak(result: str, requested_backend: str) -> bool:
    """Return whether a pre-submit Edge failure merits one stealth retry."""
    if requested_backend != "auto":
        return False
    reason = result.split(":", 1)[-1] if ":" in result else result
    normalized = reason.casefold().replace(" ", "_").replace("-", "_")
    return normalized.startswith(BOT_BLOCK_PREFIXES)


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
                min_score: int = 6, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                agent_backend: str = "claude",
                manual_captcha_relay: bool = False,
                browser_backend: str = "edge",
                authorization_manifest: dict | None = None,
                attempted_urls: set[str] | None = None) -> tuple[int, int]:
    """Run jobs until the confirmed-success target is reached or the queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Confirmed submissions to achieve for a real run; preview jobs for dry-run (0 = continuous).
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
    profile = config.load_profile()
    requested_browser_backend = resolve_browser_backend(browser_backend)
    run_attempted_urls = attempted_urls if attempted_urls is not None else set()

    while not _stop_event.is_set():
        target_progress = jobs_done if dry_run else applied
        if not continuous and target_progress >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        if not dry_run:
            allowed, cooldown, rate_reason = _submission_rate_status(
                get_connection(), profile
            )
            if not allowed:
                add_event(f"[W{worker_id}] Rate limit reached: {rate_reason}")
                update_state(worker_id, status="done", last_action=rate_reason)
                break
            if cooldown > 0:
                add_event(f"[W{worker_id}] Submission cooldown: {cooldown:.0f}s")
                update_state(worker_id, status="idle", last_action="submission cooldown")
                if _stop_event.wait(timeout=cooldown):
                    break

        job = acquire_job(
            target_url=target_url,
            min_score=min_score,
            worker_id=worker_id,
            preview_only=dry_run,
            authorization_manifest=authorization_manifest,
            exclude_urls=run_attempted_urls,
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
        run_attempted_urls.add(str(job["url"]))

        chrome_proc = None
        submission_started = False
        verification_relay_used = False
        cover_material_resolved = False
        ledger_reserved = False
        submission_evidence: dict | None = None
        try:
            active_browser_backend = (
                "edge" if requested_browser_backend == "auto" else requested_browser_backend
            )
            job["_browser_backend"] = active_browser_backend
            add_event(f"[W{worker_id}] Launching {active_browser_backend} browser...")
            start_url = str(job.get("application_url") or job["url"])
            chrome_proc = launch_chrome(
                worker_id,
                port=port,
                headless=headless,
                start_url=start_url,
                browser_backend=active_browser_backend,
            )

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

            if _should_retry_with_cloak(result, requested_browser_backend):
                edge_block_result = result
                cleanup_worker(worker_id, chrome_proc)
                chrome_proc = None
                active_browser_backend = "cloak"
                job["_browser_backend"] = active_browser_backend
                add_event(
                    f"[W{worker_id}] Bot protection detected; retrying once with CloakBrowser"
                )
                update_state(
                    worker_id,
                    status="retrying",
                    last_action="switching to CloakBrowser",
                )
                try:
                    chrome_proc = launch_chrome(
                        worker_id,
                        port=port,
                        headless=headless,
                        start_url=start_url,
                        browser_backend=active_browser_backend,
                    )
                    result, stealth_duration = run_job(
                        job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=dry_run,
                        agent_backend=agent_backend,
                        manual_captcha_relay=manual_captcha_relay,
                        submission_phase=submission_phase,
                    )
                    duration_ms += stealth_duration
                except Exception as exc:
                    logger.exception("CloakBrowser fallback failed")
                    result = f"failed:cloak_backend_unavailable:{type(exc).__name__}"
                logger.info(
                    "[worker-%d] Browser fallback edge_result=%s cloak_result=%s",
                    worker_id,
                    edge_block_result,
                    result,
                )

            while True:
                if result in {"cover_not_required", "cover_letter_required"}:
                    if cover_material_resolved:
                        result = "failed:cover_material_discovery_loop"
                        break
                    cover_material_resolved = True
                    try:
                        if result == "cover_not_required":
                            job = _mark_runtime_cover_not_required(job)
                            add_event(f"[W{worker_id}] ATS confirmed no cover letter is required")
                        else:
                            add_event(f"[W{worker_id}] ATS requires a cover letter; generating it")
                            update_state(
                                worker_id,
                                status="preparing_material",
                                last_action="generating validated cover letter",
                            )
                            job = _prepare_runtime_cover_letter(job)
                        # Runtime cover resolution reloads the database row.
                        # Re-attach the non-persisted browser policy before the
                        # agent resumes on the existing Cloak/Edge page.
                        job["_browser_backend"] = active_browser_backend
                        result, resumed_duration = run_job(
                            job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase="prepare",
                        )
                        duration_ms += resumed_duration
                        continue
                    except Exception as exc:
                        logger.exception("Runtime cover-letter resolution failed")
                        result = f"failed:manual_review_required:cover_letter_generation:{type(exc).__name__}"
                        break

                if result == "captcha":
                    if (
                        manual_captcha_relay
                        and not verification_relay_used
                        and _wait_for_manual_captcha(port, worker_id)
                    ):
                        verification_relay_used = True
                        resumed_job = dict(job)
                        resumed_job["_browser_observation"] = {
                            "verification_resume": True,
                            "signal": "manual_verification_cleared",
                            "submission_gate": True,
                        }
                        result, resumed_duration = run_job(
                            resumed_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=dry_run,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase=submission_phase,
                        )
                        duration_ms += resumed_duration
                        continue
                    break

                if result == "ready_to_submit" and not dry_run:
                    audit_signal, audit_report = _audit_live_pre_submit_page(
                        port, worker_id, job
                    )
                    observation_label = audit_signal or "clear"
                    add_event(
                        f"[W{worker_id}] Browser observation: {observation_label[:45]}"
                    )
                    update_state(
                        worker_id,
                        status="observed",
                        last_action=f"browser signal: {observation_label[:25]}",
                    )
                    if audit_signal:
                        result = f"failed:manual_review_required:{audit_signal}"
                        break
                    reserved, reservation_reason = _reserve_manifest_submission(
                        authorization_manifest, job
                    )
                    if not reserved:
                        result = f"failed:manual_review_required:{reservation_reason}"
                        break
                    ledger_reserved = authorization_manifest is not None
                    observed_job = dict(job)
                    observed_job["_browser_observation"] = {
                        **audit_report,
                        "signal": audit_signal,
                        "advisory_only": False,
                        "submission_gate": True,
                    }
                    submission_phase = "submit"
                    submission_started = True
                    result, submit_duration = run_job(
                        observed_job,
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
                    agent_evidence = observed_job.get("_agent_submission_evidence")
                    observer_evidence = _observe_post_submit_page(
                        port, worker_id, job, attempt=1
                    )
                    disposition = _classify_post_submit_observation(observer_evidence)
                    attempts = [{
                        "agent": agent_evidence,
                        "observer": observer_evidence,
                        "disposition": disposition,
                    }]

                    # One repair turn is allowed only when visible validation
                    # errors prove the first click was rejected. An absent
                    # receipt alone can never authorize another click.
                    if disposition == "validation_blocked_repairable":
                        repair_job = dict(observed_job)
                        repair_job.pop("_agent_submission_evidence", None)
                        repair_job["_browser_observation"] = {
                            "repair_mode": True,
                            "signal": disposition,
                            "validation_errors": observer_evidence.get(
                                "validation_errors", []
                            ),
                            "submission_gate": True,
                        }
                        add_event(f"[W{worker_id}] Repairing supported validation errors once")
                        update_state(
                            worker_id,
                            status="repairing",
                            last_action="one-time validation repair",
                        )
                        result, repair_duration = run_job(
                            repair_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase="submit",
                        )
                        duration_ms += repair_duration
                        agent_evidence = repair_job.get("_agent_submission_evidence")
                        observer_evidence = _observe_post_submit_page(
                            port, worker_id, job, attempt=2
                        )
                        disposition = _classify_post_submit_observation(
                            observer_evidence
                        )
                        attempts.append({
                            "agent": agent_evidence,
                            "observer": observer_evidence,
                            "disposition": disposition,
                        })
                    elif (
                        disposition == "verification_required"
                        and manual_captcha_relay
                        and not verification_relay_used
                        and _wait_for_manual_captcha(port, worker_id)
                    ):
                        verification_relay_used = True
                        verification_job = dict(observed_job)
                        verification_job.pop("_agent_submission_evidence", None)
                        verification_job["_browser_observation"] = {
                            "verification_resume": True,
                            "signal": "manual_verification_cleared",
                            "submission_gate": True,
                        }
                        result, resumed_duration = run_job(
                            verification_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase="submit",
                        )
                        duration_ms += resumed_duration
                        agent_evidence = verification_job.get(
                            "_agent_submission_evidence"
                        )
                        observer_evidence = _observe_post_submit_page(
                            port, worker_id, job, attempt=2
                        )
                        disposition = _classify_post_submit_observation(
                            observer_evidence
                        )
                        attempts.append({
                            "agent": agent_evidence,
                            "observer": observer_evidence,
                            "disposition": disposition,
                        })

                    submission_evidence = {
                        "browser_backend": active_browser_backend,
                        "fallback_from_edge": (
                            requested_browser_backend == "auto"
                            and active_browser_backend == "cloak"
                        ),
                        "agent": agent_evidence,
                        "observer": observer_evidence,
                        "attempts": attempts,
                    }
                    archived = _archive_worker_evidence(
                        config.APPLY_WORKER_DIR / f"worker-{worker_id}",
                        job,
                        worker_id,
                        datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"),
                    )
                    archived_by_name = {path.name: path for path in archived}
                    for index, attempt_evidence in enumerate(attempts, start=1):
                        filename = (
                            "submission-confirmation-observer.png"
                            if index == 1
                            else f"submission-confirmation-observer-attempt-{index}.png"
                        )
                        archived_observer = archived_by_name.get(filename)
                        if archived_observer is not None:
                            attempt_evidence["observer"]["screenshot_path"] = str(
                                archived_observer
                            )
                    final_archive = archived_by_name.get(
                        "submission-confirmation-observer.png"
                        if len(attempts) == 1
                        else f"submission-confirmation-observer-attempt-{len(attempts)}.png"
                    )
                    if final_archive is not None:
                        observer_evidence["screenshot_path"] = str(final_archive)

                    if disposition == "confirmed":
                        if result != "applied" or not _submission_evidence_consistent(
                            agent_evidence, observer_evidence
                        ):
                            result = "submission_uncertain"
                    elif disposition == "verification_required":
                        result = "captcha"
                    elif disposition == "validation_blocked_manual":
                        result = "failed:manual_review_required:submission_validation"
                    elif disposition == "validation_blocked_repairable":
                        result = "failed:submission_validation_blocked_after_repair"
                    else:
                        result = "submission_uncertain"
                    break
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
                ledger_updated = _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "applied",
                    submission_evidence,
                )
                if not ledger_updated:
                    uncertainty_evidence = {
                        "submit_started": True,
                        "reason": "submission_ledger_update_failed",
                        "submission_evidence": submission_evidence,
                    }
                    mark_result(
                        job["url"],
                        "submission_uncertain",
                        "submission ledger could not record the confirmed browser outcome",
                        duration_ms=duration_ms,
                        evidence=uncertainty_evidence,
                    )
                    add_event(
                        f"[W{worker_id}] Submission receipt found but ledger update failed"
                    )
                    update_state(
                        worker_id,
                        status="submission_uncertain",
                        last_action="ledger update failed",
                        jobs_done=applied + failed + 1,
                    )
                else:
                    mark_result(
                        job["url"],
                        "applied",
                        duration_ms=duration_ms,
                        evidence=submission_evidence,
                    )
                    applied += 1
                    update_state(worker_id, jobs_applied=applied,
                                 jobs_done=applied + failed)
            elif result == "submission_uncertain":
                uncertainty_evidence = submission_evidence or {
                    "submit_started": submission_started,
                    "reason": "agent_or_observer_confirmation_inconclusive",
                }
                _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "submission_uncertain",
                    uncertainty_evidence,
                )
                mark_result(
                    job["url"],
                    "submission_uncertain",
                    "browser did not show a decisive receipt after the final action",
                    duration_ms=duration_ms,
                    evidence=uncertainty_evidence,
                )
                add_event(f"[W{worker_id}] Submission state uncertain; status recorded")
                update_state(
                    worker_id,
                    status="submission_uncertain",
                    last_action="status recorded for agent review",
                    jobs_done=applied + failed + 1,
                )
            elif result == "previewed":
                mark_result(job["url"], "previewed", duration_ms=duration_ms)
                update_state(worker_id, jobs_done=applied + failed + 1)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                if submission_started and ledger_reserved:
                    _update_submission_ledger(
                        authorization_manifest,
                        job,
                        "failed",
                        {
                            "reason": reason,
                            "submission_evidence": submission_evidence,
                        },
                    )
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            if submission_started:
                uncertainty_evidence = {
                    "submit_started": True,
                    "reason": "operator_interrupt_after_submit_phase_started",
                }
                _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "submission_uncertain",
                    uncertainty_evidence,
                )
                mark_result(
                    job["url"],
                    "submission_uncertain",
                    "operator interrupt after submit phase started",
                    evidence=uncertainty_evidence,
                )
            else:
                release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            if submission_started:
                uncertainty_evidence = {
                    "submit_started": True,
                    "reason": f"launcher_error:{type(e).__name__}",
                }
                _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "submission_uncertain",
                    uncertainty_evidence,
                )
                mark_result(
                    job["url"],
                    "submission_uncertain",
                    "launcher error after submit phase started",
                    evidence=uncertainty_evidence,
                )
                update_state(worker_id, status="submission_uncertain")
            else:
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
         min_score: int = 6, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         agent_backend: str = "claude",
         manual_captcha_relay: bool = False,
         browser_backend: str = "edge",
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
    workers = min(max(1, workers), max(1, profile_worker_cap), 3)
    if (
        requested_browser_backend in {"cloak", "auto"}
        and workers > 1
        and os.environ.get("APPLYPILOT_CLOAK_ALLOW_CONCURRENCY") != "1"
    ):
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
        f"browser={requested_browser_backend}, poll every {POLL_INTERVAL}s)..."
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
