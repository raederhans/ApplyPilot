from __future__ import annotations

import re

from applypilot.scoring.validator import (
    validate_json_fields,
    validate_tailored_resume,
)


def test_supplemental_projects_are_evidence_not_mandatory_selection():
    source = _base_source_resume()
    short = re.sub(r"(?ms)^PROJECTS\n.*?(?=^EDUCATION)", "", source)
    evidence = short + "\n\nSUPPLEMENTAL CANDIDATE EVIDENCE\n" + source
    for result_text in (short, source):
        validation = validate_tailored_resume(result_text, {}, original_text=evidence,
                                               selection_source_text=short)
        assert validation["passed"], validation["errors"]
    unsupported = source.replace("Agent Project", "Unfounded Biomedical Compiler")
    validation = validate_tailored_resume(unsupported, {}, original_text=evidence,
                                           selection_source_text=short)
    assert not validation["passed"]
    assert any("no evidence mapping" in e for e in validation["errors"])


def test_duplicate_project_identity_is_rejected_despite_different_bullets():
    source = _base_source_resume()
    duplicated = source.replace("Analytics Dashboard", "Agent Project")
    result = validate_tailored_resume(duplicated, {}, original_text=source)
    assert any("Duplicate project entry" in error for error in result["errors"])


def _base_source_resume() -> str:
    return """Ryan Yu
ryan@example.com | +65 9000 0000

SUMMARY
Applied AI engineer building Python analytics and workflow automation products.

TECHNICAL SKILLS
Programming: Python, SQL, TypeScript
AI: RAG, tool calling, workflow automation

EXPERIENCE
Recent Company
AI Engineer | 2025 - Present
- Built Python workflow for analytics reporting with validated outputs.
- Designed automated testing pipelines for reliable system delivery.

Earlier Company
Data Analyst | 2023 - 2024
- Built SQL dashboards for stakeholder reporting and planning decisions.
- Maintained data integration pipelines across recurring release cycles.

PROJECTS
Agent Project
Developer | 2026
- Built a RAG assistant with traceable sources and tool calling.
- Tested retrieval and failure paths with repeatable evaluation cases.

Analytics Dashboard
Developer | 2024
- Built interactive analytics dashboard with SQL data models.
- Implemented real-time metric tracking for operational users.

EDUCATION
Example University, Master of Computing, 2027
"""


def test_experience_chronology_enforced_by_actual_dates() -> None:
    source = _base_source_resume()
    profile = {
        "personal": {},
        "resume_facts": {"preserved_companies": ["Recent Company", "Earlier Company"]},
        "skills_boundary": {},
    }

    # Chronologically inverted: 2023-2024 before 2025-Present
    inverted_data = {
        "title": "AI Engineer",
        "skills": {"AI": "Python, RAG"},
        "experience": [
            {
                "header": "Earlier Company",
                "subtitle": "Data Analyst | 2023 - 2024",
                "bullets": [
                    "Built SQL dashboards for stakeholder reporting and planning decisions.",
                    "Maintained data integration pipelines across recurring release cycles.",
                ],
            },
            {
                "header": "Recent Company",
                "subtitle": "AI Engineer | 2025 - Present",
                "bullets": [
                    "Built Python workflow for analytics reporting with validated outputs.",
                    "Designed automated testing pipelines for reliable system delivery.",
                ],
            },
        ],
        "projects": [
            {
                "header": "Agent Project",
                "subtitle": "Developer | 2026",
                "bullets": [
                    "Built a RAG assistant with traceable sources and tool calling.",
                    "Tested retrieval and failure paths with repeatable evaluation cases.",
                ],
            }
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python workflow",
                "support_level": "direct",
                "source_quote": "Built Python workflow for analytics reporting with validated outputs.",
            },
            {
                "requirement": "RAG assistant",
                "support_level": "direct",
                "source_quote": "Built a RAG assistant with traceable sources and tool calling.",
            },
        ],
    }

    result = validate_json_fields(
        inverted_data,
        profile,
        original_text=source,
        selection_source_text=source,
    )
    assert result["passed"] is False
    assert any("reverse chronological order" in error for error in result["errors"])

    # Correct reverse chronological order: 2025-Present before 2023-2024
    correct_data = dict(inverted_data)
    correct_data["experience"] = [
        inverted_data["experience"][1],
        inverted_data["experience"][0],
    ]
    correct_result = validate_json_fields(
        correct_data,
        profile,
        original_text=source,
        selection_source_text=source,
    )
    assert not any("reverse chronological order" in error for error in correct_result["errors"])


