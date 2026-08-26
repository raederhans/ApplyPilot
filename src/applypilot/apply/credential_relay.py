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
    """Return whether a host is an authorized employer/ATS credential target."""
    normalized = host.lower().strip(".")
    if not normalized:
        return False
    if any(_host_matches(normalized, blocked) for blocked in BLOCKED_IDENTITY_HOSTS):
        return False
    return any(
        _host_matches(normalized, candidate)
        for candidate in configured_hosts | KNOWN_ATS_HOSTS
    )


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


def _visible_locator(frame: Frame, selectors: tuple[str, ...]) -> Locator | None:
    for selector in selectors:
        locator = frame.locator(selector)
        for index in range(min(locator.count(), 10)):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible() and candidate.is_editable():
                    return candidate
            except Exception:  # noqa: BLE001, S112 - detached frames are expected during navigation
                continue
    return None


def _candidate_pages(pages: list[Page]) -> list[Page]:
    return sorted(
        (page for page in pages if page.url and page.url != "about:blank"),
        key=lambda page: page.url.startswith("https://"),
        reverse=True,
    )


def _allowed_hosts() -> set[str]:
    raw = os.environ.get("APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS", "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _fill_fields(cdp_port: int, field: str, email: str, password: str) -> dict[str, object]:
    configured_hosts = _allowed_hosts()
    if not configured_hosts:
        raise CredentialRelayError("No authorized employer/ATS host was configured for this job.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        pages = [page for context in browser.contexts for page in context.pages]
        for page in _candidate_pages(pages):
            page_host = (urlparse(page.url).hostname or "").lower()
            for frame in page.frames:
                frame_host = (urlparse(frame.url).hostname or page_host).lower()
                target_host = frame_host or page_host
                if not host_is_allowed(target_host, configured_hosts):
                    continue

                email_locator = (
                    _visible_locator(frame, EMAIL_SELECTORS)
                    if field in {"email", "both"}
                    else None
                )
                password_locator = (
                    _visible_locator(frame, PASSWORD_SELECTORS)
                    if field in {"password", "both"}
                    else None
                )
                if email_locator is None and password_locator is None:
                    continue

                email_filled = False
                password_filled = False
                if email_locator is not None:
                    email_locator.fill(email)
                    email_filled = True
                if password_locator is not None:
                    password_locator.fill(password)
                    password_filled = True
                return {
                    "status": "filled",
                    "host": target_host,
                    "email_filled": email_filled,
                    "password_filled": password_filled,
                    "submitted": False,
                }

    raise CredentialRelayError(
        "No visible editable credential field was found on an authorized employer/ATS page."
    )


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
