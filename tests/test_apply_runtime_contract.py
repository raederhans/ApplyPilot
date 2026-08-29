"""No-network contracts for the final application submission state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from applypilot import config
from applypilot.apply import agent_runtime, launcher, prompt, router
from applypilot.apply.email_routing import MailboxMcpSpec, mailbox_prepare_duplicate_receipt
from applypilot.database import init_db


def test_prepare_guidance_fragments_are_selected_from_job_context() -> None:
    fragments = prompt._select_prompt_fragments(
        {
            "title": "Data Intern",
            "full_description": "Expected compensation and background check details.",
            "source_site": "generic",
        },
        dry_run=False,
    )

    assert "core" in fragments
    assert "compensation" in fragments
    assert "screening" in fragments
    assert "linkedin" not in fragments
    assert "direct_email" not in fragments
    assert "agent_orchestration" not in fragments
    assert "agent_orchestration" in prompt._select_prompt_fragments(
        {
            "title": "Data Intern",
            "source_site": "generic",
            "_agent_orchestration_available": True,
        },
        dry_run=False,
    )


def test_reduced_specialist_results_are_bounded_context_for_the_next_turn() -> None:
    section = prompt._build_specialist_context_section(
        {
            "_agent_specialist_context": [
                {
                    "proposal_id": "duplicate-check",
                    "kind": "read-only-check",
                    "status": "completed",
                    "summary": "No exact duplicate receipt was found.",
                }
            ]
        }
    )

    assert "CONSUMED SPECIALIST CONTEXT" in section
    assert "No exact duplicate receipt was found" in section
    assert "never treat them as submission authority" in section


def test_resume_upload_tool_state_requires_visible_repairable_resume_error() -> None:
    assert launcher._repair_requires_resume_upload(
        {
            "repair_mode": True,
            "validation_errors": [
                {
                    "label": "Resume / CV",
                    "message": "Please upload a file",
                    "field_type": "file",
                    "repairable": True,
                }
            ],
        }
    ) is True
    assert launcher._repair_requires_resume_upload(
        {
            "repair_mode": True,
            "validation_errors": [
                {
                    "label": "Optional video introduction",
                    "message": "Please upload a recording",
                    "field_type": "file",
                    "repairable": False,
                }
            ],
        }
    ) is False
    assert launcher._repair_requires_resume_upload(
        {
            "repair_mode": True,
            "validation_errors": [
                {
                    "label": "Portfolio URL",
                    "message": "This field is required",
                    "field_type": "url",
                    "repairable": True,
                }
            ],
        }
    ) is False


@pytest.mark.parametrize(
    ("job", "expected_fragment"),
    [
        ({"url": "https://tenant.myworkdayjobs.com/job/1"}, "ats_workday"),
        ({"url": "https://jobs.smartrecruiters.com/Example/1-role"}, "ats_smartrecruiters"),
        ({"source_site": "greenhouse"}, "ats_greenhouse"),
        ({"source_site": "lever"}, "ats_lever"),
    ],
)
def test_provider_guidance_is_exposed_only_for_the_matching_route(
    job: dict, expected_fragment: str
) -> None:
    fragments = prompt._select_prompt_fragments(job, dry_run=False)

    assert expected_fragment in fragments
    assert len(
        {
            "ats_workday",
            "ats_smartrecruiters",
            "ats_greenhouse",
            "ats_lever",
        }
        & set(fragments)
    ) == 1


def test_submit_prompt_is_a_compact_phase_delta_without_resume_body() -> None:
    built = prompt._build_compact_submit_prompt(
        job={"url": "https://jobs.example/1", "title": "Intern", "company_name": "Example"},
        control_contract_json='{"single_writer": true, "submit_owner": "playwright"}',
        profile_summary="Legal Name: Taylor Chen\nWork Auth: confirmed",
        hard_rules="HARD RULES",
        browser_observation_section="FROZEN AUDIT: ready",
        specialist_context_section="",
        email_route_section="",
        ats_adapter_section="",
        pdf_path="C:/worker/Taylor_Resume.pdf",
        cl_upload_path="",
        opening_steps="Do not navigate or reload.",
        mission_instruction="Review and submit exactly once.",
        mission_body="The form is already populated.",
        field_review_steps="Review required fields.",
        final_steps="Click once, then observe.",
        result_codes="RESULT:APPLIED\nRESULT:SUBMISSION_UNCERTAIN",
        structured_reporting_section="Report one compact turn.",
        captcha_section="Visible CAPTCHA is a hard pause.",
        phone_digits="90000000",
    )

    assert len(built) < 7000
    assert "FROZEN AUDIT: ready" in built
    assert "single_writer" in built
    assert "Click the authorized final control exactly once" in built
    assert "receipt never authorizes a second click" in built
    assert "IDENTITY AND ELIGIBILITY MATERIALS" in built
    assert "RESUME TEXT" not in built
    assert "COVER LETTER TEXT" not in built


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


def test_agent_log_text_is_always_redacted() -> None:
    secret = "OTP 123456 and full email body"

    rendered = launcher._redacted_agent_log_line(secret)

    assert rendered == "  >> agent_text_redacted\n"
    assert secret not in rendered


def test_control_router_separates_driver_runtime_and_handoff_policy() -> None:
    primary = router.initial_route("auto")
    contract = router.prompt_control_contract(
        primary,
        interaction_mode="auto",
        resume_existing_page=False,
    )

    assert primary.interaction_driver == "playwright"
    assert primary.browser_runtime == "edge"
    assert contract["requestable_handoffs"] == ["computer_use"]
    assert contract["single_writer"] is True
    assert contract["runtime_switch_after_submit_forbidden"] is True


def test_control_router_only_escalates_explicit_pre_submit_browser_blocks() -> None:
    explicit = router.cloak_fallback_route(
        "failed:cloudflare_challenge:turnstile",
        requested_browser_backend="auto",
        phase="prepare",
        current_runtime="edge",
        fallback_already_used=False,
    )
    generic_stall = router.cloak_fallback_route(
        "failed:stuck",
        requested_browser_backend="auto",
        phase="prepare",
        current_runtime="edge",
        fallback_already_used=False,
    )
    after_submit = router.cloak_fallback_route(
        "failed:cloudflare_blocked",
        requested_browser_backend="auto",
        phase="submit",
        current_runtime="edge",
        fallback_already_used=False,
    )

    assert explicit is not None and explicit.browser_runtime == "cloak"
    assert generic_stall is None
    assert after_submit is None


def test_computer_use_is_a_prepare_only_external_handoff() -> None:
    assert router.computer_use_handoff_allowed(
        "failed:visual_only_control",
        interaction_mode="auto",
        phase="prepare",
        submit_started=False,
    )
    assert not router.computer_use_handoff_allowed(
        "failed:visual_only_control",
        interaction_mode="auto",
        phase="submit",
        submit_started=True,
    )
    assert not router.computer_use_handoff_allowed(
        "captcha",
        interaction_mode="auto",
        phase="prepare",
        submit_started=False,
    )


def test_auto_keeps_edge_workers_parallel_while_explicit_cloak_is_bounded() -> None:
    auto_workers, auto_reduced = launcher._resolve_worker_count(
        3,
        3,
        "auto",
        cloak_concurrency_allowed=False,
    )
    cloak_workers, cloak_reduced = launcher._resolve_worker_count(
        3,
        3,
        "cloak",
        cloak_concurrency_allowed=False,
    )

    assert (auto_workers, auto_reduced) == (3, False)
    assert (cloak_workers, cloak_reduced) == (1, True)


def test_worker_count_uses_configured_profile_cap_without_hidden_global_limit() -> None:
    workers, reduced = launcher._resolve_worker_count(
        8,
        6,
        "auto",
        cloak_concurrency_allowed=False,
    )

    assert workers == 6
    assert reduced is False


def test_finite_target_never_creates_zero_allocation_continuous_workers() -> None:
    assert launcher._workers_for_target(6, 2) == 2
    assert launcher._workers_for_target(6, 0) == 6


def test_codex_receives_non_required_read_only_mailbox_mcp(tmp_path) -> None:
    mailbox = MailboxMcpSpec(
        package=None,
        command="portable-mail-mcp",
        search_tool="find_messages",
        read_tool="get_message",
        send_tool="send_message",
    )

    command, _ = agent_runtime.build_agent_command(
        "codex",
        "model",
        9432,
        tmp_path,
        tmp_path / "unused.json",
        resolve_codex=lambda: ["codex"],
        mailbox_mcp=mailbox,
    )
    rendered = " ".join(command)
    overrides = {
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c"
    }

    assert "mcp_servers.mailbox.required=false" in rendered
    assert 'mcp_servers.mailbox.enabled_tools=["find_messages", "get_message"]' in overrides
    assert not any(
        "mcp_servers.mailbox.enabled_tools=" in value and "send_message" in value
        for value in overrides
    )


def test_mailbox_send_tool_requires_explicit_standing_authorization(tmp_path) -> None:
    mailbox = MailboxMcpSpec(package=None, command="portable-mail-mcp")
    config = agent_runtime.make_mcp_config(
        9432,
        mailbox_mcp=mailbox,
        direct_email_send_authorized=True,
    )
    command, _ = agent_runtime.build_agent_command(
        "codex",
        "model",
        9432,
        tmp_path,
        tmp_path / "unused.json",
        resolve_codex=lambda: ["codex"],
        mailbox_mcp=mailbox,
        direct_email_send_authorized=True,
    )
    overrides = {
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c"
    }

    assert "mailbox" in config["mcpServers"]
    assert (
        'mcp_servers.mailbox.enabled_tools=["search_emails", "read_email", "send_email"]'
        in overrides
    )


def test_prepare_phase_never_exposes_mailbox_send_even_when_profile_allows_it(
    monkeypatch, tmp_path
) -> None:
    profile = {
        "authentication": {
            "mailbox_read_authorized": True,
            "mailbox": "candidate@example.test",
        },
        "submission_policy": {"direct_email_application_authorized": True},
    }
    monkeypatch.setattr(config, "load_profile", lambda: profile)

    command, _ = launcher.run_job.__globals__["_build_agent_command"](
        "codex",
        "model",
        9432,
        tmp_path,
        tmp_path / "unused.json",
        mailbox_mcp=MailboxMcpSpec(package=None, command="portable-mail-mcp"),
        direct_email_send_authorized=False,
    )
    enabled = next(
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c" and command[index + 1].startswith("mcp_servers.mailbox.enabled_tools=")
    )

    assert "send_email" not in enabled


def test_claude_mailbox_surface_is_read_only_without_send_authorization(tmp_path) -> None:
    mailbox = MailboxMcpSpec(package=None, command="portable-mail-mcp")
    config = agent_runtime.make_mcp_config(9432, mailbox_mcp=mailbox)
    command, _ = agent_runtime.build_agent_command(
        "claude",
        "model",
        9432,
        tmp_path,
        tmp_path / "mcp.json",
        resolve_claude=lambda: ["claude"],
        mailbox_mcp=mailbox,
    )
    allowed = command[command.index("--allowedTools") + 1]
    disallowed = command[command.index("--disallowedTools") + 1]

    assert config["mcpServers"]["mailbox"]["command"] == "portable-mail-mcp"
    assert "mcp__mailbox__search_emails" in allowed
    assert "mcp__mailbox__read_email" in allowed
    assert "mcp__mailbox__send_email" not in allowed
    assert "mcp__mailbox__send_email" in disallowed
    assert "mcp__mailbox__delete_email" in disallowed


def test_claude_mailbox_send_is_exposed_only_when_authorized(tmp_path) -> None:
    mailbox = MailboxMcpSpec(package=None, command="portable-mail-mcp")
    command, _ = agent_runtime.build_agent_command(
        "claude",
        "model",
        9432,
        tmp_path,
        tmp_path / "mcp.json",
        resolve_claude=lambda: ["claude"],
        mailbox_mcp=mailbox,
        direct_email_send_authorized=True,
    )
    allowed = command[command.index("--allowedTools") + 1]
    disallowed = command[command.index("--disallowedTools") + 1]

    assert "mcp__mailbox__send_email" in allowed
    assert "mcp__mailbox__send_email" not in disallowed


def test_mailbox_environment_values_stay_out_of_codex_command(tmp_path) -> None:
    mailbox = MailboxMcpSpec(
        package=None,
        command="portable-mail-mcp",
        env={"MAILBOX_PRIVATE_TOKEN": "secret-value"},
    )
    command, _ = agent_runtime.build_agent_command(
        "codex",
        "model",
        9432,
        tmp_path,
        tmp_path / "unused.json",
        resolve_codex=lambda: ["codex"],
        mailbox_mcp=mailbox,
    )
    rendered = " ".join(command)

    assert "MAILBOX_PRIVATE_TOKEN" in rendered
    assert "secret-value" not in rendered


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


def test_upload_failure_context_becomes_actionable_without_file_paths() -> None:
    output = (
        "RESULT:FAILED:resume_upload\n"
        'FAILURE_CONTEXT: {"category":"resume_upload","field_label":"Resume/CV",'
        '"visible_state":"optional attachment accepted but required resume stayed empty",'
        '"attempts":2,"path":"C:/private/resume.pdf"}'
    )

    context = launcher._parse_failure_context(output)
    error = launcher._format_failure_error("resume_upload", context)

    assert context == {
        "category": "resume_upload",
        "field_label": "Resume/CV",
        "visible_state": "optional attachment accepted but required resume stayed empty",
        "attempts": 2,
    }
    assert error.startswith(
        "resume_upload; category=resume_upload; "
        "recoverability=retry_same_application; "
        "missing_capability=browser_file_upload_or_site_adapter; "
        "next_action=repair_bound_resume_upload_once; field=Resume/CV; "
    )
    assert error.endswith(
        "state=optional attachment accepted but required resume stayed empty; attempts=2"
    )
    assert "private" not in error


def test_required_document_context_names_the_actual_blocking_material() -> None:
    output = (
        "RESULT:FAILED:manual_review_required:required_document\n"
        'FAILURE_CONTEXT: {"category":"required_document","field_label":"Transcript",'
        '"blocking_material":"Academic transcript","visible_state":"required file not supplied",'
        '"attempts":0}'
    )

    error = launcher._format_failure_error(
        "manual_review_required:required_document",
        launcher._parse_failure_context(output),
    )

    assert "field=Transcript" in error
    assert "required_material=Academic transcript" in error
    assert "attempts=0" in error


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
    assert any(
        item.startswith("required_field_empty:confirmed_identity_fact:")
        for item in issues
    )


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


def test_pre_submit_accepts_a_configured_existing_docx_in_the_resume_card() -> None:
    issues = launcher._validate_pre_submit_snapshot(
        {
            "url": "https://www.linkedin.com/jobs/view/123/apply",
            "required_unfilled": [],
            "sensitive_required_unknown": [],
            "resume_field_present": True,
            "resume_uploaded": False,
            "resume_card_texts": ["Yu_Qiushi_AI_Automation_Projects.docx Last used today"],
            "full_name_values": [],
            "email_values": [],
            "current_location_values": [],
            "select_fields": [],
            "radio_questions": [],
            "submit_control_count": 1,
            "assessment_visible": False,
            "captcha_visible": False,
        },
        {
            "personal": {},
            "screening": {},
            "resume_library": {
                "linkedin_existing_resume_preferences": {
                    "ai_solutions": "AI_Automation_Projects"
                }
            },
        },
        {
            "url": "https://www.linkedin.com/jobs/view/123/",
            "site": "linkedin",
            "full_description": "AI agents and automation",
        },
    )

    assert "resume_not_uploaded" not in issues


def test_application_page_selection_prefers_review_tab_over_last_detail_tab() -> None:
    class Page:
        def __init__(self, signals: dict) -> None:
            self.signals = signals

        def evaluate(self, _script: str) -> dict:
            return self.signals

    review = Page(
        {"receipt": False, "final_submit": True, "review": True, "dialog": True}
    )
    detail = Page(
        {"receipt": False, "final_submit": False, "review": False, "dialog": False}
    )

    assert launcher._select_application_page([review, detail]) is review


def test_application_page_selection_prefers_populated_form_over_empty_shell() -> None:
    class Page:
        def __init__(self, signals: dict) -> None:
            self.signals = signals

        def evaluate(self, _script: str) -> dict:
            return self.signals

    empty_shell = Page(
        {
            "receipt": False,
            "final_submit": False,
            "review": False,
            "dialog": False,
            "form_controls": 0,
            "text_length": 20,
        }
    )
    populated_form = Page(
        {
            "receipt": False,
            "final_submit": False,
            "review": False,
            "dialog": False,
            "form_controls": 12,
            "text_length": 1200,
        }
    )

    assert launcher._select_application_page([empty_shell, populated_form]) is populated_form


def test_application_page_selection_scores_each_surface_once() -> None:
    class Page:
        def __init__(self, score: int) -> None:
            self.frames = [self]
            self.score = score
            self.evaluate_calls = 0

        def evaluate(self, _script: str) -> dict:
            self.evaluate_calls += 1
            return {
                "receipt": False,
                "final_submit": self.score > 0,
                "review": False,
                "dialog": False,
                "form_controls": self.score,
                "text_length": 100,
            }

    first = Page(0)
    second = Page(4)

    assert launcher._select_application_page([first, second]) is second
    assert [first.evaluate_calls, second.evaluate_calls] == [1, 1]


def test_application_frame_selection_prefers_populated_child_frame() -> None:
    class Surface:
        def __init__(self, signals: dict) -> None:
            self.signals = signals

        def evaluate(self, _script: str) -> dict:
            return self.signals

    outer = Surface(
        {"receipt": False, "final_submit": False, "form_controls": 0, "text_length": 10}
    )
    application_frame = Surface(
        {"receipt": False, "final_submit": True, "form_controls": 9, "text_length": 900}
    )

    class Page:
        def __init__(self) -> None:
            self.frames = [outer, application_frame]

    assert launcher._select_application_frame(Page()) is application_frame


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
        {
            **model,
            "confirmation_text": (
                "Congratulations! Your application has been submitted successfully."
            ),
        },
        {**observer, "confirmation_text": "Application Submitted"},
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
                "provider_submission_error_visible": True,
                "provider_submission_error_text": (
                    "There was an error verifying your application. Please try again."
                ),
            },
            "provider_submission_error",
        ),
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
    monkeypatch.setattr(
        "applypilot.apply.authorization.freeze_submission_materials",
        lambda supplied_job, profile: {"version": 1, "materials": []},
    )
    monkeypatch.setattr(config, "load_profile", dict)

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
    browser_backend: str = "edge",
    ledger_update_succeeds: bool = True,
    launch_calls: list[tuple[tuple, dict]] | None = None,
    queued_jobs: list[dict] | None = None,
    acquire_calls: list[dict] | None = None,
    verification_calls: list[dict] | None = None,
    use_target_url: bool = True,
    limit: int = 1,
    audit_results: list[tuple[str | None, dict]] | None = None,
    email_application: dict | None = None,
    staged_attachment: str | None = None,
):
    job = {
        "url": "https://jobs.example.test/role",
        "application_url": "https://jobs.example.test/role/apply",
        "title": "Data Analyst",
        "company_name": "Example",
        "description": "Apply by email to jobs@example.test for this role.",
    }
    if staged_attachment:
        job["_staged_resume_path"] = staged_attachment
    run_phases: list[str] = []
    ledger: list[tuple[str, dict | None]] = []
    marked: list[tuple[tuple, dict]] = []
    submit_index = 0
    prepare_index = 0

    def fake_run(current_job, *args, **kwargs):
        nonlocal prepare_index, submit_index
        assert current_job.get("_browser_backend") in {"edge", "cloak"}
        phase = kwargs["submission_phase"]
        expected_driver = (
            "mailbox"
            if phase == "submit" and email_application is not None
            else "playwright"
        )
        assert current_job["_control_contract"]["interaction_driver"] == expected_driver
        assert current_job["_control_contract"]["browser_runtime"] == current_job["_browser_backend"]
        run_phases.append(phase)
        if phase == "prepare":
            if email_application is not None:
                current_job["_agent_observations"] = {
                    "email_application": dict(email_application)
                }
                current_job["_mailbox_prepare_duplicate_receipt"] = (
                    mailbox_prepare_duplicate_receipt(
                        {
                            "query": (
                                f'in:sent to:{email_application["recipient"]} '
                                f'subject:"{email_application["subject"]}"'
                            )
                        },
                        {"messages": []},
                        email_application,
                    )
                )
            selected_prepare = (
                prepare_results[min(prepare_index, len(prepare_results) - 1)]
                if prepare_results
                else "ready_to_submit"
            )
            prepare_index += 1
            return selected_prepare, 10
        if submit_raises:
            raise RuntimeError("agent disconnected")
        if email_application is not None:
            current_job["_agent_submission_evidence"] = {
                "channel": "direct_email",
                "send_accepted": True,
                "sent_copy_verified": True,
                "recipient": email_application["recipient"],
                "subject": email_application["subject"],
                "attachment_names": email_application["attachment_names"],
                "confirmation_text": "Sent copy verified",
                "provider_message_id": "message-1",
            }
            current_job["_mailbox_runtime_evidence"] = {
                "send_call_completed": True,
                "send_request_bound": True,
                "post_send_search_completed": True,
                "post_send_read_completed": True,
                "sent_receipt": {
                    "folder": "sent",
                    "recipient": email_application["recipient"],
                    "subject": email_application["subject"],
                    "attachment_names": email_application["attachment_names"],
                    "body_sha256": email_application["body_sha256"],
                    "provider_message_id": "message-1",
                },
            }
        else:
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
    def fake_acquire(**kwargs):
        if acquire_calls is not None:
            acquire_calls.append(dict(kwargs))
        return next(queued, None)

    monkeypatch.setattr(launcher, "acquire_job", fake_acquire)
    def fake_launch(*args, **kwargs):
        if launch_calls is not None:
            launch_calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(launcher, "launch_chrome", fake_launch)
    monkeypatch.setattr(
        launcher,
        "_open_bound_application_target",
        lambda _port, _url: {"application-root"},
    )
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "allocate_cdp_port", lambda _worker_id: 9432)
    monkeypatch.setattr(launcher, "release_cdp_port", lambda _worker_id: None)
    monkeypatch.setattr(
        launcher,
        "capture_browser_session",
        lambda *args, **kwargs: {"cookies": [{"name": "session"}]},
    )
    monkeypatch.setattr(
        launcher,
        "restore_browser_session",
        lambda *args, **kwargs: 1,
    )
    monkeypatch.setattr(launcher, "run_job", fake_run)
    audits = list(audit_results or [])

    def fake_audit(*args):
        if audits:
            return audits.pop(0)
        return None, {"status": "clear", "disposition": "clear"}

    monkeypatch.setattr(launcher, "_audit_live_pre_submit_page", fake_audit)
    monkeypatch.setattr(launcher, "_reserve_manifest_submission", lambda *args: (True, "reserved"))
    monkeypatch.setattr(
        launcher,
        "_observe_post_submit_page",
        fake_observer,
    )
    monkeypatch.setattr(launcher, "_archive_worker_evidence", lambda *args: [])
    def fake_verification_wait(*args, **kwargs):
        if verification_calls is not None:
            verification_calls.append(dict(kwargs))
        return True

    monkeypatch.setattr(launcher, "_wait_for_manual_captcha", fake_verification_wait)
    monkeypatch.setattr(
        launcher,
        "_mark_runtime_cover_not_required",
        lambda current_job: {
            **{key: value for key, value in current_job.items() if key != "_browser_backend"},
            "cover_letter_status": "not_required",
        },
    )
    monkeypatch.setattr(
        launcher,
        "_prepare_runtime_cover_letter",
        lambda current_job: {
            **{key: value for key, value in current_job.items() if key != "_browser_backend"},
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
        "_admit_direct_email_receipt",
        lambda *args, **kwargs: {"status": "admitted"},
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
        browser_backend=browser_backend,
        authorization_manifest={"batch_id": "batch-1", "max_submissions": 1},
    )
    return result, run_phases, ledger, marked


def test_worker_reenters_prepare_once_for_repairable_audit_state(monkeypatch) -> None:
    repair_report = {
        "status": "attention",
        "disposition": "retry_prepare",
        "issues": ["required_field_empty:Application source"],
        "blocking_issues": [],
        "repairable_issues": ["required_field_empty:Application source"],
        "advisory_issues": [],
        "lossy_answer_mappings": [],
    }
    clear_report = {
        "status": "clear",
        "disposition": "clear",
        "issues": [],
        "blocking_issues": [],
        "repairable_issues": [],
        "advisory_issues": [],
        "lossy_answer_mappings": [],
    }

    result, phases, _ledger, _marked = _run_worker_contract(
        monkeypatch,
        prepare_results=["ready_to_submit", "ready_to_submit"],
        audit_results=[
            ("pre_submit_repair:required_field_empty:Application source", repair_report),
            (None, clear_report),
        ],
    )

    assert result == (1, 0)
    assert phases == ["prepare", "prepare", "submit"]


def test_worker_uses_reserved_mailbox_submit_route_for_email_application(
    monkeypatch,
    tmp_path,
) -> None:
    attachment = tmp_path / "Candidate_Resume.pdf"
    attachment.write_bytes(b"resume-pdf")
    email_plan = {
        "route": "direct_email",
        "recipient": "jobs@example.test",
        "recipient_domain": "example.test",
        "recipient_source": "official_listing",
        "subject": "Application - Data Analyst",
        "attachment_names": ["Candidate_Resume.pdf"],
        "attachments_verified": True,
        "duplicate_found": False,
        "body_sha256": "a" * 64,
        "duplicate_check": {
            "folder": "sent",
            "completed": True,
            "duplicate_found": False,
            "provider_query_id": "query-1",
        },
        "listing_evidence": "Official listing says apply to jobs@example.test",
    }

    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch,
        email_application=email_plan,
        staged_attachment=str(attachment),
    )

    assert result == (1, 0)
    assert phases == ["prepare", "submit"]
    assert ledger[0][0] == "applied"
    assert ledger[0][1]["agent"]["channel"] == "direct_email"
    assert ledger[0][1]["observer"]["confirmed"] is True
    assert marked[0][0][1] == "applied"


def test_direct_email_submission_evidence_requires_send_and_sent_copy() -> None:
    valid = (
        "RESULT:APPLIED\n"
        'SUBMISSION_EVIDENCE: {"channel":"direct_email","send_accepted":true,'
        '"sent_copy_verified":true,"recipient":"jobs@example.test",'
        '"subject":"Application - Analyst","attachment_names":["Resume.pdf"],'
        '"confirmation_text":"Sent copy verified","provider_message_id":"m-1"}'
    )
    incomplete = valid.replace('"sent_copy_verified":true', '"sent_copy_verified":false')

    evidence = launcher._validate_submission_evidence(valid)
    assert evidence is not None
    assert evidence["channel"] == "direct_email"
    assert launcher._validate_submission_evidence(incomplete) is None


def test_lossy_degree_and_work_status_mappings_are_audited_without_blocking() -> None:
    profile = {
        "education": [
            {
                "institution": "Example University",
                "degree": "Master of Computing in Applied AI",
            }
        ],
        "work_authorization": {
            "form_answer_policy": {
                "programme_credit_bearing_internship": {
                    "legally_authorized": True,
                    "requires_sponsorship": False,
                }
            }
        },
    }
    job = {
        "url": "https://jobs.example.test/intern",
        "title": "AI Intern",
        "full_description": "Credit-bearing internship",
    }
    snapshot = {
        "url": job["url"],
        "required_unfilled": [],
        "sensitive_required_unknown": [],
        "education_entries": [
            {
                "institution": "Example University",
                "degree": "Master of Science",
            }
        ],
        "select_fields": [
            {
                "text": "Citizenship / Visa Status",
                "selected": "Possess relevant work visa",
            }
        ],
        "radio_questions": [],
        "submit_control_count": 1,
        "assessment_visible": False,
        "captcha_visible": False,
    }

    assert launcher._validate_pre_submit_snapshot(snapshot, profile, job) == []
    mappings = launcher.page_observation_mod._collect_lossy_answer_mappings(
        snapshot, profile, job
    )

    assert {mapping["field_semantic"] for mapping in mappings} == {
        "education_degree",
        "citizenship_visa_status",
    }


def test_worker_uses_one_adaptive_lease_for_acquire_and_phase_renewals(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APPLYPILOT_AGENT_TIMEOUT_SECONDS", "3600")
    acquire_calls: list[dict] = []
    renewals: list[dict] = []
    job = {
        "url": "https://jobs.example.test/role",
        "application_url": "https://jobs.example.test/role/apply",
        "title": "Data Analyst",
        "company_name": "Example",
        "_attempt_id": "attempt-1",
    }
    monkeypatch.setattr(
        "applypilot.database.update_application_attempt",
        lambda _attempt_id, **kwargs: renewals.append(dict(kwargs)) or True,
    )

    result, _phases, _ledger, _marked = _run_worker_contract(
        monkeypatch,
        queued_jobs=[job],
        acquire_calls=acquire_calls,
    )

    assert result == (1, 0)
    assert acquire_calls[0]["application_lease_minutes"] == 62
    assert [item["lease_minutes"] for item in renewals] == [62, 62]


def test_manual_verification_receives_the_same_adaptive_lease(monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_AGENT_TIMEOUT_SECONDS", "3600")
    verification_calls: list[dict] = []

    _run_worker_contract(
        monkeypatch,
        prepare_results=["captcha", "failed:verification_not_cleared"],
        manual_captcha_relay=True,
        verification_calls=verification_calls,
    )

    assert verification_calls[0]["application_lease_minutes"] == 62


def test_auto_browser_backend_retries_one_explicit_bot_block_with_cloak(monkeypatch) -> None:
    launches: list[tuple[tuple, dict]] = []

    result, phases, _ledger, marked = _run_worker_contract(
        monkeypatch,
        browser_backend="auto",
        prepare_results=["failed:cloudflare_blocked", "ready_to_submit"],
        launch_calls=launches,
    )

    assert result == (1, 0)
    assert phases == ["prepare", "prepare", "submit"]
    assert [call[1]["browser_backend"] for call in launches] == ["edge", "cloak"]
    assert marked[0][1]["evidence"]["browser_backend"] == "cloak"
    assert marked[0][1]["evidence"]["fallback_from_edge"] is True
    assert marked[0][1]["evidence"]["interaction_driver"] == "playwright"
    assert marked[0][1]["evidence"]["browser_runtime"] == "cloak"
    assert marked[0][1]["evidence"]["submit_owner"] == "playwright"
    assert [event["event"] for event in marked[0][1]["evidence"]["route_history"]] == [
        "route_selected",
        "runtime_transition",
        "phase_transition",
    ]


def test_auto_browser_backend_does_not_retry_captcha(monkeypatch) -> None:
    launches: list[tuple[tuple, dict]] = []

    result, phases, _ledger, _marked = _run_worker_contract(
        monkeypatch,
        browser_backend="auto",
        prepare_results=["captcha"],
        launch_calls=launches,
    )

    assert result == (0, 1)
    assert phases == ["prepare"]
    assert [call[1]["browser_backend"] for call in launches] == ["edge"]


def test_auto_browser_backend_does_not_misclassify_generic_stall_as_bot_block(
    monkeypatch,
) -> None:
    launches: list[tuple[tuple, dict]] = []

    result, phases, _ledger, marked = _run_worker_contract(
        monkeypatch,
        browser_backend="auto",
        prepare_results=["failed:stuck", "ready_to_submit"],
        launch_calls=launches,
    )

    assert result == (0, 1)
    assert phases == ["prepare"]
    assert [call[1]["browser_backend"] for call in launches] == ["edge"]
    assert marked[0][0][:2] == (
        "https://jobs.example.test/role",
        "failed",
    )
    assert marked[0][0][2].startswith(
        "stuck; category=page_or_progress_failure; recoverability=retry_new_session"
    )


def test_dry_run_timeout_restores_pre_preview_state(monkeypatch) -> None:
    job = {
        "url": "https://example.test/preview",
        "title": "AI Intern",
        "company_name": "Example",
        "apply_status": None,
        "apply_error": None,
        "apply_attempts": 0,
        "last_attempted_at": None,
    }
    restored: list[dict] = []
    marked: list[tuple] = []

    launcher._stop_event.clear()
    monkeypatch.setattr(config, "load_profile", dict)
    monkeypatch.setattr(launcher, "acquire_job", lambda **kwargs: job)
    monkeypatch.setattr(launcher, "launch_chrome", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "allocate_cdp_port", lambda _worker_id: 9432)
    monkeypatch.setattr(launcher, "release_cdp_port", lambda _worker_id: None)
    monkeypatch.setattr(launcher, "run_job", lambda *args, **kwargs: ("timeout", 480000))
    monkeypatch.setattr(
        launcher, "restore_preview_state", lambda supplied: restored.append(dict(supplied))
    )
    monkeypatch.setattr(
        launcher, "mark_result", lambda *args, **kwargs: marked.append((args, kwargs))
    )
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)

    result = launcher.worker_loop(
        worker_id=0,
        limit=1,
        target_url=job["url"],
        dry_run=True,
        browser_backend="edge",
    )

    assert result == (0, 1)
    assert restored == [job]
    assert marked == []


def test_auto_browser_backend_accepts_a_detailed_cloudflare_reason(monkeypatch) -> None:
    launches: list[tuple[tuple, dict]] = []

    result, phases, _ledger, _marked = _run_worker_contract(
        monkeypatch,
        browser_backend="auto",
        prepare_results=["failed:cloudflare_challenge:turnstile", "ready_to_submit"],
        launch_calls=launches,
    )

    assert result == (1, 0)
    assert phases == ["prepare", "prepare", "submit"]
    assert [call[1]["browser_backend"] for call in launches] == ["edge", "cloak"]


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
    assert marked[0][0][:2] == (first["url"], "failed")
    assert marked[0][0][2].startswith(
        "expired; category=expired; recoverability=do_not_retry"
    )
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

    assert launches[0][1]["start_url"] is None


def test_exact_duplicate_preflight_skips_before_browser_launch(monkeypatch) -> None:
    launches: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        launcher,
        "_run_read_only_preflight",
        lambda _job: {
            "provider": "smartrecruiters",
            "duplicate": {
                "clear": False,
                "reason": "duplicate_submission_identity",
                "matched_job_url": "https://jobs.example.test/older",
            },
            "task_statuses": {},
        },
    )

    result, phases, _ledger, marked = _run_worker_contract(
        monkeypatch,
        launch_calls=launches,
    )

    assert result == (0, 0)
    assert phases == []
    assert launches == []
    assert marked[0][0][1] == "skipped"
    assert marked[0][1]["permanent"] is True


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


def test_worker_records_explicit_provider_submission_error_as_retry_blocked_uncertainty(
    monkeypatch,
) -> None:
    provider_rejection = {
        "confirmed": False,
        "provider_submission_error_visible": True,
        "provider_submission_error_text": (
            "There was an error verifying your application. Please try again."
        ),
        "validation_error_count": 0,
        "current_url": "https://jobs.example.test/role/apply",
    }

    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch,
        submit_result="submission_uncertain",
        observer_results=[provider_rejection],
    )

    assert result == (0, 0)
    assert phases == ["prepare", "submit"]
    assert ledger[0][0] == "submission_uncertain"
    assert marked[0][0][1] == "submission_uncertain"
    failure = marked[0][1]["evidence"]["technical_failure"]
    assert failure["category"] == "provider_submission_failure"
    assert failure["recoverability"] == "submission_uncertain"


def test_post_submit_observer_reconnects_read_only_without_repeating_submit(
    monkeypatch,
) -> None:
    initial_disconnect = {
        "confirmed": False,
        "reason": "post_submit_observer_error:ConnectionError",
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
        observer_results=[initial_disconnect, confirmed],
    )

    assert result == (1, 0)
    assert phases == ["prepare", "submit"]
    assert ledger[0][0] == "applied"
    assert marked[0][0][1] == "applied"
    observer = marked[0][1]["evidence"]["observer"]
    assert observer["observer_reconnect_attempts"] == 1
    assert observer["initial_observer_reason"].startswith(
        "post_submit_observer_error:"
    )


def test_smartrecruiters_binding_resolves_public_posting_to_publication_uuid() -> None:
    requested: list[str] = []

    def transport(url: str) -> dict:
        requested.append(url)
        return {
            "status_code": 200,
            "json": {
                "id": "744000145885499",
                "uuid": "4df5dd16-4fc7-48b4-a943-492fbc508b62",
                "company": {"identifier": "Grab"},
            },
        }

    binding = launcher._resolve_ats_application_binding(
        {
            "application_url": (
                "https://jobs.smartrecruiters.com/Grab/"
                "744000145885499-intern-strategy-insights?oga=true"
            )
        },
        transport=transport,
    )

    assert requested == [
        "https://api.smartrecruiters.com/v1/companies/Grab/postings/744000145885499"
    ]
    assert binding == {
        "provider": "smartrecruiters",
        "tenant": "Grab",
        "posting_id": "744000145885499",
        "publication_id": "4df5dd16-4fc7-48b4-a943-492fbc508b62",
        "resolved": True,
    }


def test_worker_resumes_once_after_manual_verification_clears(monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_AGENT_TIMEOUT_SECONDS", "3600")
    verification_calls: list[dict] = []
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
        verification_calls=verification_calls,
    )

    assert result == (1, 0)
    assert phases == ["prepare", "submit", "submit"]
    assert ledger[0][0] == "applied"
    assert marked[0][1]["evidence"]["attempts"][0]["disposition"] == (
        "verification_required"
    )
    assert verification_calls[0]["submit_started"] is True
    assert verification_calls[0]["application_lease_minutes"] == 62


def test_post_submit_verification_without_manual_relay_is_retry_blocked_uncertain(
    monkeypatch,
) -> None:
    verification_gate = {
        "confirmed": False,
        "verification_visible": True,
        "captcha_visible": True,
        "validation_error_count": 0,
        "current_url": "https://jobs.example.test/role/apply",
    }

    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch,
        submit_result="submission_uncertain",
        observer_results=[verification_gate],
        manual_captcha_relay=False,
    )

    assert result == (0, 0)
    assert phases == ["prepare", "submit"]
    assert ledger[0][0] == "submission_uncertain"
    assert marked[0][0][1] == "submission_uncertain"