def test_projects_may_freely_reorder_by_relevance() -> None:
    source = _base_source_resume()
    profile = {
        "personal": {},
        "resume_facts": {"preserved_companies": ["Recent Company", "Earlier Company"]},
        "skills_boundary": {},
    }

    # Projects reordered: Analytics Dashboard (2024) placed before Agent Project (2026) due to JD relevance
    reordered_data = {
        "title": "Data Analyst",
        "skills": {"Data": "Python, SQL"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "AI Engineer | 2025 - Present",
                "bullets": [
                    "Built Python workflow for analytics reporting with validated outputs.",
                    "Designed automated testing pipelines for reliable system delivery.",
                ],
            },
            {
                "header": "Earlier Company",
                "subtitle": "Data Analyst | 2023 - 2024",
                "bullets": [
                    "Built SQL dashboards for stakeholder reporting and planning decisions.",
                    "Maintained data integration pipelines across recurring release cycles.",
                ],
            },
        ],
        "projects": [
            {
                "header": "Analytics Dashboard",
                "subtitle": "Developer | 2024",
                "bullets": [
                    "Built interactive analytics dashboard with SQL data models.",
                    "Implemented real-time metric tracking for operational users.",
                ],
            },
            {
                "header": "Agent Project",
                "subtitle": "Developer | 2026",
                "bullets": [
                    "Built a RAG assistant with traceable sources and tool calling.",
                    "Tested retrieval and failure paths with repeatable evaluation cases.",
                ],
            },
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python workflow",
                "support_level": "direct",
                "source_quote": "Built Python workflow for analytics reporting with validated outputs.",
            },
            {
                "requirement": "SQL dashboards",
                "support_level": "direct",
                "source_quote": "Built SQL dashboards for stakeholder reporting and planning decisions.",
            },
        ],
    }

    result = validate_json_fields(
        reordered_data,
        profile,
        original_text=source,
        selection_source_text=source,
    )
    assert result["passed"] is True
    assert not any("project" in error.casefold() for error in result["errors"])


def test_relevant_older_experience_more_bullets() -> None:
    source = _base_source_resume()
    profile = {
        "personal": {},
        "resume_facts": {"preserved_companies": ["Recent Company", "Earlier Company"]},
        "skills_boundary": {},
    }

    # Earlier Company has 4 bullets, Recent Company has 2 bullets (because Earlier Company is more relevant to the JD)
    data = {
        "title": "Data Analyst",
        "skills": {"Data": "Python, SQL"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "AI Engineer | 2025 - Present",
                "bullets": [
                    "Built Python workflow for analytics reporting with validated outputs.",
                    "Designed automated testing pipelines for reliable system delivery.",
                ],
            },
            {
                "header": "Earlier Company",
                "subtitle": "Data Analyst | 2023 - 2024",
                "bullets": [
                    "Built SQL dashboards for stakeholder reporting and planning decisions.",
                    "Maintained data integration pipelines across recurring release cycles.",
                    "Designed schema migrations and query optimizations for warehouse performance.",
                    "Authored data documentation and automated validation checks for analysts.",
                ],
            },
        ],
        "projects": [
            {
                "header": "Agent Project",
                "subtitle": "Developer | 2026",
                "bullets": [
                    "Built a RAG assistant with traceable sources and tool calling.",
                    "Tested retrieval and failure paths with repeatable evaluation cases.",
                ],
            }
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python workflow",
                "support_level": "direct",
                "source_quote": "Built Python workflow for analytics reporting with validated outputs.",
            },
            {
                "requirement": "SQL dashboards",
                "support_level": "direct",
                "source_quote": "Built SQL dashboards for stakeholder reporting and planning decisions.",
            },
        ],
    }

    result = validate_json_fields(
        data,
        profile,
        original_text=source,
        selection_source_text=source,
    )
    assert result["passed"] is True
    assert not any("expand with age" in error for error in result["errors"])
    assert not any("bullet allocation" in error for error in result["errors"])


