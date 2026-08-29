"""Contracts for provider-neutral application email routing."""

from __future__ import annotations

import hashlib
import json

import pytest

from applypilot.apply.email_routing import (
    EmailCapabilities,
    MailboxMcpSpec,
    direct_email_send_is_reserved,
    mailbox_prepare_duplicate_receipt,
    mailbox_read_input_matches_message,
    mailbox_search_message_id,
    mailbox_send_input_matches_plan,
    mailbox_sent_search_input_matches_plan,
    normalize_mailbox_read_receipt,
    normalize_prepared_email_application,
    normalize_sent_receipt,
    reserve_direct_email_send,
    resolve_email_route,
    resolve_mailbox_mcp_spec,
)


def test_verification_routes_to_authorized_mailbox_capabilities() -> None:
    decision = resolve_email_route(
        intent="verification",
        capabilities={"mailbox.search": True, "mailbox_read": True},
        mailbox_access_authorized=True,
        mailbox="Candidate@Example.com",
        employer=" Example  Labs ",
        ats_domain="jobs.example.test",
    )

    assert decision.route == "route_to_mailbox"
    assert decision.action == "search_and_use_current_verification"
    assert decision.search_scope == {
        "recipient": "candidate@example.com",
        "employer": "Example Labs",
        "ats_domain": "jobs.example.test",
        "max_age_minutes": 10,
        "exact_recipient": True,
        "current_application_only": True,
        "return_sensitive_value": False,
    }


def test_verification_without_mailbox_tools_returns_resumable_handoff() -> None:
    decision = resolve_email_route(
        intent="verification",
        capabilities=["mailbox_search"],
        mailbox_access_authorized=True,
        mailbox="candidate@example.com",
        employer="Example Labs",
    )

    assert decision.as_dict() == {
        "route": "requires_manual_relay",
        "action": "request_email_verification_relay",
        "reason": "mailbox_capability_missing",
        "missing_capabilities": ["mailbox_get_message"],
    }


def test_verification_requires_authorization_and_scoped_identity() -> None:
    capabilities = EmailCapabilities(mailbox_search=True, mailbox_get_message=True)
    unauthorized = resolve_email_route(
        intent="verification",
        capabilities=capabilities,
        mailbox_access_authorized=False,
        mailbox="candidate@example.com",
        employer="Example Labs",
    )
    unscoped = resolve_email_route(
        intent="verification",
        capabilities=capabilities,
        mailbox_access_authorized=True,
        mailbox="candidate@example.com",
    )

    assert unauthorized.reason == "mailbox_access_not_authorized"
    assert unscoped.reason == "current_application_identity_required"
    assert unauthorized.route == unscoped.route == "requires_manual_relay"


def test_email_only_application_routes_to_send_when_authorized() -> None:
    decision = resolve_email_route(
        intent="direct_application",
        capabilities=["email.send"],
        standing_application_authorized=True,
    )

    assert decision.as_dict() == {
        "route": "route_to_email",
        "action": "prepare_and_send_direct_application",
        "reason": "authorized_direct_email_send_available",
    }


@pytest.mark.parametrize(
    ("capabilities", "authorized", "action", "reason"),
    [
        (
            [],
            True,
            "request_direct_email_send_relay",
            "direct_email_send_capability_missing",
        ),
        (
            ["direct_email_send"],
            False,
            "request_direct_email_authorization",
            "direct_email_send_not_authorized",
        ),
    ],
)
def test_email_only_application_returns_structured_handoff_when_not_routable(
    capabilities: list[str],
    authorized: bool,
    action: str,
    reason: str,
) -> None:
    decision = resolve_email_route(
        intent="direct_application",
        capabilities=capabilities,
        standing_application_authorized=authorized,
    )

    assert decision.route == "requires_manual_relay"
    assert decision.action == action
    assert decision.reason == reason


