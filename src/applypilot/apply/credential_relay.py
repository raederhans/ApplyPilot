"""Fill ATS credentials without exposing the password to the browser agent.

The password is encrypted for the current Windows user by PowerShell DPAPI.
This helper decrypts it only inside a short-lived child process, connects to the
already-running Edge instance over CDP, fills visible fields, and never submits.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Frame, Locator, Page, sync_playwright

from applypilot import config

KNOWN_ATS_HOSTS = {
    "ashbyhq.com",
    "bamboohr.com",
    "greenhouse.io",
    "greenhouse.com",
    "icims.com",
    "jobvite.com",
    "lever.co",
    "myworkdayjobs.com",
    "oraclecloud.com",
    "smartrecruiters.com",
    "successfactors.com",
    "workable.com",
    "workday.com",
    "workdayjobs.com",
}

BLOCKED_IDENTITY_HOSTS = {
    "accounts.google.com",
    "appleid.apple.com",
    "login.live.com",
    "login.microsoftonline.com",
    "okta.com",
}

EMAIL_SELECTORS = (
    'input[type="email"]',
    'input[autocomplete="email"]',
    'input[autocomplete="username"]',
    'input[name*="email" i]',
    'input[id*="email" i]',
)
PASSWORD_SELECTORS = (
    'input[type="password"]',
    'input[autocomplete="new-password"]',
    'input[autocomplete="current-password"]',
)


class CredentialRelayError(RuntimeError):
    """Expected, user-safe credential relay failure."""


def _host_matches(host: str, candidate: str) -> bool:
    host = host.lower().strip(".")
    candidate = candidate.lower().strip(".")
    return host == candidate or host.endswith(f".{candidate}")


def host_is_allowed(host: str, configured_hosts: set[str]) -> bool:
    """Return whether a host is an exact authorized employer credential target.

    Known ATS domains are deliberately *not* globally authorized here.  They
    are considered only by the unique-redirect fallback in ``_fill_fields``.
    This keeps a second, unrelated ATS tab from receiving credentials.
    """
    normalized = host.lower().strip(".")
    if not normalized:
        return False
    if any(_host_matches(normalized, blocked) for blocked in BLOCKED_IDENTITY_HOSTS):
        return False
    return any(
        _host_matches(normalized, candidate)
        for candidate in configured_hosts
    )


def host_is_known_ats(host: str) -> bool:
    """Return whether a non-identity host belongs to a supported ATS family."""
    normalized = host.lower().strip(".")
    if not normalized:
        return False
    if any(_host_matches(normalized, blocked) for blocked in BLOCKED_IDENTITY_HOSTS):
        return False
    return any(_host_matches(normalized, candidate) for candidate in KNOWN_ATS_HOSTS)


def _select_candidate(candidates: list[dict[str, object]]) -> dict[str, object]:
    """Choose one unambiguous page/frame candidate.

    Exact configured-host candidates win.  A known-ATS redirect is accepted
    only when the caller explicitly enabled it and exactly one browser page
    contains eligible credential fields.  Multiple matching tabs fail closed
    instead of selecting whichever Playwright happened to enumerate first.
    """
    exact = [candidate for candidate in candidates if candidate["match"] == "exact"]
    eligible = exact or [
        candidate for candidate in candidates if candidate["match"] == "known_ats_redirect"
    ]
    if not eligible:
        raise CredentialRelayError(
            "No visible editable credential field was found on the authorized job page."
        )

    page_indexes = {int(candidate["page_index"]) for candidate in eligible}
    if len(page_indexes) != 1:
        raise CredentialRelayError(
            "Credential relay found matching fields on multiple browser tabs; close or "
            "disambiguate the unrelated ATS tab and retry."
        )

    ranked = sorted(
        eligible,
        key=lambda candidate: (
            int(candidate.get("field_count", 0)),
            candidate.get("frame_url") == candidate.get("page_url"),
        ),
        reverse=True,
    )
    best = ranked[0]
    tied = [
        candidate
        for candidate in ranked
        if candidate.get("field_count") == best.get("field_count")
        and (candidate.get("frame_url") == candidate.get("page_url"))
        == (best.get("frame_url") == best.get("page_url"))
    ]
    if len(tied) > 1:
        raise CredentialRelayError(
            "Credential relay found equally plausible credential forms in one tab; retry "
            "after the page finishes navigating."
        )
    return best


def _credential_path() -> Path:
    configured = os.environ.get("APPLYPILOT_ATS_CREDENTIAL_FILE")
    if configured:
        return Path(configured)
    return config.APP_DIR / "credentials" / "ats-signup.json"


def _read_record(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise CredentialRelayError(
            "ATS credential relay is not configured. Run set-ats-credentials.ps1 locally."
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialRelayError("ATS credential record is unreadable.") from exc

    email = str(record.get("email", "")).strip().lower()
    encrypted = str(record.get("password_dpapi", "")).strip()
    if not email or not encrypted:
        raise CredentialRelayError("ATS credential record is incomplete.")
    return {"email": email, "password_dpapi": encrypted}


def _decrypt_password(path: Path) -> str:
    """Decrypt the DPAPI value without placing plaintext in argv or output logs."""
    powershell = os.environ.get("COMSPEC_POWERSHELL", "powershell.exe")
    script = r"""
