from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright import sync_api

from applypilot.apply import launcher
from applypilot.apply.authorization import build_bound_manifest
from applypilot.apply.browser_broker import BrowserBroker, StalePageBinding
from applypilot.apply.semantic_browser_ops import SemanticWriteAuthorityIssuer
from applypilot.storage import semantic_browser_writes as journal


class FakeInput:
    def __init__(self, uploaded: Path | None = None, *, input_id: str = "resume") -> None:
        self.uploaded = uploaded
        self.input_id = input_id
        self.set_calls = 0
        self.available = True

    def get_attribute(self, name: str) -> str | None:
        return {
            "id": self.input_id,
            "accept": "application/pdf",
            "required": "",
        }.get(name)

    def evaluate(self, _script: str) -> object:
        return {
            "labels": ["Resume / CV"],
            "ancestor_texts": ["Resume / CV"],
            "container_text": self.uploaded.name if self.uploaded else "",
            "action_text": ["Remove"] if self.uploaded else [],
            "uploaded_filename": self.uploaded.name if self.uploaded else None,
            "uploaded_size": self.uploaded.stat().st_size if self.uploaded else None,
        }

    def set_input_files(self, value: str) -> None:
        self.set_calls += 1
        self.uploaded = Path(value)


class FakeSurface:
    def __init__(self, *inputs: FakeInput) -> None:
        self.inputs = list(inputs)

    def query_selector_all(self, selector: str) -> list[FakeInput]:
        assert selector == 'input[type="file"]'
        return [value for value in self.inputs if value.available]


class InterruptAfterUploadInput(FakeInput):
    def __init__(self) -> None:
        super().__init__()
        self.interrupted = False

    def evaluate(self, script: str) -> object:
        if self.uploaded is not None and not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt
        return super().evaluate(script)


class FakePage:
    def __init__(self, url: str, surface: FakeSurface) -> None:
        self.url = url
        self.surface = surface
        self.front_calls = 0

    def bring_to_front(self) -> None:
        self.front_calls += 1


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.contexts = [SimpleNamespace(pages=[page])]


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = SimpleNamespace(
            connect_over_cdp=lambda _endpoint: browser,
        )
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def _setup(
    monkeypatch,
    tmp_path: Path,
    *inputs: FakeInput,
):
    resume = tmp_path / "Candidate_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n%%EOF")
    job = {
        "url": "https://tenant.wd5.myworkdayjobs.com/job/REQ-1",
        "application_url": "https://tenant.wd5.myworkdayjobs.com/apply/REQ-1",
        "title": "Data Intern",
        "company_name": "Example",
        "location": "Singapore",
        "full_description": "Internship",
        "tailored_resume_path": str(resume),
        "tailor_status": "validated",
        "_attempt_id": "attempt-1",
        "_browser_root_runtime": "edge",
    }
    manifest = build_bound_manifest(
        [job],
        now=datetime.now(UTC),
        ttl_minutes=120,
    )
    job["_authorization_entry"] = dict(manifest["jobs"][0])
    broker = BrowserBroker()
    bundle = broker.acquire_bundle(
        profile_id="edge:worker:0",
        page_id="application:attempt-1",
        owner_id="application:attempt-1",
        scope_id="worker:0",
        attempt_id="attempt-1",
        runtime_id="codex:edge:cdp:9432",
        ttl_seconds=600,
    )
    job["_browser_lease_binding"] = bundle.as_dict()
    surface = FakeSurface(*inputs)
    page = FakePage(job["application_url"], surface)
    browser = FakeBrowser(page)
    fake_playwright = FakePlaywright(browser)
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(launcher, "_browser_broker", broker)
    monkeypatch.setattr(
        launcher,
        "_semantic_write_authority_issuer",
        SemanticWriteAuthorityIssuer(),
    )
    monkeypatch.setattr(launcher, "get_connection", lambda: connection)
    monkeypatch.setattr(
        launcher,
        "load_runtime_settings",
        lambda: SimpleNamespace(application_lease_minutes=10),
    )
    monkeypatch.setattr(
        launcher,
        "_bound_application_pages",
        lambda _browser, pages, _job: pages,
    )
    monkeypatch.setattr(
        launcher.page_observation_mod,
        "_select_application_page_and_frame",
        lambda _pages: (page, surface),
    )
    monkeypatch.setattr(
        sync_api,
        "sync_playwright",
        lambda: SimpleNamespace(start=lambda: fake_playwright),
    )
    audit = {
        "page_url": page.url,
        "repairable_issues": ["resume_not_uploaded"],
    }
    return job, manifest, broker, connection, fake_playwright, audit, resume


