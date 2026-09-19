from __future__ import annotations

import json
from pathlib import Path

from applypilot.resume_library import assess_resume_artifact_health
from applypilot.scoring import tailor
from applypilot.scoring.resume_plan import build_content_plan
from applypilot.scoring.validator import (
    _best_source_match_index,
    _match_source_indices,
    validate_json_fields,
)


def _resume_text(*, leading_bullets: int = 2) -> str:
    bullets = "\n".join(
        f"- Built Python workflow {index} for analytics reporting with validated outputs."
        for index in range(leading_bullets)
    )
    return f"""Ryan Yu
ryan@example.com | +65 9000 0000

SUMMARY
Applied AI engineer building Python analytics and workflow automation products.

TECHNICAL SKILLS
Programming: Python, SQL, TypeScript
AI: RAG, tool calling, workflow automation

EXPERIENCE
Recent Company
AI Engineer | 2025 - Present
{bullets}

Earlier Company
Data Analyst | 2024
- Built SQL dashboards for stakeholder reporting and planning decisions.

PROJECTS
Agent Project
Independent Developer | 2026
- Built a RAG assistant with traceable sources and tool calling.
- Tested retrieval and failure paths with repeatable evaluation cases.

EDUCATION
Example University, Master of Computing, 2027
"""


def test_content_plan_selects_role_preset_and_prioritizes_jd_evidence() -> None:
    plan = build_content_plan(
        _resume_text(),
        {
            "track": "ai_implementation",
            "subtype": "ai_solutions",
            "employment_type": "full_time_or_unspecified",
            "required_skills": ["python", "rag"],
            "preferred_skills": ["typescript"],
            "deliverables": ["workflow", "prototype"],
            "features": {"content_terms": ["retrieval", "tool calling"]},
        },
        {"candidates": [{"overall_score": 0.82}]},
    )

    assert plan["preset"] == "technical_project"
    assert plan["page_intent"] == "one_page_preferred"
    assert plan["experience"][0]["retirement_allowed"] is False
    assert plan["projects"][0]["priority"] == "primary"
    assert plan["projects"][0]["bullet_budget"] >= 2


def test_cross_review_fact_prompt_does_not_duplicate_quality_scoring() -> None:
    prompt = tailor._build_judge_prompt(
        {"skills_boundary": {}, "resume_facts": {}},
        factual_only=True,
    )

    assert "Do not judge usefulness or writing preference" in prompt
    assert "an independent reviewer handles those" in prompt
    assert "Fail a section when it is materially thin" not in prompt


def test_short_evidence_quote_expands_to_exact_containing_source_line() -> None:
    evidence = (
        "- Delivered a commissioned legal-information assistant with a hybrid-RAG data layer "
        "for traceable statute and case retrieval."
    )
    normalized = tailor._normalize_evidence_map_quotes(
        [
            {
                "requirement": "hybrid RAG",
                "support_level": "direct",
                "source_quote": "hybrid-RAG data layer",
            }
        ],
        evidence,
    )

    assert normalized[0]["source_quote"] == evidence.removeprefix("- ")


def test_artifact_health_quarantines_obvious_thin_leading_entry(tmp_path: Path) -> None:
    text_path = tmp_path / "resume.txt"
    text_path.write_text(_resume_text(leading_bullets=1), encoding="utf-8")
    assessment = assess_resume_artifact_health(
        {
            "artifact_id": "resume:thin",
            "text_path": str(text_path),
            "validation_status": "source_only",
        },
        {
            "personal": {},
            "resume_facts": {},
            "tailoring": {"resume_layout": {"project_resume_min_words": 1}},
        },
    )

    assert assessment["status"] == "repair_required"
    assert any(
        "leading experience entry" in reason.casefold()
        for reason in assessment["reasons"]
    )