def test_route_envelope_has_no_secret_or_message_payload_fields() -> None:
    decision = resolve_email_route(
        intent="verification",
        capabilities=["mailbox_search", "mailbox_get_message"],
        mailbox_access_authorized=True,
        mailbox="candidate@example.com",
        ats_domain="ats.example.test",
    )
    serialized = json.dumps(decision.as_dict()).casefold()

    assert "password" not in serialized
    assert "message_body" not in serialized
    assert "verification_code" not in serialized
    assert '"return_sensitive_value": false' in serialized


def test_unknown_intent_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported email intent"):
        resolve_email_route(intent="newsletter", capabilities=[])  # type: ignore[arg-type]


def test_mailbox_mcp_spec_is_provider_replaceable_and_secret_free() -> None:
    spec = resolve_mailbox_mcp_spec(
        {
            "server_name": "enterprise_mail",
            "package": None,
            "command": "mail-mcp",
            "extra_args": ["--stdio"],
            "search_tool": "find_messages",
            "read_tool": "get_message",
            "send_tool": "deliver_message",
            "env": {"MAIL_TOKEN": "secret-value"},
        }
    )

    assert spec.process_args() == ["--stdio"]
    assert spec.enabled_tools() == ["find_messages", "get_message"]
    assert spec.enabled_tools(direct_email_send_authorized=True) == [
        "find_messages",
        "get_message",
        "deliver_message",
    ]
    assert spec.metadata()["environment_keys"] == ["MAIL_TOKEN"]
    assert "secret-value" not in repr(spec.metadata())


def test_mailbox_mcp_spec_can_be_disabled_or_overridden_from_environment() -> None:
    disabled = resolve_mailbox_mcp_spec(
        environ={"APPLYPILOT_MAILBOX_MCP_ENABLED": "false"}
    )
    replaced = resolve_mailbox_mcp_spec(
        environ={
            "APPLYPILOT_MAILBOX_MCP_SERVER_NAME": "mail_reader",
            "APPLYPILOT_MAILBOX_MCP_PACKAGE": "provider-mail-mcp@next",
            "APPLYPILOT_MAILBOX_MCP_SEARCH_TOOL": "query_mail",
            "APPLYPILOT_MAILBOX_MCP_READ_TOOL": "fetch_mail",
        }
    )

    assert not disabled.enabled
    assert replaced.server_name == "mail_reader"
    assert replaced.package == "provider-mail-mcp@next"
    assert replaced.enabled_tools() == ["query_mail", "fetch_mail"]


def test_mailbox_mcp_rejects_unsafe_server_or_tool_names() -> None:
    with pytest.raises(ValueError, match="server/tool names"):
        MailboxMcpSpec(server_name="mailbox.injected=true")


def _email_plan(path) -> dict[str, object]:
    return {
        "route": "direct_email",
        "recipient": "jobs@example.test",
        "recipient_domain": "example.test",
        "recipient_source": "official_listing",
        "subject": "Application - Data Analyst",
        "attachment_names": [path.name],
        "attachments_verified": True,
        "body_sha256": "a" * 64,
        "duplicate_check": {
            "folder": "sent",
            "completed": True,
            "duplicate_found": False,
            "provider_query_id": "query-1",
        },
        "listing_evidence": "Official listing instructs applications to jobs@example.test",
    }


def _email_job(path, plan: dict[str, object] | None = None) -> dict[str, object]:
    selected_plan = plan or _email_plan(path)
    search_input = {
        "query": (
            f'in:sent to:{selected_plan["recipient"]} '
            f'subject:"{selected_plan["subject"]}"'
        )
    }
    duplicate_receipt = mailbox_prepare_duplicate_receipt(
        search_input,
        {"messages": []},
        selected_plan,
    )
    assert duplicate_receipt is not None
    return {
        "url": "https://jobs.example.test/role",
        "description": "Apply by email to jobs@example.test for this role.",
        "_staged_resume_path": str(path),
        "_mailbox_prepare_duplicate_receipt": duplicate_receipt,
    }