def test_summary_optional_but_quality_validated_when_present() -> None:
    source = _base_source_resume()
    profile = {
        "personal": {},
        "resume_facts": {"preserved_companies": ["Recent Company", "Earlier Company"]},
        "skills_boundary": {},
    }

    base_data = {
        "title": "AI Engineer",
        "skills": {"AI": "Python, RAG"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "AI Engineer | 2025 - Present",
                "bullets": [
                    "Built Python workflow for analytics reporting with validated outputs.",
                    "Designed automated testing pipelines for reliable system delivery.",
                ],
            },
            {
                "header": "Earlier Company",
                "subtitle": "Data Analyst | 2023 - 2024",
                "bullets": [
                    "Built SQL dashboards for stakeholder reporting and planning decisions.",
                    "Maintained data integration pipelines across recurring release cycles.",
                ],
            },
        ],
        "projects": [
            {
                "header": "Agent Project",
                "subtitle": "Developer | 2026",
                "bullets": [
                    "Built a RAG assistant with traceable sources and tool calling.",
                    "Tested retrieval and failure paths with repeatable evaluation cases.",
                ],
            }
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python workflow",
                "support_level": "direct",
                "source_quote": "Built Python workflow for analytics reporting with validated outputs.",
            },
            {
                "requirement": "RAG assistant",
                "support_level": "direct",
                "source_quote": "Built a RAG assistant with traceable sources and tool calling.",
            },
        ],
    }

    # Case 1: Summary is completely omitted
    data_no_summary = dict(base_data)
    result_no_summary = validate_json_fields(
        data_no_summary,
        profile,
        original_text=source,
        selection_source_text=source,
    )
    assert result_no_summary["passed"] is True
    assert not any("summary" in error.casefold() for error in result_no_summary["errors"])

    # Case 2: Summary is present and contains banned filler words in strict mode
    data_banned_summary = dict(base_data)
    data_banned_summary["summary"] = "Passionate and dedicated AI engineer building scalable solutions."
    result_banned = validate_json_fields(
        data_banned_summary,
        profile,
        mode="strict",
        original_text=source,
        selection_source_text=source,
    )
    assert result_banned["passed"] is False
    assert any("banned words" in error.casefold() for error in result_banned["errors"])

    # Case 3: Summary is present and contains JD-only claims not in source
    data_jd_claim_summary = dict(base_data)
    data_jd_claim_summary["summary"] = "AI engineer specializing in fintech monetization and clinical trials."
    result_jd_claim = validate_json_fields(
        data_jd_claim_summary,
        profile,
        original_text=source,
        selection_source_text=source,
        job_description="Looking for an AI engineer to drive clinical monetization.",
    )
    assert result_jd_claim["passed"] is False
    assert any("imports jd-only claim terms" in error.casefold() for error in result_jd_claim["errors"])


def test_sourced_supplemental_project_allowed() -> None:
    source = _base_source_resume()
    combined_evidence = (
        source
        + "\n\nSUPPLEMENTAL CANDIDATE EVIDENCE\n"
        + "Supplemental Cloud Infrastructure\n"
        + "Infrastructure Engineer | 2025\n"
        + "- Built Terraform modules for AWS deployment with automated linting.\n"
        + "- Automated multi-region backup and disaster recovery validation.\n"
    )
    profile = {
        "personal": {},
        "resume_facts": {"preserved_companies": ["Recent Company", "Earlier Company"]},
        "skills_boundary": {},
    }

    # Tailored output retires Analytics Dashboard and includes Supplemental Cloud Infrastructure
    data = {
        "title": "Cloud AI Engineer",
        "skills": {"Cloud": "Python, Terraform"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "AI Engineer | 2025 - Present",
                "bullets": [
                    "Built Python workflow for analytics reporting with validated outputs.",
                    "Designed automated testing pipelines for reliable system delivery.",
                ],
            },
            {
                "header": "Earlier Company",
                "subtitle": "Data Analyst | 2023 - 2024",
                "bullets": [
                    "Built SQL dashboards for stakeholder reporting and planning decisions.",
                    "Maintained data integration pipelines across recurring release cycles.",
                ],
            },
        ],
        "projects": [
            {
                "header": "Supplemental Cloud Infrastructure",
                "subtitle": "Infrastructure Engineer | 2025",
                "bullets": [
                    "Built Terraform modules for AWS deployment with automated linting.",
                    "Automated multi-region backup and disaster recovery validation.",
                ],
            },
            {
                "header": "Agent Project",
                "subtitle": "Developer | 2026",
                "bullets": [
                    "Built a RAG assistant with traceable sources and tool calling.",
                    "Tested retrieval and failure paths with repeatable evaluation cases.",
                ],
            },
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python workflow",
                "support_level": "direct",
                "source_quote": "Built Python workflow for analytics reporting with validated outputs.",
            },
            {
                "requirement": "Terraform AWS",
                "support_level": "direct",
                "source_quote": "Built Terraform modules for AWS deployment with automated linting.",
            },
        ],
    }

    result = validate_json_fields(
        data,
        profile,
        original_text=combined_evidence,
        selection_source_text=source,
    )
    assert result["passed"] is True
    assert not any("supplemental" in error.casefold() for error in result["errors"])
    assert not any("no evidence mapping" in error.casefold() for error in result["errors"])