def test_launcher_executes_bound_upload_and_advances_epoch_without_submit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    element = FakeInput()
    job, manifest, _broker, connection, playwright, audit, _resume = _setup(
        monkeypatch,
        tmp_path,
        element,
    )

    result = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert result["status"] == "verified"
    assert element.set_calls == 1
    assert job["_browser_lease_binding"]["page_binding"]["page_epoch"] == 1
    records = journal.list_attempt_operations(connection, "attempt-1")
    assert len(records) == 1
    assert records[0].state == "verified"
    assert records[0].dispatch_count == 1
    assert "_submission_gate" not in job
    assert "_submission_gate_binding" not in job
    assert playwright.stop_calls == 1


def test_launcher_preuploaded_resume_converges_with_zero_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    placeholder = FakeInput()
    job, manifest, _broker, connection, _playwright, audit, resume = _setup(
        monkeypatch,
        tmp_path,
        placeholder,
    )
    placeholder.uploaded = resume

    result = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert result["status"] == "replayed"
    assert placeholder.set_calls == 0
    assert journal.list_attempt_operations(connection, "attempt-1")[0].state == (
        "verified"
    )


def test_launcher_ambiguous_resume_target_is_zero_write_and_unjournaled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first = FakeInput(input_id="resume-one")
    second = FakeInput(input_id="resume-two")
    job, manifest, _broker, connection, _playwright, audit, _resume = _setup(
        monkeypatch,
        tmp_path,
        first,
        second,
    )

    result = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert result["status"] == "target_manual"
    assert first.set_calls == second.set_calls == 0
    assert journal.list_attempt_operations(connection, "attempt-1") == []


def test_crash_after_effect_resumes_by_cas_without_second_upload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    element = FakeInput()
    job, manifest, broker, connection, _playwright, audit, _resume = _setup(
        monkeypatch,
        tmp_path,
        element,
    )
    original_advance = broker.advance_page

    def interrupt_after_effect(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(broker, "advance_page", interrupt_after_effect)
    with pytest.raises(KeyboardInterrupt):
        launcher._try_semantic_pre_submit_repair(9432, 0, job, manifest, audit)

    crashed = journal.list_attempt_operations(connection, "attempt-1")[0]
    assert crashed.state == "effect_observed"
    assert element.set_calls == 1

    monkeypatch.setattr(broker, "advance_page", original_advance)
    result = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert result["status"] == "replayed"
    assert element.set_calls == 1
    recovered = journal.list_attempt_operations(connection, "attempt-1")[0]
    assert recovered.state == "verified"
    assert recovered.resulting_page_epoch == 1


def test_postcondition_effect_with_cas_failure_parks_and_never_rewrites(
    monkeypatch,
    tmp_path: Path,
) -> None:
    element = FakeInput()
    job, manifest, broker, connection, _playwright, audit, _resume = _setup(
        monkeypatch,
        tmp_path,
        element,
    )

    def stale_after_effect(*_args, **_kwargs):
        raise StalePageBinding("raced after write")

    monkeypatch.setattr(broker, "advance_page", stale_after_effect)
    first = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )
    second = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert first["status"] == second["status"] == "parked_stale_after_effect"
    assert element.set_calls == 1
    assert journal.list_attempt_operations(connection, "attempt-1")[0].state == (
        "parked_stale_after_effect"
    )


def test_crash_after_upload_with_unobservable_target_parks_without_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    element = InterruptAfterUploadInput()
    job, manifest, _broker, connection, _playwright, audit, _resume = _setup(
        monkeypatch,
        tmp_path,
        element,
    )

    with pytest.raises(KeyboardInterrupt):
        launcher._try_semantic_pre_submit_repair(9432, 0, job, manifest, audit)
    crashed = journal.list_attempt_operations(connection, "attempt-1")[0]
    assert crashed.state == "started"
    assert crashed.dispatch_count == 1
    assert element.set_calls == 1

    element.available = False
    result = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert result["status"] == "parked_side_effect_unknown"
    assert element.set_calls == 1
    parked = journal.list_attempt_operations(connection, "attempt-1")[0]
    assert parked.state == "parked_side_effect_unknown"


def test_interruption_after_dispatch_claim_never_replays_without_no_effect_proof(
    monkeypatch,
    tmp_path: Path,
) -> None:
    element = FakeInput()
    job, manifest, _broker, connection, _playwright, audit, _resume = _setup(
        monkeypatch,
        tmp_path,
        element,
    )
    original_upload = launcher.SemanticBrowserOps.upload_bound_resume

    def interrupt_before_adapter(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        launcher.SemanticBrowserOps,
        "upload_bound_resume",
        interrupt_before_adapter,
    )
    with pytest.raises(KeyboardInterrupt):
        launcher._try_semantic_pre_submit_repair(9432, 0, job, manifest, audit)
    crashed = journal.list_attempt_operations(connection, "attempt-1")[0]
    assert crashed.state == "started"
    assert crashed.dispatch_count == 1
    assert element.set_calls == 0

    monkeypatch.setattr(
        launcher.SemanticBrowserOps,
        "upload_bound_resume",
        original_upload,
    )
    result = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert result["status"] == "parked_side_effect_unknown"
    assert element.set_calls == 0
    parked = journal.list_attempt_operations(connection, "attempt-1")[0]
    assert parked.state == "parked_side_effect_unknown"
    assert parked.reason_code == "process_interrupted_after_dispatch"


