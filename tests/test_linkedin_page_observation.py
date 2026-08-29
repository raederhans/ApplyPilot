from __future__ import annotations

import pytest

from applypilot.apply import page_observation

_ENCODED_JOB = (
    "https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4455274411%2F"
)
_ENCODED_SLUG_JOB = (
    "https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2Fdata-role-4455274411"
)
_DOUBLE_ENCODED_JOB = (
    "https%253A%252F%252Fwww.linkedin.com%252Fjobs%252Fview%252F"
    "4455274411%252F"
)


def test_linkedin_job_id_requires_full_path_identity() -> None:
    assert page_observation._linkedin_job_id(
        "https://www.linkedin.com/jobs/view/1234/"
    ) == "1234"
    assert page_observation._linkedin_page_matches_job_id(
        "https://www.linkedin.com/jobs/view/12345/", "1234"
    ) is False
    assert page_observation._linkedin_job_id(
        "https://www.linkedin.com/jobs/view/data-analyst-123456789"
    ) == "123456789"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            f"https://www.linkedin.com/authwall?trk=bf&sessionRedirect={_ENCODED_JOB}",
            "4455274411",
        ),
        (
            f"https://sg.linkedin.com/authwall/?sessionRedirect={_ENCODED_SLUG_JOB}",
            "4455274411",
        ),
        (
            "https://www.linkedin.com/authwall?sessionRedirect="
            + _ENCODED_JOB.replace("4455274411", "4455274422"),
            "4455274422",
        ),
    ],
)
def test_linkedin_authwall_extracts_one_exact_redirect_job_id(
    url: str, expected: str
) -> None:
    assert page_observation._linkedin_authwall_redirect_job_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.linkedin.com/authwall?sessionRedirect={_ENCODED_JOB}",
        f"https://example.com/authwall?sessionRedirect={_ENCODED_JOB}",
        f"https://www.linkedin.com/login?sessionRedirect={_ENCODED_JOB}",
        "https://www.linkedin.com/authwall?sessionRedirect=%2Fjobs%2Fview%2F4455274411%2F",
        "https://www.linkedin.com/authwall?sessionRedirect=http%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4455274411%2F",
        "https://www.linkedin.com/authwall?sessionRedirect=https%3A%2F%2Fexample.com%2Fjobs%2Fview%2F4455274411%2F",
        f"https://www.linkedin.com/authwall?sessionRedirect={_DOUBLE_ENCODED_JOB}",
        f"https://www.linkedin.com/authwall?sessionRedirect={_ENCODED_JOB}&sessionRedirect={_ENCODED_JOB}",
        f"https://www.linkedin.com/authwall#fragment?sessionRedirect={_ENCODED_JOB}",
    ],
)
def test_linkedin_authwall_rejects_unbound_or_ambiguous_redirects(url: str) -> None:
    assert page_observation._linkedin_authwall_redirect_job_id(url) == ""