def test_prepare_plan_binds_official_recipient_body_and_attachment_bytes(tmp_path) -> None:
    attachment = tmp_path / "Candidate_Resume.pdf"
    attachment.write_bytes(b"resume-pdf")
    job = {**_email_job(attachment), "_attempt_id": "attempt-1"}

    normalized = normalize_prepared_email_application(_email_plan(attachment), job)

    assert normalized is not None
    assert normalized["body_sha256"] == "a" * 64
    assert normalized["listing_source_url"] == job["url"]
    assert normalized["duplicate_check"]["provider_query_id"] == job[
        "_mailbox_prepare_duplicate_receipt"
    ]["query_digest"]
    assert normalized["attachments"] == [
        {
            "name": attachment.name,
            "path": str(attachment.resolve()),
            "sha256": hashlib.sha256(b"resume-pdf").hexdigest(),
        }
    ]


@pytest.mark.parametrize(
    "missing",
    ["recipient_source", "listing_evidence", "body_sha256", "duplicate_check"],
)
def test_prepare_plan_rejects_unbound_or_unverified_fields(tmp_path, missing: str) -> None:
    attachment = tmp_path / "Candidate_Resume.pdf"
    attachment.write_bytes(b"resume-pdf")
    plan = _email_plan(attachment)
    plan.pop(missing)

    assert normalize_prepared_email_application(
        plan,
        _email_job(attachment, plan),
    ) is None


def test_send_capability_requires_exact_submit_reservation(tmp_path) -> None:
    attachment = tmp_path / "Candidate_Resume.pdf"
    attachment.write_bytes(b"resume-pdf")
    job = {**_email_job(attachment), "_attempt_id": "attempt-1"}
    plan = normalize_prepared_email_application(_email_plan(attachment), job)
    assert plan is not None
    reservation = reserve_direct_email_send(job, plan)
    submit_job = {
        **job,
        "_browser_observation": {"email_application": plan},
        "_direct_email_send_reservation": reservation,
    }

    assert direct_email_send_is_reserved(submit_job, submission_phase="submit")
    assert not direct_email_send_is_reserved(submit_job, submission_phase="prepare")
    assert not direct_email_send_is_reserved(
        {**submit_job, "url": "https://jobs.example.test/other"},
        submission_phase="submit",
    )
    assert not direct_email_send_is_reserved(
        {key: value for key, value in submit_job.items() if key != "_attempt_id"},
        submission_phase="submit",
    )


def test_sent_receipt_requires_provider_identity_and_exact_plan_match(tmp_path) -> None:
    attachment = tmp_path / "Candidate_Resume.pdf"
    attachment.write_bytes(b"resume-pdf")
    job = _email_job(attachment)
    plan = normalize_prepared_email_application(_email_plan(attachment), job)
    assert plan is not None
    receipt = {
        "folder": "sent",
        "recipient": plan["recipient"],
        "subject": plan["subject"],
        "attachment_names": plan["attachment_names"],
        "body_sha256": plan["body_sha256"],
        "provider_message_id": "provider-message-1",
    }

    assert normalize_sent_receipt(receipt, plan) == receipt
    assert normalize_sent_receipt({**receipt, "folder": "inbox"}, plan) is None
    assert normalize_sent_receipt({**receipt, "provider_message_id": ""}, plan) is None
    assert normalize_sent_receipt({**receipt, "subject": "Different role"}, plan) is None


def test_actual_send_input_must_match_reserved_body_recipient_and_attachments(tmp_path) -> None:
    attachment = tmp_path / "Candidate_Resume.pdf"
    attachment.write_bytes(b"resume-pdf")
    body = "Dear hiring team, please find my application attached."
    raw_plan = _email_plan(attachment)
    raw_plan["body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    plan = normalize_prepared_email_application(
        raw_plan,
        _email_job(attachment, raw_plan),
    )
    assert plan is not None
    send_input = {
        "to": "jobs@example.test",
        "subject": plan["subject"],
        "body": body,
        "attachments": [str(attachment)],
    }

    assert mailbox_send_input_matches_plan(send_input, plan)
    assert not mailbox_send_input_matches_plan({**send_input, "body": "different"}, plan)
    assert not mailbox_send_input_matches_plan(
        {**send_input, "to": "other@example.test"},
        plan,
    )


