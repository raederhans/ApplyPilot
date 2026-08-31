from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.apply import agent_runtime, credential_relay, credential_relay_mcp, prompt
from applypilot.apply.contracts import ensure_persistable
from applypilot.apply.page_observation import _redact_protected_identifier_snapshot


def _profile() -> dict:
    return {
        "personal": {
            "full_name": "Taylor Chen",
            "email": "candidate@example.com",
            "phone": "+65 9000 0000",
            "city": "Singapore",
            "country": "Singapore",
        },
        "work_authorization": {
            "legally_authorized_to_work": "Conditional",
            "require_sponsorship": "Role-specific",
        },
        "compensation": {
            "salary_currency": "SGD",
            "salary_expectation": "Negotiable",
        },
        "availability": {"earliest_start_date": "2026-11-10"},
        "education": [
            {
                "institution": "Nanyang Technological University",
                "degree": "MCAAI",
                "start_date": "August 2026",
                "expected_graduation": "May 2027",
            },
            {
                "institution": "University of Pennsylvania",
                "degree": "Master of City Planning",
                "start_date": "August 2024",
                "graduation": "May 2026",
            },
            {
                "institution": "University College London",
                "degree": "BSc Urban Planning",
                "start_date": "September 2021",
                "graduation": "May 2024",
            },
        ],
        "application_material_policy": {
            "ntu_academic_proof": {
                "label": "Current NTU transcript or enrolment proof",
                "availability": "Not currently supplied",
                "optional_field_action": "leave blank",
                "required_field_action": "stop before submission and skip this job",
                "substitution_policy": "Never use another school's document as NTU proof",
            },
            "programme_credit_or_placement_proof": {
                "label": "Programme-credit-bearing or placement approval proof",
                "availability": "Unavailable before an offer and NTU confirmation",
                "optional_field_action": "leave blank",
                "required_field_action": "stop before submission and skip this job",
                "substitution_policy": "Never fabricate an offer or approval document",
            },
        },
        "identity_materials": {
            "fin": {
                "secure_relay_authorized": True,
            }
        },
        "application_facts": [
            {
                "key": "nightlight_ml_framework_experience",
                "value": "scikit-learn; TensorFlow",
                "context": (
                    "Keyword-only, ongoing modeling work; do not imply completed "
                    "delivery, production maturity, duration, or outcomes"
                ),
                "source": "user_confirmed",
                "confirmed_at": "2026-08-31",
            },
            {
                "key": "pytorch_learning_and_project_exploration",
                "value": "PyTorch",
                "context": "Keyword-only, ongoing learning and early project exploration",
                "source": "user_confirmed",
                "confirmed_at": "2026-08-31",
            },
        ],
    }


def test_education_start_dates_are_rendered_for_the_browser_agent() -> None:
    summary = prompt._build_profile_summary(_profile())

    assert "August 2026 - May 2027" in summary
    assert "August 2024 - May 2026" in summary
    assert "September 2021 - May 2024" in summary


def test_unavailable_material_and_fin_rules_are_agent_consumable() -> None:
    section = prompt._build_identity_materials_section(
        _profile(),
        identity_relay_authorized=True,
    )

    assert "Current NTU transcript or enrolment proof" in section
    assert "optional field = leave blank" in section
    assert "required field = stop before submission and skip this job" in section
    assert "Never fabricate an offer or approval document" in section
    assert "mcp__credential_relay__fill_protected_identifier" in section
    assert "stored FIN/NRIC number is not an uploadable identity document" in section