def test_linkedin_causal_entry_admits_only_direct_bound_authwall(
    monkeypatch,
) -> None:
    class Page:
        url = (
            "https://www.linkedin.com/authwall?sessionRedirect=https%3A%2F%2F"
            "www.linkedin.com%2Fjobs%2Fview%2F4455274411%2F"
        )

    page = Page()

    class Browser:
        def __init__(self) -> None:
            self.contexts = [type("Context", (), {"pages": [page]})()]

        @staticmethod
        def new_browser_cdp_session():
            return object()

    class Chromium:
        @staticmethod
        def connect_over_cdp(_endpoint: str):
            return Browser()

    class Playwright:
        chromium = Chromium()

        @staticmethod
        def start():
            return Playwright()

        @staticmethod
        def stop() -> None:
            return None

    monkeypatch.setattr("playwright.sync_api.sync_playwright", Playwright)
    monkeypatch.setattr(
        page_observation, "_bound_application_pages", lambda *_args: [page]
    )
    monkeypatch.setattr(page_observation, "_page_target_id", lambda _page: "root")
    monkeypatch.setattr(
        page_observation,
        "_target_infos",
        lambda _session: {"root": {"targetId": "root", "url": page.url}},
    )
    monkeypatch.setattr(
        page_observation,
        "_wait_for_linkedin_main_apply_control",
        lambda _page: pytest.fail("authwall admission must not click Apply"),
    )
    job = {
        "url": "https://www.linkedin.com/jobs/view/4455274411/",
        "application_url": "https://www.linkedin.com/jobs/view/4455274411/",
        "_browser_root_target_ids": ["root"],
    }

    signal, observation = page_observation._click_linkedin_main_apply_causally(
        9432, 0, job
    )

    assert signal is None
    assert observation == {
        "disposition": "linkedin_login_required",
        "stage": "pre_entry_authwall",
    }
    assert job["_linkedin_login_source_job_id"] == "4455274411"
    assert job["_linkedin_login_entry_stage"] == "pre_entry_authwall"


def test_linkedin_causal_entry_rejects_authwall_outside_direct_root(
    monkeypatch,
) -> None:
    class Page:
        url = (
            "https://www.linkedin.com/authwall?sessionRedirect=https%3A%2F%2F"
            "www.linkedin.com%2Fjobs%2Fview%2F4455274411%2F"
        )

    page = Page()

    class Browser:
        def __init__(self) -> None:
            self.contexts = [type("Context", (), {"pages": [page]})()]

    class Chromium:
        @staticmethod
        def connect_over_cdp(_endpoint: str):
            return Browser()

    class Playwright:
        chromium = Chromium()

        @staticmethod
        def start():
            return Playwright()

        @staticmethod
        def stop() -> None:
            return None

    monkeypatch.setattr("playwright.sync_api.sync_playwright", Playwright)
    monkeypatch.setattr(
        page_observation, "_bound_application_pages", lambda *_args: [page]
    )
    monkeypatch.setattr(page_observation, "_page_target_id", lambda _page: "child")
    job = {
        "url": "https://www.linkedin.com/jobs/view/4455274411/",
        "application_url": "https://www.linkedin.com/jobs/view/4455274411/",
        "_browser_root_target_ids": ["root"],
    }

    signal, observation = page_observation._click_linkedin_main_apply_causally(
        9432, 0, job
    )

    assert signal == "linkedin_apply_click:source_root_mismatch"
    assert observation == {}


def test_causal_apply_rejects_preexisting_or_unbound_external_target() -> None:
    before = {
        "root": {"targetId": "root", "url": "https://www.linkedin.com/jobs/view/1234/"},
        "old": {"targetId": "old", "url": "https://boards.greenhouse.io/acme/jobs/old"},
    }
    after = {
        **before,
        "agent-nav": {
            "targetId": "agent-nav",
            "url": "https://boards.greenhouse.io/acme/jobs/other",
            "openerId": "unrelated",
        },
    }

    assert page_observation._classify_linkedin_causal_target(
        before, after, source_target_id="root"
    ) == (None, "linkedin_apply_click:no_causal_external_target")


def test_target_snapshot_only_corroborates_same_target_navigation() -> None:
    before = {
        "root": {"targetId": "root", "url": "https://www.linkedin.com/jobs/view/1234/"}
    }
    after = {
        "root": {
            "targetId": "root",
            "url": "https://hp.wd5.myworkdayjobs.com/External/job/role",
        }
    }

    attested, reason = page_observation._classify_linkedin_causal_target(
        before, after, source_target_id="root"
    )

    assert reason == "linkedin_apply_click:causal_external_target"
    assert attested["target_id"] == "root"
    assert attested["mode"] == "same_target_navigation"