def test_unsupported_project_and_experience_entries_rejected() -> None:
    source = _base_source_resume()
    profile = {
        "personal": {},
        "resume_facts": {"preserved_companies": ["Recent Company", "Earlier Company"]},
        "skills_boundary": {},
    }

    # Case 1: Fabricated project not in source or supplemental evidence
    data_fake_project = {
        "title": "AI Engineer",
        "skills": {"AI": "Python, RAG"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "AI Engineer | 2025 - Present",
                "bullets": [
                    "Built Python workflow for analytics reporting with validated outputs.",
                    "Designed automated testing pipelines for reliable system delivery.",
                ],
            },
            {
                "header": "Earlier Company",
                "subtitle": "Data Analyst | 2023 - 2024",
                "bullets": [
                    "Built SQL dashboards for stakeholder reporting and planning decisions.",
                    "Maintained data integration pipelines across recurring release cycles.",
                ],
            },
        ],
        "projects": [
            {
                "header": "Agent Project",
                "subtitle": "Developer | 2026",
                "bullets": [
                    "Built a RAG assistant with traceable sources and tool calling.",
                    "Tested retrieval and failure paths with repeatable evaluation cases.",
                ],
            },
            {
                "header": "Fabricated Crypto Blockchain Platform",
                "subtitle": "Lead Architect | 2025",
                "bullets": [
                    "Engineered decentralized smart contracts with zero knowledge proofs.",
                    "Designed high-frequency consensus algorithms for cross-chain liquidity.",
                ],
            },
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python workflow",
                "support_level": "direct",
                "source_quote": "Built Python workflow for analytics reporting with validated outputs.",
            },
            {
                "requirement": "RAG assistant",
                "support_level": "direct",
                "source_quote": "Built a RAG assistant with traceable sources and tool calling.",
            },
        ],
    }

    result_project = validate_json_fields(
        data_fake_project,
        profile,
        original_text=source,
        selection_source_text=source,
    )
    assert result_project["passed"] is False
    assert any(
        "Project entry 'Fabricated Crypto Blockchain Platform' has no evidence mapping"
        in error
        for error in result_project["errors"]
    )

    # Case 2: Fabricated experience not in source or supplemental evidence
    data_fake_exp = {
        "title": "AI Engineer",
        "skills": {"AI": "Python, RAG"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "AI Engineer | 2025 - Present",
                "bullets": [
                    "Built Python workflow for analytics reporting with validated outputs.",
                    "Designed automated testing pipelines for reliable system delivery.",
                ],
            },
            {
                "header": "Invented Silicon Valley Startup",
                "subtitle": "VP of AI | 2024",
                "bullets": [
                    "Led foundational model research with custom neural architectures.",
                    "Deployed edge inference models to millions of consumer devices.",
                ],
            },
        ],
        "projects": [
            {
                "header": "Agent Project",
                "subtitle": "Developer | 2026",
                "bullets": [
                    "Built a RAG assistant with traceable sources and tool calling.",
                    "Tested retrieval and failure paths with repeatable evaluation cases.",
                ],
            }
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python workflow",
                "support_level": "direct",
                "source_quote": "Built Python workflow for analytics reporting with validated outputs.",
            },
            {
                "requirement": "RAG assistant",
                "support_level": "direct",
                "source_quote": "Built a RAG assistant with traceable sources and tool calling.",
            },
        ],
    }

    result_exp = validate_json_fields(
        data_fake_exp,
        profile,
        original_text=source,
        selection_source_text=source,
    )
    assert result_exp["passed"] is False
    assert any(
        "Experience entry 'Invented Silicon Valley Startup' has no evidence mapping"
        in error
        for error in result_exp["errors"]
    )