def test_artifact_health_requires_repair_for_recency_allocation_inversion(
    tmp_path: Path,
) -> None:
    text_path = tmp_path / "resume.txt"
    text_path.write_text(
        _resume_text().replace(
            "\nPROJECTS\n",
            "\nOldest Company\nLegacy Analyst | 2022\n"
            "- Built a legacy reporting workflow with documented validation checks.\n"
            "- Produced recurring planning outputs for stakeholder review and delivery.\n"
            "- Coordinated source-data checks across a recurring reporting cycle.\n"
            "\nPROJECTS\n",
        ),
        encoding="utf-8",
    )

    assessment = assess_resume_artifact_health(
        {
            "artifact_id": "resume:inverted-allocation",
            "text_path": str(text_path),
            "validation_status": "source_only",
        },
        {
            "personal": {},
            "resume_facts": {},
            "tailoring": {"resume_layout": {"project_resume_min_words": 1}},
        },
    )

    assert assessment["status"] == "repair_required"
    assert any("bullet allocation" in reason.casefold() for reason in assessment["reasons"])


def test_cross_review_combines_independent_fact_and_quality_verdicts(monkeypatch) -> None:
    resume = _resume_text()
    summary = "Applied AI engineer building Python analytics and workflow automation products."
    bullets = [
        line[2:].strip() for line in resume.splitlines() if line.startswith("- ")
    ]
    claim_audits = [
        {
            "section": "SUMMARY",
            "claim": summary,
            "source_quotes": [summary],
            "supported": True,
        },
        *[
            {
                "section": "PROJECTS" if "RAG assistant" in bullet or "retrieval" in bullet else "EXPERIENCE",
                "claim": bullet,
                "source_quotes": [bullet],
                "supported": True,
            }
            for bullet in bullets
        ],
    ]
    fact_review = {
        "verdict": "PASS",
        "issues": [],
        "section_reviews": [
            {
                "section": section,
                "verdict": "PASS",
                "issues": [],
                "relevance": "high",
                "density": "strong",
            }
            for section in (
                "SUMMARY",
                "TECHNICAL SKILLS",
                "EXPERIENCE",
                "PROJECTS",
                "EDUCATION",
            )
        ],
        "claim_audits": claim_audits,
    }
    quality_review = {
        "verdict": "PASS",
        "dimensions": {
            "jd_alignment": 86,
            "section_allocation": 82,
            "content_density": 84,
            "narrative_completeness": 85,
            "specificity": 88,
            "redundancy_control": 91,
            "professional_style": 87,
        },
        "issues": [],
        "section_reviews": [],
    }

    class FakeClient:
        def __init__(self) -> None:
            self.responses = [fact_review, quality_review]
            self.last_response_meta = {}

        def chat(self, *args, **kwargs):
            return json.dumps(self.responses.pop(0))

    client = FakeClient()
    monkeypatch.setattr(tailor, "get_client", lambda: client)
    result = tailor.judge_tailored_resume(
        resume,
        resume,
        "AI Engineer",
        {"skills_boundary": {}, "resume_facts": {}},
        job_description="Build Python RAG workflows and analytics prototypes.",
        cross_review=True,
        content_plan={"preset": "technical_project"},
    )

    assert result["passed"] is True
    assert result["review_mode"] == "independent_factual_and_quality_cross_review"
    assert result["quality_overall_score"] >= 80
    assert client.responses == []


def test_content_plan_front_loads_detail_by_recency_even_when_old_entry_matches_jd() -> None:
    resume = _resume_text().replace(
        "\nPROJECTS\n",
        "\nOldest Company\nLegacy Specialist | 2020\n"
        "- Built Python RAG retrieval and tool calling workflows for validated analytics reporting.\n"
        "\nPROJECTS\n",
    )
    plan = build_content_plan(
        resume,
        {
            "track": "ai_implementation",
            "employment_type": "full_time_or_unspecified",
            "required_skills": ["python", "rag", "tool calling"],
            "preferred_skills": [],
            "deliverables": ["retrieval workflow"],
            "features": {"content_terms": ["validated analytics"]},
        },
    )

    counts = [int(item["bullet_budget"]) for item in plan["experience"]]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] > counts[-1]
    assert plan["experience"][-1]["relevance_score"] > 0


