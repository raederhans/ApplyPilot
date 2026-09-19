"""Regressions found while reviewing the resume-library integration."""

import json

import pytest

from applypilot import resume_library
from applypilot.resume_versions import changed_used_facts
from applypilot.scoring.cover_letter import load_evidence_sources
from applypilot.scoring.pdf import build_html, parse_resume
from applypilot.scoring.scorer import build_score_input_binding
from applypilot.scoring.validator import _section_entries


def test_renderer_preserves_supported_section_aliases_and_uppercase_employers():
    text = """Candidate
candidate@example.test
Core Skills
Languages: Python
WORK HISTORY
ACME CORPORATION
Analyst | Jan 2025 - Present
- Delivered client analysis.
Relevant Projects
Research Observatory
Maintainer | Jan 2024 - Present
- Evaluated transfer performance.
Academic Background
Example University, MSc, 2025
"""
    parsed = parse_resume(text)
    assert parsed["contact"] == "candidate@example.test"
    assert parsed["section_order"] == ["TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"]
    html = build_html(parsed)
    for value in ("ACME CORPORATION", "Research Observatory", "Example University"):
        assert value in html
    assert _section_entries(text, "EXPERIENCE")[0]["title"] == "ACME CORPORATION"
    assert _section_entries(text, "PROJECTS")[0]["title"] == "Research Observatory"


def test_section_aliases_do_not_overwrite_earlier_section_content():
    parsed = parse_resume("Candidate\ncandidate@example.test\nPROJECTS\nFirst Project\n"
                          "- First evidence.\nSELECTED PROJECTS\nSecond Project\n- Second evidence.")
    assert "First Project" in parsed["sections"]["PROJECTS"]
    assert "Second Project" in parsed["sections"]["PROJECTS"]
    assert parsed["section_order"] == ["PROJECTS"]


@pytest.mark.parametrize("description,required,preferred", [
    ("Required: Python; SQL; AWS.", ["aws", "python", "sql"], []),
    ("Preferred: Python; SQL; AWS.", [], ["aws", "python", "sql"]),
    ("Required: Python; SQL; AWS preferred.", ["python", "sql"], ["aws"]),
])
def test_semicolon_lists_keep_requirement_intent(description, required, preferred):
    profile = resume_library.extract_job_profile({"full_description": description})
    assert profile["required_skills"] == required
    assert profile["preferred_skills"] == preferred


@pytest.mark.parametrize("description", ["CV should not exceed two pages", "Resume: maximum 2 pages", "resume of no more than 2 pages"])
def test_explicit_page_caps(description):
    assert resume_library._maximum_resume_pages(description) == 2


@pytest.mark.parametrize("change", ["edit", "remove", "profile", "prompt"])
def test_all_actual_scoring_context_is_bound(tmp_path, change):
    primary = tmp_path / "primary.txt"
    primary.write_text("Python", encoding="utf-8")
    extra = tmp_path / "extra.txt"
    extra.write_text("AWS project evidence", encoding="utf-8")
    profile = {"cover_letter": {"evidence_sources": [str(extra)]}}
    job = {"url": "https://example.test/job", "title": "Analyst", "full_description": "Required: Python and AWS."}
    binding = build_score_input_binding(job, primary, "Python", profile=profile,
                                        evidence_sources=load_evidence_sources(profile, primary, "Python"))
    job["score_evidence_json"] = json.dumps({"input_binding": binding})
    assert resume_library._score_binding_error(job, profile) is None
    if change == "edit":
        extra.write_text("No relevant evidence", encoding="utf-8")
    elif change == "remove":
        extra.unlink()
    elif change == "profile":
        profile["contact_preferences"] = {"email_application_availability_policy": "Available from December"}
    else:
        binding["prompt_revision"] = "old-policy"
        job["score_evidence_json"] = json.dumps({"input_binding": binding})
    assert "rescore" in resume_library._score_binding_error(job, profile)


def test_new_contact_facts_reject_explicit_old_header_values():
    text = "Candidate\nold@example.test | +65 9000 1111\nEXPERIENCE\n"
    after = {"personal": {"email": "new@example.test", "phone": "+65 9000 2222"}}
    assert set(changed_used_facts({}, after, text)) == {"personal.email", "personal.phone"}
    assert not changed_used_facts({}, {"personal": {"phone": "9000 1111"}}, text)


def test_unknown_named_required_tools_are_not_silently_ignored():
    extracted = resume_library.extract_job_profile({"full_description": "Required: Databricks, Snowflake and Docker."})
    assert extracted["required_skills"] == ["databricks", "docker", "snowflake"]


def test_degree_lists_do_not_become_unknown_required_tools():
    extracted = resume_library.extract_job_profile({"full_description":
        "Required: pursuing IT, CS, data science, analytics or related discipline; analytical skills."})
    assert "cs" not in extracted["required_skills"]


def test_advantageous_but_not_required_heading_resets_required_section():
    extracted = resume_library.extract_job_profile({"full_description":
        "Qualifications:\nPython\nAdvantageous, but not required:\n"
        "Familiarity with tools such as Jira, Notion, or Microsoft Project."})
    assert extracted["required_skills"] == ["python"]
    assert "jira" in extracted["preferred_skills"]
