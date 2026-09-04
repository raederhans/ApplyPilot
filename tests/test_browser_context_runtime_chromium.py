"""Offline Chromium proof for the hot-process/per-application-context seam."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

playwright_sync_api = pytest.importorskip("playwright.sync_api")

from applypilot.apply.browser_context_runtime import (
    BrowserContextFeature,
    BrowserStateScope,
    HotBrowserContextRuntime,
    ScopedBrowserState,
)

pytestmark = pytest.mark.browser


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/sw.js":
            body = b"self.addEventListener('install', event => self.skipWaiting());"
            content_type = "application/javascript"
        elif self.path == "/application":
            body = b"""<!doctype html><title>loading</title><iframe srcdoc='<p>frame</p>'></iframe>
<script>
(async () => {
  await navigator.serviceWorker.register('/sw.js');
  await navigator.serviceWorker.ready;
  document.title = 'application-ready';
})();
</script>"""
            content_type = "text/html"
        else:
            body = b"<!doctype html><title>blank</title>"
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@contextmanager
def _fixture_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_ten_sequential_applications_leave_no_page_frame_storage_or_service_worker_residuals() -> None:
    sync_playwright = playwright_sync_api.sync_playwright
    with _fixture_server() as fixture_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        launches = 0

        def launch_browser():
            nonlocal launches
            launches += 1
            return browser

        runtime = HotBrowserContextRuntime(
            feature=BrowserContextFeature(True),
            launch_browser=launch_browser,
        )
        scope = BrowserStateScope("synthetic", "127.0.0.1", "synthetic-account")
        state = ScopedBrowserState(
            scope,
            {
                "cookies": [
                    {
                        "name": "auth_seed",
                        "value": "authenticated",
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ],
                "origins": [],
            },
        )

        for index in range(10):
            lease = runtime.open_application(
                application_id=f"synthetic-application-{index}",
                scope=scope,
                state=state,
            )
            page = runtime.new_page(lease)
            page.goto(f"{fixture_url}/blank")
            cookies = {cookie["name"]: cookie["value"] for cookie in page.context.cookies()}
            assert cookies["auth_seed"] == "authenticated"
            assert "application_mutation" not in cookies
            assert page.evaluate("localStorage.getItem('application')") is None
            assert page.evaluate("navigator.serviceWorker.getRegistrations().then(items => items.length)") == 0
            page.context.add_cookies(
                [
                    {
                        "name": "application_mutation",
                        "value": f"application-{index}",
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )
            page.evaluate(f"localStorage.setItem('application', 'application-{index}')")
            page.goto(f"{fixture_url}/application")
            page.wait_for_function("document.title === 'application-ready'")
            assert page.evaluate("navigator.serviceWorker.getRegistrations().then(items => items.length)") == 1

            runtime.close_application(lease)
            assert browser.contexts == []
            assert browser.is_connected()

        metrics = runtime.metrics
        assert launches == 1
        assert metrics.contexts_created == 10
        assert metrics.contexts_closed == 10
        assert metrics.active_contexts == 0
        assert metrics.pages_before_close == 10
        assert metrics.frames_before_close >= 20
        assert metrics.service_workers_before_close >= 10
        assert (
            metrics.pages_after_close,
            metrics.frames_after_close,
            metrics.service_workers_after_close,
        ) == (0, 0, 0)
        runtime.close()
        assert not browser.is_connected()