def test_count_and_word_targets_are_advisory_without_readability_risk() -> None:
    source = _base_source_resume()
    profile = {
        "personal": {"full_name": "Ryan Yu"},
        "resume_facts": {
            "preserved_school": "Example University",
            "preserved_companies": ["Recent Company", "Earlier Company"],
        },
        "tailoring": {
            "resume_layout": {"project_resume_min_words": 320}
        },
    }

    # Concise but substantive resume (approx 180 words)
    concise_resume = """Ryan Yu
ryan@example.com | +65 9000 0000

TECHNICAL SKILLS
Programming: Python, SQL, TypeScript
AI: RAG, tool calling, workflow automation

EXPERIENCE
Recent Company
AI Engineer | 2025 - Present
- Built Python workflow for analytics reporting with validated outputs.
- Designed automated testing pipelines for reliable system delivery.

Earlier Company
Data Analyst | 2023 - 2024
- Built SQL dashboards for stakeholder reporting and planning decisions.
- Maintained data integration pipelines across recurring release cycles.

PROJECTS
Agent Project
Developer | 2026
- Built a RAG assistant with traceable sources and tool calling.
- Tested retrieval and failure paths with repeatable evaluation cases.

Analytics Dashboard
Developer | 2024
- Built interactive analytics dashboard with SQL data models.
- Implemented real-time metric tracking for operational users.

EDUCATION
Example University, Master of Computing, 2027
"""
    result = validate_tailored_resume(concise_resume, profile, original_text=source)
    # Target is advisory: no error, only warning
    assert result["passed"] is True
    assert any("advisory minimum" in warning or "under-evidenced" in warning for warning in result["warnings"])


def test_fake_project_sharing_words_with_supplemental_bullet_is_rejected() -> None:
    source = _base_source_resume()
    combined_evidence = (
        source
        + "\n\nSUPPLEMENTAL CANDIDATE EVIDENCE\n"
        + "Supplemental Cloud Infrastructure\n"
        + "Infrastructure Engineer | 2025\n"
        + "- Built Terraform modules for AWS deployment with automated linting.\n"
        + "- Automated multi-region backup and disaster recovery validation.\n"
    )
    profile = {
        "personal": {"full_name": "Ryan Yu"},
        "resume_facts": {
            "preserved_school": "Example University",
            "preserved_companies": ["Recent Company", "Earlier Company"],
        },
        "skills_boundary": {},
    }

    # Fake project "Terraform Modules" shares 2 words ("terraform", "modules")
    # with a bullet in the supplemental evidence, but does not match any entry identity.
    fake_project_data = {
        "title": "Cloud Engineer",
        "skills": {"Cloud": "Python, Terraform"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "AI Engineer | 2025 - Present",
                "bullets": [
                    "Built Python workflow for analytics reporting with validated outputs.",
                    "Designed automated testing pipelines for reliable system delivery.",
                ],
            },
            {
                "header": "Earlier Company",
                "subtitle": "Data Analyst | 2023 - 2024",
                "bullets": [
                    "Built SQL dashboards for stakeholder reporting and planning decisions.",
                    "Maintained data integration pipelines across recurring release cycles.",
                ],
            },
        ],
        "projects": [
            {
                "header": "Agent Project",
                "subtitle": "Developer | 2026",
                "bullets": [
                    "Built a RAG assistant with traceable sources and tool calling.",
                    "Tested retrieval and failure paths with repeatable evaluation cases.",
                ],
            },
            {
                "header": "Terraform Modules",
                "subtitle": "Developer | 2025",
                "bullets": [
                    "Created cloud infrastructure blueprints with modular patterns.",
                    "Automated resource provisioning across multiple cloud environments.",
                ],
            },
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python workflow",
                "support_level": "direct",
                "source_quote": "Built Python workflow for analytics reporting with validated outputs.",
            },
            {
                "requirement": "RAG assistant",
                "support_level": "direct",
                "source_quote": "Built a RAG assistant with traceable sources and tool calling.",
            },
        ],
    }

    # JSON validation must reject the fake project
    result_json = validate_json_fields(
        fake_project_data,
        profile,
        original_text=combined_evidence,
        selection_source_text=source,
    )
    assert result_json["passed"] is False
    assert any(
        "Project entry 'Terraform Modules' has no evidence mapping" in error
        for error in result_json["errors"]
    )

    # Text validation must also reject the fake project
    resume_text = """Ryan Yu
ryan@example.com | +65 9000 0000

TECHNICAL SKILLS
Cloud: Python, Terraform

EXPERIENCE
Recent Company
AI Engineer | 2025 - Present
- Built Python workflow for analytics reporting with validated outputs.
- Designed automated testing pipelines for reliable system delivery.

Earlier Company
Data Analyst | 2023 - 2024
- Built SQL dashboards for stakeholder reporting and planning decisions.
- Maintained data integration pipelines across recurring release cycles.

PROJECTS
Agent Project
Developer | 2026
- Built a RAG assistant with traceable sources and tool calling.
- Tested retrieval and failure paths with repeatable evaluation cases.

Terraform Modules
Developer | 2025
- Created cloud infrastructure blueprints with modular patterns.
- Automated resource provisioning across multiple cloud environments.

EDUCATION
Example University, Master of Computing, 2027
"""
    result_text = validate_tailored_resume(resume_text, profile, original_text=combined_evidence)
    assert result_text["passed"] is False
    assert any(
        "Project entry 'Terraform Modules' has no evidence mapping" in error
        for error in result_text["errors"]
    )