def test_feature_disable_after_dispatch_parks_instead_of_legacy_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    element = InterruptAfterUploadInput()
    job, manifest, _broker, connection, _playwright, audit, _resume = _setup(
        monkeypatch,
        tmp_path,
        element,
    )

    with pytest.raises(KeyboardInterrupt):
        launcher._try_semantic_pre_submit_repair(9432, 0, job, manifest, audit)
    monkeypatch.setenv("APPLYPILOT_SEMANTIC_RESUME_UPLOAD", "0")

    result = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert result["status"] == "parked_side_effect_unknown"
    assert result["legacy_fallback_safe"] is False
    assert element.set_calls == 1
    parked = journal.list_attempt_operations(connection, "attempt-1")[0]
    assert parked.state == "parked_side_effect_unknown"
    assert parked.reason_code == "feature_disabled_after_dispatch"


def test_provider_change_after_dispatch_is_attempt_guarded_without_rewrite(
    monkeypatch,
    tmp_path: Path,
) -> None:
    element = InterruptAfterUploadInput()
    job, manifest, _broker, connection, playwright, audit, _resume = _setup(
        monkeypatch,
        tmp_path,
        element,
    )

    with pytest.raises(KeyboardInterrupt):
        launcher._try_semantic_pre_submit_repair(9432, 0, job, manifest, audit)
    page = playwright.chromium.connect_over_cdp("ignored").contexts[0].pages[0]
    page.url = "https://example.invalid/changed-after-upload"

    result = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert result["status"] == "parked_side_effect_unknown"
    assert result["legacy_fallback_safe"] is False
    assert element.set_calls == 1
    parked = journal.list_attempt_operations(connection, "attempt-1")[0]
    assert parked.state == "parked_side_effect_unknown"
    assert parked.reason_code == "provider_changed_after_dispatch"


def test_container_identity_change_after_dispatch_parks_without_rewrite(
    monkeypatch,
    tmp_path: Path,
) -> None:
    element = InterruptAfterUploadInput()
    job, manifest, _broker, connection, _playwright, audit, _resume = _setup(
        monkeypatch,
        tmp_path,
        element,
    )

    with pytest.raises(KeyboardInterrupt):
        launcher._try_semantic_pre_submit_repair(9432, 0, job, manifest, audit)
    element.input_id = "resume-rerendered"

    result = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert result["status"] == "parked_side_effect_unknown"
    assert element.set_calls == 1
    parked = journal.list_attempt_operations(connection, "attempt-1")[0]
    assert parked.state == "parked_side_effect_unknown"
    assert parked.reason_code == "page_binding_changed_after_dispatch"


def test_provider_change_to_supported_adapter_after_dispatch_is_zero_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workday_input = InterruptAfterUploadInput()
    job, manifest, _broker, connection, playwright, audit, _resume = _setup(
        monkeypatch,
        tmp_path,
        workday_input,
    )

    with pytest.raises(KeyboardInterrupt):
        launcher._try_semantic_pre_submit_repair(9432, 0, job, manifest, audit)
    browser = playwright.chromium.connect_over_cdp("ignored")
    page = browser.contexts[0].pages[0]
    smartrecruiters_input = FakeInput(input_id="smartrecruiters-resume")
    page.surface.inputs = [smartrecruiters_input]
    page.url = "https://jobs.smartrecruiters.com/Example/REQ-1/apply"
    audit["page_url"] = page.url

    result = launcher._try_semantic_pre_submit_repair(
        9432,
        0,
        job,
        manifest,
        audit,
    )

    assert result["status"] == "parked_side_effect_unknown"
    assert smartrecruiters_input.set_calls == 0
    records = journal.list_attempt_operations(connection, "attempt-1")
    assert len(records) == 1
    assert records[0].provider == "workday"
    assert records[0].state == "parked_side_effect_unknown"
    assert records[0].reason_code == "attempt_scope_changed_after_dispatch"


def test_no_prior_dispatch_explicitly_attests_safe_legacy_fallback(
    monkeypatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(launcher, "get_connection", lambda: connection)

    result = launcher._semantic_legacy_fallback_decision(
        {"_attempt_id": "attempt-without-semantic-dispatch"},
        reason_code="feature_disabled_after_dispatch",
    )

    assert result == {
        "status": "not_applicable",
        "legacy_fallback_safe": True,
        "reason": "feature_disabled_after_dispatch",
    }