def test_target_snapshot_only_corroborates_popup_with_source_opener() -> None:
    before = {
        "root": {"targetId": "root", "url": "https://www.linkedin.com/jobs/view/1234/"}
    }
    after = {
        **before,
        "popup": {
            "targetId": "popup",
            "url": "https://jobs.smartrecruiters.com/Acme/1234-role",
            "openerId": "root",
        },
    }

    attested, reason = page_observation._classify_linkedin_causal_target(
        before, after, source_target_id="root"
    )

    assert reason == "linkedin_apply_click:causal_external_target"
    assert attested["target_id"] == "popup"
    assert attested["mode"] == "new_popup_from_source"


def test_linkedin_external_page_identity_is_bounded() -> None:
    class Page:
        def evaluate(self, _script: str) -> dict:
            return {
                "page_title": "T" * 500,
                "primary_headings": ["H" * 500 for _ in range(20)],
            }

    identity = page_observation._linkedin_external_page_identity(Page())

    assert identity["version"] == 1
    assert len(identity["page_title"]) == 300
    assert len(identity["primary_headings"]) == 6
    assert all(len(value) == 300 for value in identity["primary_headings"])


def test_linkedin_causal_click_waits_for_delayed_public_top_card() -> None:
    class Page:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def wait_for_function(self, script: str, *, timeout: int) -> None:
            self.calls.append((script, timeout))

    page = Page()
    page_observation._wait_for_linkedin_main_apply_control(page)

    assert len(page.calls) == 1
    script, timeout = page.calls[0]
    assert ".top-card-layout__card" in script
    assert ".top-card-layout" in script
    assert "topCard.querySelectorAll" in script
    assert "document.querySelectorAll('a[href" not in script
    assert 1_000 <= timeout <= 15_000


def test_linkedin_public_apply_handle_uses_only_top_card_cta() -> None:
    class Handle:
        def as_element(self):
            return self

    class Page:
        def evaluate_handle(self, script: str):
            assert ".top-card-layout__card" in script
            assert "topCard.querySelectorAll('.top-card-layout__cta')" in script
            assert "document.querySelectorAll('.top-card-layout__cta')" not in script
            assert "apply\\b" in script
            return Handle()

    result = page_observation._linkedin_main_apply_handle(Page())

    assert result is not None


def test_linkedin_public_apply_handle_accepts_explicit_chinese_cta() -> None:
    class Handle:
        def as_element(self):
            return self

    class Page:
        def evaluate_handle(self, script: str):
            assert "申请" in script
            assert "轻松" in script
            assert "topCard.querySelectorAll" in script
            return Handle()

    assert page_observation._linkedin_main_apply_handle(Page()) is not None


def test_linkedin_app_promo_dismissal_is_exact_and_pre_application() -> None:
    calls: list[object] = []

    class Control:
        def click(self, *, timeout: int) -> None:
            calls.append(("click", timeout))

    class Handle:
        @staticmethod
        def as_element():
            return Control()

    class Page:
        @staticmethod
        def evaluate_handle(script: str):
            assert "[role=\"dialog\"].cta-modal" in script
            assert "LinkedIn is better on the app" in script
            assert "^Dismiss$" in script
            assert "form, input, textarea, select" in script
            return Handle()

        @staticmethod
        def wait_for_function(script: str, *, timeout: int) -> None:
            assert "cta-modal" in script
            calls.append(("wait", timeout))

    assert page_observation._dismiss_linkedin_app_promo(Page()) is True
    assert calls == [("click", 5_000), ("wait", 5_000)]