def test_source_misordered_but_output_corrected_is_accepted() -> None:
    # Source resume has EXPERIENCE in inverted order (Older first)
    misordered_source = """Ryan Yu
ryan@example.com | +65 9000 0000

TECHNICAL SKILLS
Programming: Python, SQL

EXPERIENCE
Earlier Company
Data Analyst | 2023 - 2024
- Built SQL dashboards for stakeholder reporting and planning decisions.
- Maintained data integration pipelines across recurring release cycles.

Recent Company
AI Engineer | 2025 - Present
- Built Python workflow for analytics reporting with validated outputs.
- Designed automated testing pipelines for reliable system delivery.

PROJECTS
Agent Project
Developer | 2026
- Built a RAG assistant with traceable sources and tool calling.
- Tested retrieval and failure paths with repeatable evaluation cases.

EDUCATION
Example University, Master of Computing, 2027
"""
    profile = {
        "personal": {"full_name": "Ryan Yu"},
        "resume_facts": {
            "preserved_school": "Example University",
            "preserved_companies": ["Recent Company", "Earlier Company"],
        },
        "skills_boundary": {},
    }

    # Output corrects the order: Recent Company (2025-Present) first, Earlier Company (2023-2024) second
    corrected_data = {
        "title": "AI Engineer",
        "skills": {"Programming": "Python, SQL"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "AI Engineer | 2025 - Present",
                "bullets": [
                    "Built Python workflow for analytics reporting with validated outputs.",
                    "Designed automated testing pipelines for reliable system delivery.",
                ],
            },
            {
                "header": "Earlier Company",
                "subtitle": "Data Analyst | 2023 - 2024",
                "bullets": [
                    "Built SQL dashboards for stakeholder reporting and planning decisions.",
                    "Maintained data integration pipelines across recurring release cycles.",
                ],
            },
        ],
        "projects": [
            {
                "header": "Agent Project",
                "subtitle": "Developer | 2026",
                "bullets": [
                    "Built a RAG assistant with traceable sources and tool calling.",
                    "Tested retrieval and failure paths with repeatable evaluation cases.",
                ],
            }
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python workflow",
                "support_level": "direct",
                "source_quote": "Built Python workflow for analytics reporting with validated outputs.",
            },
            {
                "requirement": "SQL dashboards",
                "support_level": "direct",
                "source_quote": "Built SQL dashboards for stakeholder reporting and planning decisions.",
            },
        ],
    }

    # JSON validation must accept the corrected reverse-chronological order
    result_json = validate_json_fields(
        corrected_data,
        profile,
        original_text=misordered_source,
        selection_source_text=misordered_source,
    )
    assert result_json["passed"] is True
    assert not any("reverse chronological order" in error for error in result_json["errors"])
    assert not any("most recent experience entry" in error for error in result_json["errors"])

    # Text validation must also accept the corrected order
    corrected_text = """Ryan Yu
ryan@example.com | +65 9000 0000

TECHNICAL SKILLS
Programming: Python, SQL

EXPERIENCE
Recent Company
AI Engineer | 2025 - Present
- Built Python workflow for analytics reporting with validated outputs.
- Designed automated testing pipelines for reliable system delivery.

Earlier Company
Data Analyst | 2023 - 2024
- Built SQL dashboards for stakeholder reporting and planning decisions.
- Maintained data integration pipelines across recurring release cycles.

PROJECTS
Agent Project
Developer | 2026
- Built a RAG assistant with traceable sources and tool calling.
- Tested retrieval and failure paths with repeatable evaluation cases.

EDUCATION
Example University, Master of Computing, 2027
"""
    result_text = validate_tailored_resume(corrected_text, profile, original_text=misordered_source)
    assert result_text["passed"] is True
    assert not any("reverse chronological order" in error for error in result_text["errors"])