def test_sent_search_and_read_are_bound_to_provider_evidence(tmp_path) -> None:
    attachment = tmp_path / "Candidate_Resume.pdf"
    attachment.write_bytes(b"resume-pdf")
    body = "Dear hiring team, please find my application attached."
    raw_plan = _email_plan(attachment)
    raw_plan["body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    plan = normalize_prepared_email_application(
        raw_plan,
        _email_job(attachment, raw_plan),
    )
    assert plan is not None
    search_input = {
        "query": f'in:sent to:{plan["recipient"]} subject:"{plan["subject"]}"'
    }
    search_output = {"messages": [{"id": "provider-message-1"}]}

    assert mailbox_sent_search_input_matches_plan(search_input, plan)
    assert not mailbox_sent_search_input_matches_plan({"query": "in:sent"}, plan)
    assert mailbox_search_message_id(search_output) == "provider-message-1"
    assert mailbox_search_message_id(
        {"messages": [{"id": "one"}, {"id": "two"}]}
    ) is None
    assert mailbox_read_input_matches_message(
        {"message_id": "provider-message-1"},
        "provider-message-1",
    )

    read_output = {
        "message": {
            "folder": "SENT",
            "to": [plan["recipient"]],
            "subject": plan["subject"],
            "attachments": [{"filename": attachment.name}],
            "body": body,
            "id": "provider-message-1",
        }
    }
    receipt = normalize_mailbox_read_receipt(
        read_output,
        plan,
        "provider-message-1",
    )

    assert receipt == {
        "folder": "sent",
        "recipient": plan["recipient"],
        "subject": plan["subject"],
        "attachment_names": plan["attachment_names"],
        "body_sha256": plan["body_sha256"],
        "provider_message_id": "provider-message-1",
    }
    assert "body" not in receipt
    assert normalize_mailbox_read_receipt(
        {"message": {**read_output["message"], "folder": "INBOX"}},
        plan,
        "provider-message-1",
    ) is None


def test_prepare_duplicate_receipt_requires_structured_zero_results(tmp_path) -> None:
    attachment = tmp_path / "Candidate_Resume.pdf"
    attachment.write_bytes(b"resume-pdf")
    plan = _email_plan(attachment)
    search_input = {
        "query": f'in:sent to:{plan["recipient"]} subject:"{plan["subject"]}"'
    }

    assert mailbox_prepare_duplicate_receipt(
        search_input,
        {"messages": []},
        plan,
    ) is not None
    assert mailbox_prepare_duplicate_receipt(
        search_input,
        {"messages": [{"id": "existing-message"}]},
        plan,
    ) is None
    assert mailbox_prepare_duplicate_receipt(
        search_input,
        "No messages found",
        plan,
    ) is None


def test_prepare_plan_requires_recipient_in_original_listing(tmp_path) -> None:
    attachment = tmp_path / "Candidate_Resume.pdf"
    attachment.write_bytes(b"resume-pdf")
    plan = _email_plan(attachment)
    job = _email_job(attachment, plan)
    job["description"] = "Apply through our careers portal."

    assert normalize_prepared_email_application(plan, job) is None


def test_prepare_plan_requires_launcher_duplicate_receipt(tmp_path) -> None:
    attachment = tmp_path / "Candidate_Resume.pdf"
    attachment.write_bytes(b"resume-pdf")
    plan = _email_plan(attachment)
    plan["duplicate_check"]["provider_query_id"] = "agent-invented-query-id"
    job = _email_job(attachment, plan)
    runtime_digest = job["_mailbox_prepare_duplicate_receipt"]["query_digest"]

    normalized = normalize_prepared_email_application(plan, job)

    assert normalized is not None
    assert normalized["duplicate_check"]["provider_query_id"] == runtime_digest
    assert normalized["duplicate_check"]["provider_query_id"] != plan[
        "duplicate_check"
    ]["provider_query_id"]
    assert normalize_prepared_email_application(
        plan,
        {key: value for key, value in job.items() if key != "_mailbox_prepare_duplicate_receipt"},
    ) is None