def test_linkedin_causal_entry_rejects_job_drift_after_app_promo(
    monkeypatch,
) -> None:
    class Page:
        url = "https://www.linkedin.com/jobs/view/4455274411/"

    page = Page()

    class Browser:
        def __init__(self) -> None:
            self.contexts = [type("Context", (), {"pages": [page]})()]

    class Chromium:
        @staticmethod
        def connect_over_cdp(_endpoint: str):
            return Browser()

    class Playwright:
        chromium = Chromium()

        @staticmethod
        def start():
            return Playwright()

        @staticmethod
        def stop() -> None:
            return None

    apply_clicks: list[str] = []
    monkeypatch.setattr("playwright.sync_api.sync_playwright", Playwright)
    monkeypatch.setattr(
        page_observation, "_bound_application_pages", lambda *_args: [page]
    )
    monkeypatch.setattr(page_observation, "_page_target_id", lambda _page: "root")
    monkeypatch.setattr(
        page_observation, "_wait_for_linkedin_main_apply_control", lambda _page: None
    )

    def dismiss_and_drift(_page) -> bool:
        page.url = "https://www.linkedin.com/jobs/view/4455274422/"
        return True

    monkeypatch.setattr(
        page_observation, "_dismiss_linkedin_app_promo", dismiss_and_drift
    )
    monkeypatch.setattr(
        page_observation,
        "_linkedin_click_page_state",
        lambda _page: pytest.fail("job drift must stop before login inspection"),
    )
    monkeypatch.setattr(
        page_observation,
        "_linkedin_main_apply_handle",
        lambda _page: apply_clicks.append("apply"),
    )
    job = {
        "url": "https://www.linkedin.com/jobs/view/4455274411/",
        "application_url": "https://www.linkedin.com/jobs/view/4455274411/",
        "_browser_root_target_ids": ["root"],
    }

    signal, observation = page_observation._click_linkedin_main_apply_causally(
        9432, 0, job
    )

    assert signal == "linkedin_apply_click:source_changed_after_app_promo"
    assert observation == {}
    assert apply_clicks == []
    assert "_linkedin_causal_apply_attestation" not in job


def test_snapshot_without_click_epoch_event_is_not_authoritative() -> None:
    corroborated = {
        "target_id": "root",
        "mode": "same_target_navigation",
        "initial_url": "https://www.linkedin.com/jobs/view/1234/",
        "final_url": "https://jobs.example/role",
    }

    admitted, reason = page_observation._admit_linkedin_causal_events(
        corroborated,
        source_target_id="root",
        navigation_events=[],
        redirect_lineage=[],
        popup_event_count=0,
        popup_candidates=[],
    )

    assert admitted is None
    assert reason == "linkedin_apply_click:no_click_epoch_causal_target"


def test_same_tab_event_preserves_bounded_redirect_lineage() -> None:
    corroborated = {
        "target_id": "root",
        "mode": "same_target_navigation",
        "initial_url": "https://www.linkedin.com/jobs/view/1234/",
        "final_url": "https://jobs.example/final",
    }
    lineage = [
        "https://www.linkedin.com/redir",
        "https://jobs.example/final",
    ]

    admitted, reason = page_observation._admit_linkedin_causal_events(
        corroborated,
        source_target_id="root",
        navigation_events=["https://jobs.example/final"],
        redirect_lineage=lineage,
        popup_event_count=0,
        popup_candidates=[],
    )

    assert reason == "linkedin_apply_click:causal_external_target"
    assert admitted is not None
    assert admitted["redirect_lineage"] == lineage
    assert admitted["lineage_complete"] is True


def test_popup_event_and_same_tab_event_are_ambiguous() -> None:
    corroborated = {
        "target_id": "root",
        "mode": "same_target_navigation",
        "initial_url": "https://www.linkedin.com/jobs/view/1234/",
        "final_url": "https://jobs.example/final",
    }
    popup = {
        "target_id": "popup",
        "mode": "new_popup_from_source",
        "initial_url": "https://ats.example/role",
        "final_url": "https://ats.example/role",
        "redirect_lineage": ["https://ats.example/role"],
        "lineage_complete": True,
    }

    admitted, reason = page_observation._admit_linkedin_causal_events(
        corroborated,
        source_target_id="root",
        navigation_events=["https://jobs.example/final"],
        redirect_lineage=["https://jobs.example/final"],
        popup_event_count=1,
        popup_candidates=[popup],
    )

    assert admitted is None
    assert reason == "linkedin_apply_click:ambiguous_causal_targets"