def test_entry_order_matching_prefers_exact_company_over_shared_planning_tokens() -> None:
    headers = [
        "Damon & Ryan Design and Planning LLC.",
        "WRT",
        "China Academy of Urban Planning and Design",
    ]

    assert _best_source_match_index(headers[0], headers) == 0
    assert _best_source_match_index(headers[2], headers) == 2


def test_entry_matching_does_not_reuse_one_planning_header_for_two_sources() -> None:
    source_headers = [
        "Damon & Ryan Design and Planning LLC.",
        "WRT",
        "China Academy of Urban Planning and Design",
    ]

    assert _match_source_indices([source_headers[2]], source_headers) == [2]
    assert set(_match_source_indices([source_headers[0], source_headers[2]], source_headers)) == {
        0,
        2,
    }


def test_content_plan_repairs_misordered_artifact_from_preserved_dates() -> None:
    source = """Ryan Yu
ryan@example.com

SUMMARY
Analyst building validated workflows.

TECHNICAL SKILLS
Data: Python, SQL

EXPERIENCE
Older Company
Analyst | May 2024 - Aug 2024
- Built a validated reporting workflow for planning reviews and delivery teams.

Current Company
Lead Analyst | May 2024 - Present
- Delivered a current analytics product with traceable outputs for client review.
- Coordinated implementation and validation across recurring project delivery cycles.

Recent Internship
Analyst Intern | Jun 2025 - Aug 2025
- Built scheduled Python pipelines for recurring operational reporting and validation.

PROJECTS
Current Project
Developer | Aug 2026 - Present
- Built a current workflow tool with documented validation and release checks.

EDUCATION
Example University, Master of Computing, 2027
"""
    plan = build_content_plan(
        source,
        {
            "track": "data_bi_decision",
            "employment_type": "internship",
            "required_skills": ["python", "sql"],
            "preferred_skills": [],
            "deliverables": ["reporting"],
        },
    )

    assert [item["header"] for item in plan["experience"]] == [
        "Current Company",
        "Recent Internship",
        "Older Company",
    ]


def test_validator_rejects_recency_inversion_and_combined_education_paragraph() -> None:
    source = """Ryan Yu
ryan@example.com

SUMMARY
Data analyst building validated workflows for planning decisions.

TECHNICAL SKILLS
Data: Python, SQL

EXPERIENCE
Recent Company
Analyst | 2026
- Built a validated Python workflow for recurring operational reporting and review.
- Designed SQL checks that surfaced missing records before stakeholder delivery.
- Documented the reporting process so analysts could reproduce each published output.

Middle Company
Analyst Intern | 2024
- Prepared recurring planning reports from verified public datasets for project teams.

Old Company
Assistant | 2021
- Cleaned multi-source records and produced a quality-checked dataset for analysis.

EDUCATION
School A, Degree A, 2027
School B, Degree B, 2024
"""
    data = {
        "title": "Data Analyst",
        "summary": "Data analyst building validated workflows for planning decisions.",
        "skills": {"Data": "Python, SQL"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "Analyst | 2026",
                "bullets": [
                    "Built a validated Python workflow for recurring operational reporting and review.",
                    "Designed SQL checks that surfaced missing records before stakeholder delivery.",
                    "Documented the reporting process so analysts could reproduce each published output.",
                ],
            },
            {
                "header": "Middle Company",
                "subtitle": "Analyst Intern | 2024",
                "bullets": ["Prepared recurring planning reports from verified public datasets for project teams."],
            },
            {
                "header": "Old Company",
                "subtitle": "Assistant | 2021",
                "bullets": [
                    "Cleaned multi-source records and produced a quality-checked dataset for analysis.",
                    "Built supporting charts that helped reviewers compare alternative planning scenarios.",
                ],
            },
        ],
        "projects": [],
        "education": "School A, Degree A, 2027; School B, Degree B, 2024",
        "evidence_map": [
            {
                "requirement": "Python",
                "support_level": "direct",
                "source_quote": "Built a validated Python workflow for recurring operational reporting and review.",
            },
            {
                "requirement": "SQL",
                "support_level": "direct",
                "source_quote": "Designed SQL checks that surfaced missing records before stakeholder delivery.",
            },
        ],
    }
    result = validate_json_fields(
        data,
        {
            "resume_facts": {"preserved_school": "School A; School B"},
            "skills_boundary": {},
        },
        original_text=source,
        selection_source_text=source,
    )

    assert result["passed"] is False
    assert any("must not expand with age" in error for error in result["errors"])
    assert any("one separate item/line" in error for error in result["errors"])


