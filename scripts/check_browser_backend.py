#!/usr/bin/env python3
"""Run a non-submitting CDP smoke test against an ApplyPilot browser backend."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from applypilot.apply import chrome
from applypilot.apply.profile_lock import sidecar_path_for_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("edge", "cloak"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--worker-id", type=int, default=90)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--hold-seconds",
        type=int,
        default=0,
        help="Keep the CDP browser alive briefly for an external MCP probe",
    )
    parser.add_argument("url", nargs="+", help="Public HTTP(S) page(s) to inspect")
    return parser


def _inspect_page(page, url: str) -> dict[str, object]:
    if not url.startswith(("http://", "https://")):
        raise ValueError("smoke-test URLs must use HTTP(S)")

    response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1_000)
    interaction = None
    if "selenium.dev/selenium/web/web-form.html" in page.url:
        field = page.locator('input[name="my-text"]')
        field.fill("ApplyPilot browser smoke")
        interaction = field.input_value()

    browser_state = page.evaluate(
        """() => ({
            webdriver: navigator.webdriver,
            userAgent: navigator.userAgent,
            platform: navigator.platform,
            pluginCount: navigator.plugins.length,
            hasChromeObject: Boolean(window.chrome)
        })"""
    )
    body_text = page.locator("body").inner_text(timeout=10_000)
    interesting_lines = []
    for line in body_text.splitlines():
        cleaned = " ".join(line.split())
        if cleaned and any(
            token in cleaned.casefold()
            for token in ("bot", "headless", "selenium", "webdriver", "automation")
        ):
            interesting_lines.append(cleaned[:300])
        if len(interesting_lines) == 20:
            break
    return {
        "requestedUrl": url,
        "finalUrl": page.url,
        "status": response.status if response else None,
        "title": page.title(),
        "interactionValue": interaction,
        "bodyExcerpt": " ".join(body_text.split())[:500],
        "diagnosticLines": interesting_lines,
        **browser_state,
    }


def main() -> int:
    args = _parser().parse_args()
    process = None
    report: dict[str, object] = {
        "backend": args.backend,
        "port": args.port,
        "headless": args.headless,
        "pages": [],
    }
    exit_code = 0
    try:
        process = chrome.launch_chrome(
            args.worker_id,
            port=args.port,
            headless=args.headless,
            start_url="about:blank",
            browser_backend=args.backend,
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
            context = browser.contexts[0]
            page = context.pages[-1] if context.pages else context.new_page()
            report["pages"] = [_inspect_page(page, url) for url in args.url]
        report["ok"] = True
        if args.hold_seconds > 0:
            report["heldForSeconds"] = args.hold_seconds
            time.sleep(args.hold_seconds)
    except Exception as exc:  # noqa: BLE001 - diagnostic must emit structured failure
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 1
    finally:
        profile = chrome.resolve_worker_profile_path(args.worker_id, args.backend)
        cleanup_error = None
        try:
            chrome.cleanup_worker(args.worker_id, process)
        except Exception as exc:  # noqa: BLE001 - preserve diagnostic output
            cleanup_error = f"{type(exc).__name__}: {exc}"
        sidecar_exists = sidecar_path_for_profile(profile).exists()
        endpoint_reachable = chrome.cdp_endpoint_reachable(args.port)
        bootstrap_stopped = process is None or process.poll() is not None
        cleanup_report = {
            "ok": not endpoint_reachable
            and bootstrap_stopped
            and not sidecar_exists
            and cleanup_error is None,
            "endpointReachable": endpoint_reachable,
            "bootstrapStopped": bootstrap_stopped,
            "sidecarExists": sidecar_exists,
            "error": cleanup_error,
            "profile": str(profile),
        }
        report["cleanup"] = cleanup_report
        if not cleanup_report["ok"]:
            report["ok"] = False
            report["error"] = report.get("error") or "Browser cleanup was not confirmed"
            exit_code = 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