def test_full_prepare_prompt_consumes_profile_dates_and_material_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = _profile()
    profile["screening"] = {}
    profile["mobility"] = {}
    profile["eeo_voluntary"] = {}
    profile["authentication"] = {}
    profile["submission_policy"] = {}
    resume = tmp_path / "resume.txt"
    resume.write_text("Verified resume", encoding="utf-8")
    resume.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(prompt.config, "load_profile", lambda: profile)
    monkeypatch.setattr(
        prompt.config,
        "load_search_config",
        lambda: {"location": {"primary": "Singapore"}},
    )
    job = {
        "url": "https://jobs.example.test/1",
        "application_url": "https://jobs.example.test/1/apply",
        "title": "Data Intern",
        "company_name": "Example",
        "source_site": "official",
        "tailored_resume_path": str(resume),
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
        "_browser_backend": "edge",
    }

    built = prompt.build_prompt(
        job,
        "Verified resume",
        dry_run=True,
        worker_dir=tmp_path / "worker",
        identity_relay_authorized=True,
    )

    assert "August 2026 - May 2027" in built
    assert "Current NTU transcript or enrolment proof" in built
    assert "stop before submission and skip this job" in built
    assert "mcp__credential_relay__fill_protected_identifier" in built
    assert "scikit-learn; TensorFlow" in built
    assert "PyTorch" in built
    assert "Keyword-only, ongoing" in built


def test_identity_only_relay_is_exposed_without_ats_password_tool(tmp_path: Path) -> None:
    command, final_path = agent_runtime.build_agent_command(
        "claude",
        "sonnet",
        9432,
        tmp_path,
        tmp_path / "mcp.json",
        resolve_claude=lambda: ["claude"],
        identity_relay_authorized=True,
    )
    rendered = " ".join(command)

    assert "mcp__credential_relay__fill_protected_identifier" in rendered
    assert "mcp__credential_relay__fill_ats_credentials" not in rendered
    assert final_path is None

    config = agent_runtime.make_mcp_config(
        9432,
        python_executable="python",
        identity_relay_authorized=True,
    )
    assert config["mcpServers"]["credential_relay"] == {
        "command": "python",
        "args": ["-m", "applypilot.apply.credential_relay_mcp"],
    }


def test_mcp_refuses_fin_before_profile_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPLYPILOT_IDENTITY_RELAY_AUTHORIZED", raising=False)
    monkeypatch.setattr(
        credential_relay_mcp,
        "_decrypt_fin",
        lambda _path: pytest.fail("FIN must not be decrypted"),
    )

    response = credential_relay_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "fill_protected_identifier",
                "arguments": {"kind": "fin"},
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    assert "not authorized" in response["result"]["content"][0]["text"]


def test_fin_field_matching_does_not_match_financial_fields() -> None:
    assert credential_relay.FIN_FIELD_RE.search("NRIC / FIN identification number")
    assert credential_relay.FIN_FIELD_RE.search("FIN Number (required)")
    assert credential_relay.FIN_FIELD_RE.search("Financial information") is None


def test_protected_identifier_values_are_redacted_and_rejected_from_reports() -> None:
    snapshot = {
        "text_fields": [
            {
                "text": "NRIC / FIN identification number",
                "value": "M1234567A",
            }
        ]
    }

    _redact_protected_identifier_snapshot(snapshot)

    field = snapshot["text_fields"][0]
    assert field == {
        "text": "NRIC / FIN identification number",
        "value": "[redacted-present]",
        "value_present": True,
        "protected_identifier": True,
    }
    with pytest.raises(ValueError, match="protected identity number"):
        ensure_persistable({"observation": "M1234567A"})
    with pytest.raises(ValueError, match="protected identity number"):
        ensure_persistable({"observation": "Applicant entered M1234567A successfully"})
    with pytest.raises(ValueError, match="may not be persisted"):
        ensure_persistable({"fin": "redacted"})


def test_protected_identifier_redaction_honors_explicit_marker_and_id() -> None:
    snapshot = {
        "text_fields": [
            {
                "text": "Identification",
                "value": "M1234567A",
                "protected_identifier": True,
            },
            {
                "text": "Identification",
                "id": "fin",
                "value": "M1234567A",
            },
        ]
    }

    _redact_protected_identifier_snapshot(snapshot)

    assert [field["value"] for field in snapshot["text_fields"]] == [
        "[redacted-present]",
        "[redacted-present]",
    ]
    assert all(field["protected_identifier"] for field in snapshot["text_fields"])
