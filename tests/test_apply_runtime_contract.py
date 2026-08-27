"""No-network contracts for the final application submission state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from applypilot import config
from applypilot.apply import launcher
from applypilot.database import init_db


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("RESULT:READY_TO_SUBMIT", ("READY_TO_SUBMIT", None)),
        ("RESULT:FAILED:manual_review_required", ("FAILED", "manual_review_required")),
        ("I would output RESULT:APPLIED now", None),
        ("RESULT:APPLIED\nRESULT:APPLIED", None),
        ("RESULT:APPLIED\nRESULT:BOGUS", None),
        ("`RESULT:APPLIED`", None),
    ],
)
def test_result_marker_is_one_standalone_line(output: str, expected: object) -> None:
    assert launcher._parse_result_line(output) == expected


@pytest.mark.parametrize(
    ("phase", "output", "expected"),
    [
        ("prepare", "RESULT:READY_TO_SUBMIT", "ready_to_submit"),
        ("prepare", "RESULT:COVER_NOT_REQUIRED", "cover_not_required"),
        ("prepare", "RESULT:COVER_LETTER_REQUIRED", "cover_letter_required"),
        ("prepare", "RESULT:FAILED:manual_review_required", "failed:manual_review_required"),
        ("prepare", "RESULT:APPLIED", "submission_uncertain"),
        ("submit", "RESULT:READY_TO_SUBMIT", "submission_uncertain"),
        ("submit", "RESULT:CAPTCHA", "submission_uncertain"),
        ("submit", "RESULT:APPLIED\nRESULT:APPLIED", "submission_uncertain"),
        ("submit", "no marker", "submission_uncertain"),
    ],
)
def test_result_interpretation_is_phase_strict(
    phase: str, output: str, expected: str
) -> None:
    status, _ = launcher._interpret_agent_output(
        output,
        dry_run=False,
        submission_phase=phase,
    )
    assert status == expected


def test_pre_submit_snapshot_hard_pauses_new_uncertain_states() -> None:
    snapshot = {
        "url": "https://jobs.example.test/role/apply",
        "required_unfilled": ["I accept the required declaration"],
        "sensitive_required_unknown": ["Will you require visa sponsorship?"],
        "resume_field_present": True,
        "resume_uploaded": False,
        "full_name_values": [],
        "current_location_values": [],
        "select_fields": [],
        "radio_questions": [],
        "submit_control_count": 1,
        "assessment_visible": True,
        "captcha_visible": True,
        "captcha_token_present": False,
    }
    issues = launcher._validate_pre_submit_snapshot(
        snapshot,
        {"personal": {}, "screening": {}},
        {"url": "https://jobs.example.test/role"},
    )

    assert "visible_captcha" in issues
    assert "assessment_present" in issues
    assert "resume_not_uploaded" in issues
    assert any(item.startswith("required_field_empty:") for item in issues)
    assert any(item.startswith("sensitive_required_unknown:") for item in issues)


def test_visible_captcha_remains_a_hard_pause_even_if_a_token_exists() -> None:
    issues = launcher._validate_pre_submit_snapshot(
        {
            "url": "https://jobs.example.test/role/apply",
            "required_unfilled": [],
            "sensitive_required_unknown": [],
            "resume_field_present": True,
            "resume_uploaded": True,
            "full_name_values": [],
            "current_location_values": [],
            "select_fields": [],
            "radio_questions": [],
            "submit_control_count": 1,
            "assessment_visible": False,
            "captcha_visible": True,
            "captcha_token_present": True,
        },
        {"personal": {}, "screening": {}},
        {"application_url": "https://jobs.example.test/role/apply"},
    )

    assert "visible_captcha" in issues


def test_hidden_captcha_iframe_is_not_a_visible_verification_gate() -> None:
    class Iframe:
        def __init__(self, visible: bool) -> None:
            self.visible = visible

        def get_attribute(self, name: str) -> str:
            return {
                "title": "reCAPTCHA",
                "src": "https://www.recaptcha.net/recaptcha/api2/anchor?size=invisible",
            }.get(name, "")

        def is_visible(self) -> bool:
            return self.visible

        def bounding_box(self) -> dict[str, int]:
            return {"width": 320, "height": 180}

    class Locator:
        def __init__(self, iframe: Iframe) -> None:
            self.iframe = iframe

        def all(self) -> list[Iframe]:
            return [self.iframe]

    class Page:
        def __init__(self, visible: bool) -> None:
            self.iframe = Iframe(visible)

        def locator(self, selector: str) -> Locator:
            assert selector == "iframe"
            return Locator(self.iframe)

    assert launcher._visible_captcha_overlay(Page(False)) is False
    assert launcher._visible_captcha_overlay(Page(True)) is True


def test_model_and_observer_evidence_must_strongly_agree() -> None:
    model = {
        "receipt_visible": True,
        "applied_badge_visible": False,
        "confirmation_text": "Your application has been submitted",
        "confirmation_url": "https://jobs.example.test/confirmation",
    }
    observer = {
        "confirmed": True,
        "receipt_visible": True,
        "applied_badge_visible": False,
        "confirmation_text": "Your application has been submitted. Thank you.",
        "current_url": "https://jobs.example.test/confirmation",
    }

    assert launcher._submission_evidence_consistent(model, observer) is True
    assert launcher._submission_evidence_consistent(
        {**model, "confirmation_text": "Success — Your application has been submitted"},
        {**observer, "confirmation_text": "Success Your application has been submitted"},
    ) is True
    assert launcher._submission_evidence_consistent(
        model, {**observer, "current_url": "https://evil.example/confirmation"}
    ) is False
    assert launcher._submission_evidence_consistent(
        model, {**observer, "confirmation_text": "Application form"}
    ) is False


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        ({"confirmed": True}, "confirmed"),
        ({"verification_visible": True}, "verification_required"),
        ({"captcha_visible": True}, "verification_required"),
        (
            {
                "validation_error_count": 1,
                "repairable_validation_error_count": 1,
                "manual_validation_error_count": 0,
            },
            "validation_blocked_repairable",
        ),
        (
            {
                "validation_error_count": 1,
                "repairable_validation_error_count": 0,
                "manual_validation_error_count": 1,
            },
            "validation_blocked_manual",
        ),
        ({"form_visible": True, "submit_control_count": 1}, "uncertain"),
    ],
)
def test_post_submit_observation_separates_receipts_gates_and_rejections(
    observation: dict, expected: str
) -> None:
    assert launcher._classify_post_submit_observation(observation) == expected


def test_single_url_acquisition_never_uses_substring_matching(
    tmp_path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    stored_url = "https://jobs.example.test/role?tracking=campaign"
    conn.execute(
        "INSERT INTO jobs (url, application_url, title, company_name, "
        "tailored_resume_path, tailor_status, eligibility_status) "
        "VALUES (?, ?, 'Analyst', 'Example', 'resume.pdf', "
        "'machine_validated', 'eligible')",
        (stored_url, stored_url),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher.acquire_job(
        target_url="https://jobs.example.test/role",
        preview_only=True,
    ) is None


def test_manifest_is_rechecked_before_atomic_reservation(monkeypatch) -> None:
    calls: list[tuple[str, str, int]] = []
    job = {"url": "https://jobs.example.test/role"}
    manifest = {
        "batch_id": "batch-1",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "max_submissions": 2,
    }
    monkeypatch.setattr(
        "applypilot.apply.authorization.authorize_job",
        lambda supplied_manifest, supplied_job: {"url": supplied_job["url"]},
    )
    monkeypatch.setattr(
        "applypilot.database.reserve_batch_submission",
        lambda batch, url, cap: calls.append((batch, url, cap)) or True,
    )

    assert launcher._reserve_manifest_submission(manifest, job) == (True, "reserved")
    assert calls == [("batch-1", job["url"], 2)]


def test_missing_manifest_can_never_reserve_a_real_submission() -> None:
    assert launcher._reserve_manifest_submission(
        None, {"url": "https://jobs.example.test/role"}
    ) == (False, "authorization_manifest_required")


def _run_worker_contract(
    monkeypatch,
    *,
    submit_raises: bool = False,
    submit_result: str = "applied",
    submit_results: list[str] | None = None,
    prepare_results: list[str] | None = None,
    observer_results: list[dict] | None = None,
    manual_captcha_relay: bool = False,
    ledger_update_succeeds: bool = True,
    launch_calls: list[tuple[tuple, dict]] | None = None,
    queued_jobs: list[dict] | None = None,
    use_target_url: bool = True,
    limit: int = 1,
):
    job = {
        "url": "https://jobs.example.test/role",
        "application_url": "https://jobs.example.test/role/apply",
        "title": "Data Analyst",
        "company_name": "Example",
    }
    run_phases: list[str] = []
    ledger: list[tuple[str, dict | None]] = []
    marked: list[tuple[tuple, dict]] = []
    submit_index = 0
    prepare_index = 0

    def fake_run(current_job, *args, **kwargs):
        nonlocal prepare_index, submit_index
        phase = kwargs["submission_phase"]
        run_phases.append(phase)
        if phase == "prepare":
            selected_prepare = (
                prepare_results[min(prepare_index, len(prepare_results) - 1)]
                if prepare_results
                else "ready_to_submit"
            )
            prepare_index += 1
            return selected_prepare, 10
        if submit_raises:
            raise RuntimeError("agent disconnected")
        current_job["_agent_submission_evidence"] = {
            "receipt_visible": True,
            "applied_badge_visible": False,
            "confirmation_text": "Your application has been submitted",
            "confirmation_url": "https://jobs.example.test/confirmation",
        }
        selected_result = (
            submit_results[min(submit_index, len(submit_results) - 1)]
            if submit_results
            else submit_result
        )
        submit_index += 1
        return selected_result, 10

    observed = list(observer_results or [])

    def fake_observer(*args, **kwargs):
        if observed:
            return observed.pop(0)
        return {
            "confirmed": True,
            "receipt_visible": True,
            "applied_badge_visible": False,
            "confirmation_text": "Your application has been submitted",
            "current_url": "https://jobs.example.test/confirmation",
        }

    launcher._stop_event.clear()
    monkeypatch.setattr(config, "load_profile", dict)
    monkeypatch.setattr(launcher, "_submission_rate_status", lambda *args: (True, 0, "ready"))
    monkeypatch.setattr(launcher, "get_connection", lambda: object())
    queued = iter(queued_jobs or [job])
    monkeypatch.setattr(launcher, "acquire_job", lambda **kwargs: next(queued, None))
    def fake_launch(*args, **kwargs):
        if launch_calls is not None:
            launch_calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(launcher, "launch_chrome", fake_launch)
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "run_job", fake_run)
    monkeypatch.setattr(launcher, "_audit_live_pre_submit_page", lambda *args: (None, {"status": "clear"}))
    monkeypatch.setattr(launcher, "_reserve_manifest_submission", lambda *args: (True, "reserved"))
    monkeypatch.setattr(
        launcher,
        "_observe_post_submit_page",
        fake_observer,
    )
    monkeypatch.setattr(launcher, "_archive_worker_evidence", lambda *args: [])
    monkeypatch.setattr(launcher, "_wait_for_manual_captcha", lambda *args: True)
    monkeypatch.setattr(
        launcher,
        "_mark_runtime_cover_not_required",
        lambda current_job: {**current_job, "cover_letter_status": "not_required"},
    )
    monkeypatch.setattr(
        launcher,
        "_prepare_runtime_cover_letter",
        lambda current_job: {
            **current_job,
            "cover_letter_status": "agent_validated",
            "cover_letter_path": "cover.txt",
        },
    )
    monkeypatch.setattr(
        launcher,
        "_update_submission_ledger",
        lambda manifest, supplied_job, status, evidence=None: ledger.append(
            (status, evidence)
        ) or ledger_update_succeeds,
    )
    monkeypatch.setattr(
        launcher,
        "mark_result",
        lambda *args, **kwargs: marked.append((args, kwargs)),
    )
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)

    result = launcher.worker_loop(
        worker_id=0,
        limit=limit,
        target_url=job["url"] if use_target_url else None,
        dry_run=False,
        manual_captcha_relay=manual_captcha_relay,
        authorization_manifest={"batch_id": "batch-1", "max_submissions": 1},
    )
    return result, run_phases, ledger, marked


def test_real_batch_replaces_pre_submit_failure_until_success_target(monkeypatch) -> None:
    first = {
        "url": "https://jobs.example.test/expired",
        "application_url": "https://jobs.example.test/expired/apply",
        "title": "Expired role",
        "company_name": "Example",
    }
    second = {
        "url": "https://jobs.example.test/replacement",
        "application_url": "https://jobs.example.test/replacement/apply",
        "title": "Replacement role",
        "company_name": "Example",
    }

    result, phases, _ledger, marked = _run_worker_contract(
        monkeypatch,
        queued_jobs=[first, second],
        use_target_url=False,
        limit=1,
        prepare_results=["expired", "ready_to_submit"],
    )

    assert result == (1, 1)
    assert phases == ["prepare", "prepare", "submit"]
    assert marked[0][0][:3] == (first["url"], "failed", "expired")
    assert marked[-1][0][:2] == (second["url"], "applied")


def test_worker_marks_applied_only_after_independent_observer(monkeypatch) -> None:
    result, phases, ledger, marked = _run_worker_contract(monkeypatch)

    assert result == (1, 0)
    assert phases == ["prepare", "submit"]
    assert ledger[0][0] == "applied"
    assert marked[0][0][:2] == ("https://jobs.example.test/role", "applied")
    assert marked[0][1]["evidence"]["observer"]["confirmed"] is True


def test_worker_bootstraps_browser_at_the_authorized_application_url(monkeypatch) -> None:
    launches: list[tuple[tuple, dict]] = []

    _run_worker_contract(monkeypatch, launch_calls=launches)

    assert launches[0][1]["start_url"] == "https://jobs.example.test/role/apply"


@pytest.mark.parametrize("discovery_result", ["cover_not_required", "cover_letter_required"])
def test_worker_resolves_cover_material_after_opening_ats(
    monkeypatch, discovery_result: str
) -> None:
    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch,
        prepare_results=[discovery_result, "ready_to_submit"],
    )

    assert result == (1, 0)
    assert phases == ["prepare", "prepare", "submit"]
    assert ledger[0][0] == "applied"
    assert marked[0][0][1] == "applied"


def test_worker_never_marks_applied_when_ledger_update_fails(monkeypatch) -> None:
    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch, ledger_update_succeeds=False
    )

    assert result == (0, 0)
    assert phases == ["prepare", "submit"]
    assert ledger[0][0] == "applied"
    assert marked[0][0][1] == "submission_uncertain"
    assert marked[0][1]["evidence"]["reason"] == "submission_ledger_update_failed"


def test_worker_exception_after_submit_start_is_never_retryable_failed(monkeypatch) -> None:
    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch, submit_raises=True
    )

    assert result == (0, 0)
    assert phases == ["prepare", "submit"]
    assert ledger[0][0] == "submission_uncertain"
    assert marked[0][0][1] == "submission_uncertain"
    assert marked[0][1]["evidence"]["submit_started"] is True


def test_submit_phase_ready_marker_never_starts_a_second_submit_turn(monkeypatch) -> None:
    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch, submit_result="ready_to_submit"
    )

    assert result == (0, 0)
    assert phases == ["prepare", "submit"]
    assert ledger[0][0] == "submission_uncertain"
    assert marked[0][0][1] == "submission_uncertain"


def test_worker_repairs_a_deterministic_validation_rejection_only_once(monkeypatch) -> None:
    validation_rejection = {
        "confirmed": False,
        "receipt_visible": False,
        "applied_badge_visible": False,
        "validation_error_count": 1,
        "repairable_validation_error_count": 1,
        "manual_validation_error_count": 0,
        "validation_errors": [{
            "label": "Portfolio URL (optional)",
            "message": "Please provide a valid URL",
            "field_type": "url",
            "optional_claimed": True,
            "repairable": True,
        }],
        "current_url": "https://jobs.example.test/role/apply",
    }
    confirmed = {
        "confirmed": True,
        "receipt_visible": True,
        "applied_badge_visible": False,
        "confirmation_text": "Your application has been submitted",
        "current_url": "https://jobs.example.test/confirmation",
    }

    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch,
        submit_results=["submission_uncertain", "applied"],
        observer_results=[validation_rejection, confirmed],
    )

    assert result == (1, 0)
    assert phases == ["prepare", "submit", "submit"]
    assert ledger[0][0] == "applied"
    evidence = marked[0][1]["evidence"]
    assert len(evidence["attempts"]) == 2
    assert evidence["attempts"][0]["disposition"] == "validation_blocked_repairable"


def test_worker_never_repairs_media_or_identity_validation(monkeypatch) -> None:
    manual_rejection = {
        "confirmed": False,
        "validation_error_count": 1,
        "repairable_validation_error_count": 0,
        "manual_validation_error_count": 1,
        "validation_errors": [{
            "label": "Optional video introduction",
            "message": "Please upload a recording",
            "field_type": "file",
            "optional_claimed": True,
            "repairable": False,
        }],
    }

    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch,
        submit_result="submission_uncertain",
        observer_results=[manual_rejection],
    )

    assert result == (0, 1)
    assert phases == ["prepare", "submit"]
    assert ledger[0][0] == "failed"
    assert marked[0][0][1] == "failed"
    assert marked[0][1]["permanent"] is True


def test_worker_resumes_once_after_manual_verification_clears(monkeypatch) -> None:
    verification_gate = {
        "confirmed": False,
        "verification_visible": True,
        "validation_error_count": 0,
        "current_url": "https://jobs.example.test/role/apply",
    }
    confirmed = {
        "confirmed": True,
        "receipt_visible": True,
        "applied_badge_visible": False,
        "confirmation_text": "Your application has been submitted",
        "current_url": "https://jobs.example.test/confirmation",
    }

    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch,
        submit_results=["submission_uncertain", "applied"],
        observer_results=[verification_gate, confirmed],
        manual_captcha_relay=True,
    )

    assert result == (1, 0)
    assert phases == ["prepare", "submit", "submit"]
    assert ledger[0][0] == "applied"
    assert marked[0][1]["evidence"]["attempts"][0]["disposition"] == (
        "verification_required"
    )