$ErrorActionPreference = 'Stop'
$record = Get-Content -LiteralPath $env:APPLYPILOT_ATS_CREDENTIAL_FILE -Raw | ConvertFrom-Json
$secure = ConvertTo-SecureString -String $record.password_dpapi
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  [Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr))
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}
"""
    child_env = os.environ.copy()
    child_env["APPLYPILOT_ATS_CREDENTIAL_FILE"] = str(path)
    # A Python process launched from PowerShell 7 may inherit a PSModulePath
    # that contains only pwsh module roots. Windows PowerShell then cannot
    # autoload Microsoft.PowerShell.Security, which makes DPAPI decryption fail
    # even for the same Windows user. Point the child at the built-in Windows
    # PowerShell modules explicitly.
    system_root = child_env.get("SystemRoot", r"C:\Windows")
    child_env["PSModulePath"] = str(
        Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialRelayError("Windows DPAPI decryption could not be started.") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise CredentialRelayError(
            "Windows DPAPI could not decrypt the ATS credential for this user."
        )
    return completed.stdout


def _visible_locators(frame: Frame, selectors: tuple[str, ...]) -> list[Locator]:
    locator = frame.locator(", ".join(selectors))
    visible: list[Locator] = []
    for index in range(min(locator.count(), 10)):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible() and candidate.is_editable():
                visible.append(candidate)
        except Exception:  # noqa: BLE001, S112 - detached frames are expected during navigation
            continue
    return visible


def _visible_locator(frame: Frame, selectors: tuple[str, ...]) -> Locator | None:
    visible = _visible_locators(frame, selectors)
    return visible[0] if visible else None


def _fill_password_fields(password_fields: list[Locator], password: str) -> int:
    """Fill an ordinary ATS password plus its optional confirmation field."""
    if len(password_fields) > 2:
        raise CredentialRelayError(
            "Credential relay found more than two password fields and refused a "
            "possible password-change or recovery form."
        )
    for field in password_fields:
        field.fill(password)
    return len(password_fields)


def _requested_fields_present(field: str, email_present: bool, password_present: bool) -> bool:
    if field == "both":
        return email_present and password_present
    if field == "email":
        return email_present
    return password_present


def _candidate_pages(pages: list[Page]) -> list[Page]:
    return sorted(
        (page for page in pages if page.url and page.url != "about:blank"),
        key=lambda page: page.url.startswith("https://"),
        reverse=True,
    )


def _allowed_hosts() -> set[str]:
    raw = os.environ.get("APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS", "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _password_allowed_hosts() -> set[str]:
    raw = os.environ.get("APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS", "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _relay_is_authorized() -> bool:
    return os.environ.get("APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED", "").strip() == "1"


def _password_host_is_allowed(host: str) -> bool:
    return host_is_known_ats(host) or host_is_allowed(host, _password_allowed_hosts())


def _known_ats_redirect_enabled() -> bool:
    return os.environ.get("APPLYPILOT_CREDENTIAL_ALLOW_KNOWN_ATS_REDIRECT", "").strip() == "1"


def _root_target_ids() -> set[str]:
    raw = os.environ.get("APPLYPILOT_CREDENTIAL_ROOT_TARGET_IDS", "")
    return {value.strip() for value in raw.split(",") if value.strip()}


def _target_descends_from(
    target_id: str,
    root_target_ids: set[str],
    target_infos: dict[str, dict[str, object]],
) -> bool:
    """Accept the original application CDP target or an opener descendant."""
    current = target_id
    visited: set[str] = set()
    while current and current not in visited:
        if current in root_target_ids:
            return True
        visited.add(current)
        current = str(target_infos.get(current, {}).get("openerId") or "")
    return False


def _fill_fields(cdp_port: int, field: str, email: str, password: str) -> dict[str, object]:
    if not _relay_is_authorized():
        raise CredentialRelayError("Credential relay is not authorized by the trusted profile.")
    configured_hosts = _allowed_hosts()
    if not configured_hosts:
        raise CredentialRelayError("No authorized employer/ATS host was configured for this job.")
    root_target_ids = _root_target_ids()
    if not root_target_ids:
        raise CredentialRelayError(
            "Credential relay could not bind this request to the worker's application tab."
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        browser_session = browser.new_browser_cdp_session()
        target_infos = {
            str(info.get("targetId") or ""): info
            for info in browser_session.send("Target.getTargets").get("targetInfos", [])
            if info.get("targetId")
        }
        pages = [page for context in browser.contexts for page in context.pages]
        candidates: list[dict[str, object]] = []
        for page_index, page in enumerate(_candidate_pages(pages)):
            try:
                target_info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
                    "targetInfo"
                ]
            except Exception:  # noqa: BLE001, S112 - navigation may detach between snapshots
                continue
            target_id = str(target_info.get("targetId") or "")
            target_infos[target_id] = target_info
            if not _target_descends_from(target_id, root_target_ids, target_infos):
                continue
            page_host = (urlparse(page.url).hostname or "").lower()
            for frame_index, frame in enumerate(page.frames):
                frame_host = (urlparse(frame.url).hostname or page_host).lower()
                target_host = frame_host or page_host
                if host_is_allowed(target_host, configured_hosts):
                    match = "exact"
                elif _known_ats_redirect_enabled() and host_is_known_ats(target_host):
                    match = "known_ats_redirect"
                else:
                    continue
                if field in {"password", "both"} and not (
                    _password_host_is_allowed(target_host)
                ):
                    continue

                email_locator = (
                    _visible_locator(frame, EMAIL_SELECTORS)
                    if field in {"email", "both"}
                    else None
                )
                password_locators = (
                    _visible_locators(frame, PASSWORD_SELECTORS)
                    if field in {"password", "both"}
                    else []
                )
                if len(password_locators) > 2:
                    continue
                if not _requested_fields_present(
                    field,
                    email_locator is not None,
                    bool(password_locators),
                ):
                    continue
                candidates.append(
                    {
                        "page_index": page_index,
                        "target_id": target_id,
                        "frame_index": frame_index,
                        "page_url": page.url,
                        "frame_url": frame.url,
                        "host": target_host,
                        "match": match,
                        "field_count": int(email_locator is not None) + len(password_locators),
                        "email_locator": email_locator,
                        "password_locators": password_locators,
                    }
                )

        selected = _select_candidate(candidates)
        email_locator = selected["email_locator"]
        password_locators = list(selected["password_locators"])
        email_filled = False
        password_fields_filled = 0
        if email_locator is not None:
            email_locator.fill(email)
            email_filled = True
        if password_locators:
            password_fields_filled = _fill_password_fields(password_locators, password)
        return {
            "status": "filled",
            "host": selected["host"],
            "selection": selected["match"],
            "email_filled": email_filled,
            "password_filled": password_fields_filled > 0,
            "password_fields_filled": password_fields_filled,
            "submitted": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Securely fill employer ATS credentials.")
    parser.add_argument("--field", choices=("email", "password", "both"), default="both")
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=int(os.environ.get("APPLYPILOT_CDP_PORT", "9222")),
    )
    args = parser.parse_args(argv)

    path = _credential_path().resolve()
    try:
        record = _read_record(path)
        password = _decrypt_password(path)
        result = _fill_fields(args.cdp_port, args.field, record["email"], password)
    except CredentialRelayError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        password = ""  # best-effort release; plaintext is never printed or persisted

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
