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
        runtime = HotBrowserContextRuntime(
            feature=BrowserContextFeature(True),
            launch_browser=lambda: browser,
        )
        scope = BrowserStateScope("synthetic", "127.0.0.1", "synthetic-account")
        state = ScopedBrowserState(scope, {"cookies": [], "origins": []})

        for index in range(10):
            lease = runtime.open_application(
                application_id=f"synthetic-application-{index}",
                scope=scope,
                state=state,
            )
            page = runtime.new_page(lease)
            page.goto(f"{fixture_url}/blank")
            assert page.evaluate("localStorage.getItem('application')") is None
            assert page.evaluate("navigator.serviceWorker.getRegistrations().then(items => items.length)") == 0
            page.evaluate(f"localStorage.setItem('application', 'application-{index}')")
            page.goto(f"{fixture_url}/application")
            page.wait_for_function("document.title === 'application-ready'")
            assert page.evaluate("navigator.serviceWorker.getRegistrations().then(items => items.length)") == 1

            runtime.close_application(lease)
            assert browser.contexts == []

        metrics = runtime.metrics
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