def test_count5_accepted_warning() -> None:
    source = _base_source_resume()
    profile = {
        "personal": {"full_name": "Ryan Yu"},
        "resume_facts": {
            "preserved_school": "Example University",
            "preserved_companies": ["Recent Company", "Earlier Company"],
        },
        "skills_boundary": {},
    }

    # Recent Company has 5 bullets
    data = {
        "title": "AI Engineer",
        "skills": {"AI": "Python, RAG"},
        "experience": [
            {
                "header": "Recent Company",
                "subtitle": "AI Engineer | 2025 - Present",
                "bullets": [
                    "Built Python workflow for analytics reporting with validated outputs.",
                    "Designed automated testing pipelines for reliable system delivery.",
                    "Implemented asynchronous job queues for high-throughput batch evaluation.",
                    "Authored developer documentation and architecture decision records.",
                    "Mentored junior engineers on code quality and unit testing best practices.",
                ],
            },
            {
                "header": "Earlier Company",
                "subtitle": "Data Analyst | 2023 - 2024",
                "bullets": [
                    "Built SQL dashboards for stakeholder reporting and planning decisions.",
                    "Maintained data integration pipelines across recurring release cycles.",
                ],
            },
        ],
        "projects": [
            {
                "header": "Agent Project",
                "subtitle": "Developer | 2026",
                "bullets": [
                    "Built a RAG assistant with traceable sources and tool calling.",
                    "Tested retrieval and failure paths with repeatable evaluation cases.",
                ],
            }
        ],
        "education": "Example University, Master of Computing, 2027",
        "evidence_map": [
            {
                "requirement": "Python workflow",
                "support_level": "direct",
                "source_quote": "Built Python workflow for analytics reporting with validated outputs.",
            },
            {
                "requirement": "SQL dashboards",
                "support_level": "direct",
                "source_quote": "Built SQL dashboards for stakeholder reporting and planning decisions.",
            },
        ],
    }

    # JSON validation: count 5 is accepted with warning, not an error
    result_json = validate_json_fields(
        data,
        profile,
        original_text=source,
        selection_source_text=source,
    )
    assert result_json["passed"] is True
    assert not any("four bullets" in error for error in result_json["errors"])
    assert any("four bullets" in warning for warning in result_json["warnings"])

    # Text validation: count 5 is accepted with warning, not an error
    text = """Ryan Yu
ryan@example.com | +65 9000 0000

TECHNICAL SKILLS
Programming: Python, SQL

EXPERIENCE
Recent Company
AI Engineer | 2025 - Present
- Built Python workflow for analytics reporting with validated outputs.
- Designed automated testing pipelines for reliable system delivery.
- Implemented asynchronous job queues for high-throughput batch evaluation.
- Authored developer documentation and architecture decision records.
- Mentored junior engineers on code quality and unit testing best practices.

Earlier Company
Data Analyst | 2023 - 2024
- Built SQL dashboards for stakeholder reporting and planning decisions.
- Maintained data integration pipelines across recurring release cycles.

PROJECTS
Agent Project
Developer | 2026
- Built a RAG assistant with traceable sources and tool calling.
- Tested retrieval and failure paths with repeatable evaluation cases.

EDUCATION
Example University, Master of Computing, 2027
"""
    result_text = validate_tailored_resume(text, profile, original_text=source)
    assert result_text["passed"] is True
    assert not any("four bullets" in error for error in result_text["errors"])
    assert any("four bullets" in warning for warning in result_text["warnings"])
