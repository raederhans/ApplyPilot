from __future__ import annotations

from dataclasses import replace

import pytest

from applypilot.apply.browser_context_runtime import (
    BrowserContextDrained,
    BrowserContextFeature,
    BrowserContextFeatureDisabled,
    BrowserContextLeaseError,
    BrowserContextRuntimeError,
    BrowserStateScope,
    HotBrowserContextRuntime,
    ScopedBrowserState,
)


class FakePage:
    frames = (object(),)


class FakeContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.service_workers: list[object] = []
        self.closed = False

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True
        self.pages.clear()
        self.service_workers.clear()


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.closed = False

    def new_context(self, **_kwargs: object) -> FakeContext:
        context = FakeContext()
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.closed = True


class FailingBrowser(FakeBrowser):
    def new_context(self, **_kwargs: object) -> FakeContext:
        raise RuntimeError("synthetic new_context failure")


def _scope() -> BrowserStateScope:
    return BrowserStateScope("workday", "tenant.myworkdayjobs.com", "candidate-1")


def _state() -> ScopedBrowserState:
    return ScopedBrowserState(
        _scope(),
        {
            "cookies": [
                {
                    "name": "session",
                    "value": "redacted",
                    "domain": "tenant.myworkdayjobs.com",
                    "path": "/",
                }
            ],
            "origins": [
                {
                    "origin": "https://tenant.myworkdayjobs.com",
                    "localStorage": [{"name": "session", "value": "redacted"}],
                }
            ],
        },
    )


def test_feature_is_disabled_by_default_and_does_not_launch() -> None:
    launches: list[object] = []
    runtime = HotBrowserContextRuntime(
        feature=BrowserContextFeature(),
        launch_browser=lambda: launches.append(object()),  # type: ignore[arg-type]
    )

    with pytest.raises(BrowserContextFeatureDisabled):
        runtime.start()

    assert launches == []


def test_exact_scope_import_is_fresh_per_application_and_closes_before_release() -> None:
    browser = FakeBrowser()
    runtime = HotBrowserContextRuntime(
        feature=BrowserContextFeature(True),
        launch_browser=lambda: browser,
    )
    scope = _scope()
    first = runtime.open_application(application_id="application-1", scope=scope, state=_state())
    first_page = runtime.new_page(first)
    second = runtime.open_application(application_id="application-2", scope=scope, state=_state())
    second_page = runtime.new_page(second)

    assert first.context_id != second.context_id
    assert first_page is not second_page
    runtime.close_application(first)
    runtime.close_application(second)

    assert all(context.closed for context in browser.contexts)
    assert runtime.metrics.contexts_created == 2
    assert runtime.metrics.contexts_closed == 2
    assert runtime.metrics.active_contexts == 0
    assert runtime.metrics.pages_after_close == 0
    assert runtime.metrics.frames_after_close == 0
    assert runtime.metrics.service_workers_after_close == 0
    with pytest.raises(BrowserContextLeaseError, match="released"):
        runtime.new_page(first)
    with pytest.raises(BrowserContextLeaseError, match="cannot reuse"):
        runtime.open_application(application_id="application-1", scope=scope, state=_state())


@pytest.mark.parametrize(
    "state",
    (
        {"cookies": [{"name": "session", "value": "x", "domain": ".myworkdayjobs.com"}], "origins": []},
        {"cookies": [{"name": "session", "value": "x", "domain": "other.myworkdayjobs.com"}], "origins": []},
        {"cookies": [], "origins": [{"origin": "https://other.myworkdayjobs.com"}]},
        {"cookies": [], "origins": [], "profile_path": "C:/browser-profile"},
    ),
)
def test_import_rejects_parent_cross_host_and_profile_wide_state(state: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="(exact|unsupported)"):
        ScopedBrowserState(_scope(), state)


def test_scope_account_provider_or_host_mismatch_fails_closed() -> None:
    browser = FakeBrowser()
    launches: list[FakeBrowser] = []
    runtime = HotBrowserContextRuntime(
        feature=BrowserContextFeature(True),
        launch_browser=lambda: (launches.append(browser) or browser),
    )
    scope = _scope()
    state = _state()

    for mismatch in (
        replace(scope, provider="greenhouse"),
        replace(scope, host="other.myworkdayjobs.com"),
        replace(scope, account_id="candidate-2"),
    ):
        with pytest.raises(BrowserContextRuntimeError, match="scope"):
            runtime.open_application(application_id=f"application-{mismatch.account_id}", scope=mismatch, state=state)
    assert launches == []
    assert browser.contexts == []


def test_context_creation_failure_drains_the_owned_hot_process() -> None:
    browser = FailingBrowser()
    runtime = HotBrowserContextRuntime(
        feature=BrowserContextFeature(True),
        launch_browser=lambda: browser,
    )

    with pytest.raises(BrowserContextRuntimeError, match="creation failed; runtime drained"):
        runtime.open_application(application_id="application-1", scope=_scope(), state=_state())

    assert browser.closed is True
    assert runtime.metrics.drained is True


def test_taint_score_closes_the_tainted_context_then_drains_hot_process() -> None:
    browser = FakeBrowser()
    runtime = HotBrowserContextRuntime(
        feature=BrowserContextFeature(True),
        launch_browser=lambda: browser,
        drain_threshold=2,
    )
    scope = _scope()
    first = runtime.open_application(application_id="application-1", scope=scope, state=_state())
    runtime.taint(first, reason="unknown_effect")
    second = runtime.open_application(application_id="application-2", scope=scope, state=_state())
    runtime.taint(second, reason="unknown_effect")

    assert browser.closed is True
    assert runtime.metrics.taint_score == 2
    assert runtime.metrics.drained is True
    assert runtime.metrics.active_contexts == 0
    with pytest.raises(BrowserContextDrained):
        runtime.open_application(application_id="application-3", scope=scope, state=_state())