def test_structural_failure_uses_one_section_scoped_repair(monkeypatch) -> None:
    source = """Ryan Yu
ryan@example.com | +65 9000 0000

SUMMARY
Python and SQL analyst building validated reporting workflows.

TECHNICAL SKILLS
Data: Python, SQL

EXPERIENCE
Recent Company
Analyst | 2025 - Present
- Built Python reporting workflows for operational analysis.
- Validated SQL outputs with repeatable quality checks.

Older Company
Assistant | 2024
- Prepared stakeholder reports from public datasets.

PROJECTS
Recent Project
Builder | 2026
- Built a Python analytics prototype for reporting teams.
- Tested output quality with repeatable evaluation cases.

Old Project
Builder | 2024
- Produced a compact SQL dashboard for planning decisions.

EDUCATION
Example University, Master of Computing, 2027
"""
    job = {
        "title": "Data Analyst Intern",
        "company_name": "Example",
        "full_description": "Use Python and SQL to build reporting workflows.",
    }
    base_data = {
        "title": "Data Analyst Intern",
        "summary": "Python and SQL analyst building validated reporting workflows.",
        "skills": {"Data": "Python, SQL"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "Analyst | 2025 - Present",
                "bullets": [
                    "Built Python reporting workflows for operational analysis.",
                    "Validated SQL outputs with repeatable quality checks.",
                ],
            },
            {
                "header": "Older Company",
                "subtitle": "Assistant | 2024",
                "bullets": ["Prepared stakeholder reports from public datasets."],
            },
        ],
        "projects": [
            {
                "header": "Old Project",
                "subtitle": "Builder | 2024",
                "bullets": ["Produced a compact SQL dashboard for planning decisions."],
            }
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python reporting",
                "support_level": "direct",
                "source_quote": "Built Python reporting workflows for operational analysis.",
            },
            {
                "requirement": "SQL quality",
                "support_level": "direct",
                "source_quote": "Validated SQL outputs with repeatable quality checks.",
            },
            {
                "requirement": "Reporting workflows",
                "support_level": "transferable",
                "source_quote": "Prepared stakeholder reports from public datasets.",
            },
        ],
    }
    repaired_data = json.loads(json.dumps(base_data))
    repaired_data["projects"] = [
        {
            "header": "Recent Project",
            "subtitle": "Builder | 2026",
            "bullets": [
                "Built a Python analytics prototype for reporting teams.",
                "Tested output quality with repeatable evaluation cases.",
            ],
        },
        *base_data["projects"],
    ]

    class FakeClient:
        def __init__(self) -> None:
            self.responses = [base_data, repaired_data]
            self.last_response_meta = {}

        def chat(self, *args, **kwargs):
            return json.dumps(self.responses.pop(0))

    client = FakeClient()
    monkeypatch.setattr(tailor, "get_client", lambda: client)
    monkeypatch.setattr(
        tailor,
        "judge_tailored_resume",
        lambda *args, **kwargs: {
            "passed": True,
            "verdict": "PASS",
            "issues": {},
            "failed_sections": [],
        },
    )
    text, report = tailor.tailor_resume(
        source,
        job,
        {"personal": {}, "resume_facts": {}, "skills_boundary": {}},
        max_retries=0,
        validation_mode="strict",
    )

    assert report["status"] == "machine_validated"
    assert report["attempts"] == 1
    assert report["local_repair"]["sections"] == ["PROJECTS"]
    assert report["local_repair"]["changed_fields"] == ["projects"]
    assert "Recent Project" in text
    assert "Old Project" in text
    assert client.responses == []