def test_valid_popup_plus_lost_popup_fails_closed() -> None:
    valid_popup = {
        "target_id": "popup",
        "mode": "new_popup_from_source",
        "initial_url": "https://ats.example/role",
        "final_url": "https://ats.example/role",
        "redirect_lineage": ["https://ats.example/role"],
        "lineage_complete": True,
    }

    admitted, reason = page_observation._admit_linkedin_causal_events(
        None,
        source_target_id="root",
        navigation_events=[],
        redirect_lineage=[],
        popup_event_count=2,
        popup_candidates=[valid_popup],
    )

    assert admitted is None
    assert reason == "linkedin_apply_click:target_lost_or_unclassified"


def test_single_classified_popup_event_is_admitted() -> None:
    valid_popup = {
        "target_id": "popup",
        "mode": "new_popup_from_source",
        "initial_url": "https://ats.example/role",
        "final_url": "https://ats.example/role",
        "redirect_lineage": ["https://ats.example/role"],
        "lineage_complete": True,
    }

    admitted, reason = page_observation._admit_linkedin_causal_events(
        None,
        source_target_id="root",
        navigation_events=[],
        redirect_lineage=[],
        popup_event_count=1,
        popup_candidates=[valid_popup],
    )

    assert reason == "linkedin_apply_click:causal_external_target"
    assert admitted == valid_popup


def test_valid_same_tab_plus_lost_popup_fails_closed() -> None:
    corroborated = {
        "target_id": "root",
        "mode": "same_target_navigation",
        "initial_url": "https://www.linkedin.com/jobs/view/123456/",
        "final_url": "https://jobs.example/final",
    }

    admitted, reason = page_observation._admit_linkedin_causal_events(
        corroborated,
        source_target_id="root",
        navigation_events=["https://jobs.example/final"],
        redirect_lineage=["https://jobs.example/final"],
        popup_event_count=1,
        popup_candidates=[],
    )

    assert admitted is None
    assert reason == "linkedin_apply_click:target_lost_or_unclassified"


def test_native_surface_cannot_mask_lost_popup() -> None:
    signal, disposition = page_observation._resolve_linkedin_click_epoch(
        None,
        "linkedin_apply_click:target_lost_or_unclassified",
        source_job_page_matches=True,
        login_surface=False,
        native_surface=True,
    )

    assert signal == "linkedin_apply_click:target_lost_or_unclassified"
    assert disposition == ""


def test_login_surface_cannot_mask_lost_popup() -> None:
    signal, disposition = page_observation._resolve_linkedin_click_epoch(
        None,
        "linkedin_apply_click:target_lost_or_unclassified",
        source_job_page_matches=True,
        login_surface=True,
        native_surface=False,
    )

    assert signal == "linkedin_apply_click:target_lost_or_unclassified"
    assert disposition == ""


def test_external_popup_and_native_surface_are_ambiguous() -> None:
    signal, disposition = page_observation._resolve_linkedin_click_epoch(
        {"target_id": "popup", "mode": "new_popup_from_source"},
        "linkedin_apply_click:causal_external_target",
        source_job_page_matches=True,
        login_surface=False,
        native_surface=True,
    )

    assert signal == "linkedin_apply_click:ambiguous_click_epoch_results"
    assert disposition == ""


def test_external_popup_and_login_surface_are_ambiguous() -> None:
    signal, disposition = page_observation._resolve_linkedin_click_epoch(
        {"target_id": "popup", "mode": "new_popup_from_source"},
        "linkedin_apply_click:causal_external_target",
        source_job_page_matches=True,
        login_surface=True,
        native_surface=False,
    )

    assert signal == "linkedin_apply_click:ambiguous_click_epoch_results"
    assert disposition == ""
