"""Contracts for content-addressed resume reuse and append-only provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.main import get_command

from applypilot import resume_library, single_job
from applypilot.cli import app
from applypilot.database import init_db
from applypilot.resume_library import (
    TAXONOMY_VERSION,
    extract_job_profile,
    library_status,
    project_reuse_to_job,
    route_resume_for_job,
    sync_resume_library,
)
from applypilot.scoring import tailor


def test_resume_taxonomy_version_tracks_routing_term_changes() -> None:
    assert TAXONOMY_VERSION == "resume-library-v5"


def test_resume_route_cli_exposes_explicit_candidate_selection() -> None:
    resume_route = get_command(app).commands["resume-route"]
    params = {param.name: param for param in resume_route.params}

    assert params["artifact_id"].opts == ["--artifact-id"]
    assert params["artifact_id"].hidden is False
    assert params["project_reuse"].opts == ["--project-reuse"]
    assert params["project_reuse"].hidden is False


def _profile(base_resume: Path) -> dict:
    return {
        "skills_boundary": {"languages": ["Python", "SQL"]},
        "tailoring": {
            "resume_variants": [
                {
                    "track": "data_bi_decision_analysis",
                    "path": str(base_resume),
                    "keywords": ["data analyst", "sql", "dashboard"],
                }
            ]
        },
    }


def _insert_job(conn, *, url: str, title: str, description: str, **fields: object) -> None:
    values = {
        "company_name": "Example",
        "application_url": url + "/apply",
        "eligibility_status": "eligible",
        **fields,
    }
    columns = ["url", "title", "full_description", *values]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders})",
        [url, title, description, *values.values()],
    )
    conn.commit()


def _validated_history(
    tmp_path: Path,
    conn,
    base_resume: Path,
    *,
    suffix: str = "one",
    content: str | None = None,
) -> str:
    text_path = tmp_path / f"validated-{suffix}.txt"
    text_path.write_text(
        content
        or "DATA ANALYST\nSQL, Python, data analysis\nBuilt a dashboard and reporting workflow.",
        encoding="utf-8",
    )
    text_path.with_suffix(".pdf").write_bytes(b"synthetic-pdf-binding-" + suffix.encode())
    report_path = tmp_path / f"validated-{suffix}-report.json"
    report_path.write_text('{"status":"machine_validated"}', encoding="utf-8")
    url = f"https://careers.example.test/history-{suffix}"
    _insert_job(
        conn,
        url=url,
        title="Data Analyst",
        description="Required: SQL. Build dashboards and reporting for business decisions.",
        tailored_resume_path=str(text_path),
        tailor_source_resume_path=str(base_resume),
        tailor_report_path=str(report_path),
        tailor_status="machine_validated",
        tailored_at="2026-08-25T00:00:00+00:00",
    )
    return url


def test_sync_does_not_nest_an_existing_resume_library_artifact(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "library.db")
    base_resume = tmp_path / "base.txt"
    base_resume.write_text("Python and SQL", encoding="utf-8")
    artifact_root = tmp_path / "resume-library" / "artifacts"
    artifact_root.mkdir(parents=True)
    text_path = artifact_root / "resume-existing.txt"
    text_path.write_text("DATA ANALYST\nPython and SQL", encoding="utf-8")
    text_path.with_suffix(".pdf").write_bytes(b"synthetic-pdf")
    report_path = tmp_path / "validation.json"
    report_path.write_text('{"status":"machine_validated"}', encoding="utf-8")
    _insert_job(
        conn,
        url="https://careers.example.test/existing-artifact",
        title="Data Analyst",
        description="Required: SQL and Python.",
        tailored_resume_path=str(text_path),
        tailor_source_resume_path=str(base_resume),
        tailor_report_path=str(report_path),
        tailor_status="machine_validated",
        tailored_at="2026-08-26T00:00:00+00:00",
    )

    sync_resume_library(conn, profile=_profile(base_resume))

    stored = conn.execute(
        "SELECT text_path, pdf_path FROM resume_artifacts WHERE validation_status='machine_validated'"
    ).fetchone()
    assert Path(stored["text_path"]).parent == artifact_root
    assert Path(stored["pdf_path"]).parent == artifact_root
    assert not (artifact_root / "resume-library").exists()


def test_schema_and_job_profile_use_fine_grained_subtype(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "library.db")
    profile = extract_job_profile(
        {
            "url": "https://example.test/jobs/1",
            "title": "Business Intelligence Analyst",
            "full_description": "Required: SQL. Build Power BI dashboards.",
        },
        {"skills_boundary": {"languages": ["SQL"]}},
    )

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {
        "resume_artifacts",
        "resume_coverage_cells",
        "resume_artifact_aliases",
        "job_resume_profiles",
        "job_resume_assignments",
        "resume_validation_runs",
    } <= tables
    assert profile["track"] == "data_bi_decision"
    assert profile["subtype"] == "business_intelligence"
    assert profile["required_skills"] == ["sql"]


def test_job_profile_does_not_promote_trailing_preferred_skills_to_required() -> None:
    profile = extract_job_profile(
        {
            "url": "https://example.test/temasek-data",
            "title": "Data Analytics Intern",
            "full_description": (
                "Requirements: Proficiency in Python, R, and SQL. "
                "Experience with AWS, S3, JFrog, Kubernetes preferred. "
                "Experience with QlikSense, Snowflake, Tableau, and Git preferred but not required."
            ),
        },
        {},
    )

    assert profile["required_skills"] == ["python", "r", "sql"]
    assert profile["preferred_skills"] == ["aws", "git", "tableau"]


def test_route_does_not_turn_preferred_skills_into_hard_gaps(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("Python, R, and SQL analytics experience.", encoding="utf-8")
    _validated_history(
        tmp_path,
        conn,
        base,
        content="DATA ANALYST\nPython, R, and SQL. Built analytics dashboards.",
    )
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)

    route = route_resume_for_job(
        conn,
        {
            "url": "https://example.test/temasek-data-route",
            "title": "Data Analytics Intern",
            "full_description": (
                "Proficiency in Python, R, and SQL. "
                "Experience with AWS, S3, JFrog, Kubernetes preferred. "
                "Experience with QlikSense, Snowflake, Tableau, and Git preferred but not required."
            ),
            "eligibility_status": "eligible",
        },
        profile,
    )

    assert route["hard_gaps"] == []
    assert route["job_profile"]["required_skills"] == ["python", "r", "sql"]


def test_job_profile_keeps_explicit_required_clauses_after_preferred_clauses() -> None:
    profile = extract_job_profile(
        {
            "url": "https://example.test/mixed-requirements",
            "title": "Data Analytics Intern",
            "full_description": (
                "AWS is preferred but not required. "
                "Python and SQL are required for this internship."
            ),
        },
        {},
    )

    assert profile["required_skills"] == ["python", "sql"]
    assert profile["preferred_skills"] == ["aws"]


def test_job_profile_treats_same_clause_preferred_and_required_by_nearest_marker() -> None:
    profile = extract_job_profile(
        {
            "url": "https://example.test/mixed-clause",
            "title": "Data Analytics Intern",
            "full_description": "AWS is preferred, but Python and SQL are required.",
        },
        {},
    )

    assert profile["required_skills"] == ["python", "sql"]
    assert profile["preferred_skills"] == ["aws"]


@pytest.mark.parametrize(
    ("description", "required", "preferred"),
    [
        ("Required Python, preferred AWS.", ["python"], ["aws"]),
        ("Python is required; AWS preferred.", ["python"], ["aws"]),
        ("Python is not preferred but is required.", ["python"], []),
        ("Preferred Python, SQL is required.", ["sql"], ["python"]),
        ("Python optional, SQL required.", ["sql"], ["python"]),
    ],
)
def test_job_profile_honors_prefix_and_negated_requirement_markers(
    description: str, required: list[str], preferred: list[str]
) -> None:
    profile = extract_job_profile(
        {
            "url": "https://example.test/marker-boundaries",
            "title": "Data Analytics Intern",
            "full_description": description,
        },
        {},
    )

    assert profile["required_skills"] == required
    assert profile["preferred_skills"] == preferred


def test_senior_or_high_experience_role_is_routed_after_fit_scoring(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python dashboard delivery.", encoding="utf-8")
    _validated_history(tmp_path, conn, base)
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)

    result = route_resume_for_job(
        conn,
        {
            "url": "https://careers.example.test/senior-data",
            "title": "Senior Data Analyst",
            "full_description": (
                "Requires at least five years of experience. Required: SQL. "
                "Build dashboards and reporting for business decisions."
            ),
            "eligibility_status": "eligible",
            "fit_score": 8,
        },
        profile,
    )

    assert result["job_profile"]["seniority"] == "senior_or_high_experience"
    assert result["decision"] == "reuse_exact"


def test_resume_taxonomy_extends_discovery_for_real_work_natures() -> None:
    cases = {
        "Operations Automation Engineer Intern": ("ai_implementation", "workflow_automation"),
        "Autonomous Driving Data Analysis Intern": ("data_bi_decision", "data_analytics"),
        "Map Annotation Intern": ("spatial", "map_data_operations"),
        "Autonomous Vehicle Integration & Validation Intern": (
            "spatial",
            "autonomous_vehicle_integration",
        ),
        "Map Simulation Intern": ("spatial", "spatial_simulation"),
    }

    for title, expected in cases.items():
        result = extract_job_profile(
            {
                "url": "https://example.test/" + title.replace(" ", "-"),
                "title": title,
                "full_description": "Singapore internship with Python delivery work.",
            }
        )
        assert (result["track"], result["subtype"]) == expected
        assert result["confidence"] >= 0.85


def test_resume_taxonomy_routes_analytics_and_insights_titles_before_degree_terms() -> None:
    cases = {
        "Intern, Analytics & Projects, GrabMart": ("data_bi_decision", "data_analytics"),
        "Intern, Strategy & Insights": ("data_bi_decision", "strategy_analytics"),
    }

    for title, expected in cases.items():
        result = extract_job_profile(
            {
                "url": "https://example.test/" + title.replace(" ", "-"),
                "title": title,
                "full_description": (
                    "Applicants may study Software Engineering. Required: SQL and Python."
                ),
            }
        )
        assert (result["track"], result["subtype"]) == expected
        assert result["confidence"] >= 0.79


def test_resume_taxonomy_routes_ai_platform_before_generic_platform_terms() -> None:
    result = extract_job_profile(
        {
            "url": "https://example.test/ai-platform",
            "title": "Intern, AI Platform",
            "full_description": (
                "Build data pipelines for model training, validation, and deployment."
            ),
        }
    )

    assert (result["track"], result["subtype"]) == (
        "ai_implementation",
        "ai_solutions",
    )
    assert result["confidence"] >= 0.79


def test_resume_taxonomy_prefers_title_match_over_description_only_term() -> None:
    result = extract_job_profile(
        {
            "url": "https://example.test/omnicommerce",
            "title": "Intern, OmniCommerce",
            "full_description": (
                "Support merchant operations and commercial analytics with data analysis."
            ),
        }
    )

    assert (result["track"], result["subtype"]) == (
        "general_product_consulting",
        "product_ops",
    )
    assert result["confidence"] >= 0.79


def test_resume_taxonomy_uses_specific_ml_engineer_rule_to_break_title_tie() -> None:
    result = extract_job_profile(
        {
            "url": "https://example.test/ml-ocr",
            "title": "Machine Learning Engineer / Data Scientist (OCR/CV)",
            "full_description": "Build and deploy OCR models for enterprise workflows.",
        }
    )

    assert (result["track"], result["subtype"]) == (
        "ai_implementation",
        "ai_solutions",
    )
    assert result["confidence"] >= 0.85


def test_sync_collapses_duplicate_content_and_keeps_coverage_evidence(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    _validated_history(tmp_path, conn, base, suffix="one")
    second = tmp_path / "validated-two.txt"
    second.write_text(
        "DATA ANALYST\nSQL, Python, data analysis\nBuilt a dashboard and reporting workflow.",
        encoding="utf-8",
    )
    second.with_suffix(".pdf").write_bytes(b"synthetic-pdf-binding-two")
    _insert_job(
        conn,
        url="https://careers.example.test/history-two",
        title="Data Analyst",
        description="Required: SQL. Build dashboards and reporting for business decisions.",
        tailored_resume_path=str(second),
        tailor_source_resume_path=str(base),
        tailor_report_path=str(tmp_path / "validated-two-report.json"),
        tailor_status="machine_validated",
        tailored_at="2026-08-25T00:01:00+00:00",
    )

    first = sync_resume_library(conn, _profile(base), tmp_path)
    second_sync = sync_resume_library(conn, _profile(base), tmp_path)
    status = library_status(conn)

    assert first["validated_jobs"] == 2
    assert second_sync["coverage_cells"] == 0
    assert status["artifacts"] == 2  # one base plus one deduplicated tailored content
    assert status["active_validated_artifacts"] == 1
    assert conn.execute("SELECT COUNT(*) FROM resume_coverage_cells").fetchone()[0] == 2


def test_taxonomy_v5_ignores_v4_coverage_until_sync_rebuilds_it(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    history_url = _validated_history(tmp_path, conn, base)
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)

    conn.execute(
        "UPDATE resume_coverage_cells SET taxonomy_version='resume-library-v4', "
        "track='spatial', subtype='geospatial'"
    )
    conn.execute(
        "UPDATE job_resume_profiles SET taxonomy_version='resume-library-v4', "
        "track='spatial', subtype='geospatial' WHERE job_url=?",
        (history_url,),
    )
    conn.execute("UPDATE resume_artifacts SET track='spatial' WHERE kind='tailored'")
    conn.commit()

    new_job = {
        "url": "https://careers.example.test/new-data-v5",
        "application_url": "https://ats.example.test/new-data-v5",
        "title": "Data Analyst",
        "company_name": "New Data",
        "location": "Singapore",
        "eligibility_status": "eligible",
        "full_description": "Required: SQL. Build dashboards for business decisions.",
    }
    before_rebuild = route_resume_for_job(conn, new_job, profile)
    assert before_rebuild["decision"] == "create_variant"

    rebuilt = sync_resume_library(conn, profile, tmp_path)
    assert rebuilt["coverage_cells"] == 1
    versions = {
        row["taxonomy_version"]
        for row in conn.execute(
            "SELECT taxonomy_version FROM resume_coverage_cells"
        ).fetchall()
    }
    assert versions == {"resume-library-v4", "resume-library-v5"}
    artifact = conn.execute(
        "SELECT track FROM resume_artifacts WHERE kind='tailored'"
    ).fetchone()
    assert artifact["track"] == "data_bi_decision"
    assert library_status(conn)["covered_subtypes"] == ["data_analytics"]

    after_rebuild = route_resume_for_job(conn, new_job, profile)
    assert after_rebuild["decision"] == "reuse_exact"


def test_same_subtype_reuses_current_validated_artifact(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    _validated_history(tmp_path, conn, base)
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)
    job = {
        "url": "https://careers.example.test/new-data",
        "application_url": "https://ats.example.test/new-data",
        "title": "Data Analyst",
        "company_name": "New Data",
        "location": "Singapore",
        "eligibility_status": "eligible",
        "full_description": "Required: SQL. Build dashboards and reporting for business decisions.",
    }

    result = route_resume_for_job(conn, job, profile)

    assert result["decision"] == "reuse_exact"
    assert result["required_coverage"] == 1.0
    assert result["overall_score"] >= 0.85
    assert result["artifact"]["validation_status"] == "machine_validated"


def test_stale_explicit_gpa_artifact_is_not_reused(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    _validated_history(
        tmp_path,
        conn,
        base,
        content=(
            "DATA ANALYST\nSQL, Python, data analysis\n"
            "University of Pennsylvania, Master of City Planning, GPA: 3.6\n"
            "Built a dashboard and reporting workflow."
        ),
    )
    profile = _profile(base)
    profile["education"] = [
        {
            "institution": "University of Pennsylvania",
            "gpa": "3.46/4.0",
            "gpa_may_be_disclosed": True,
        }
    ]
    sync_resume_library(conn, profile, tmp_path)

    result = route_resume_for_job(
        conn,
        {
            "url": "https://careers.example.test/new-data",
            "title": "Data Analyst",
            "eligibility_status": "eligible",
            "full_description": (
                "Required: SQL. Build dashboards and reporting for business decisions."
            ),
        },
        profile,
    )

    assert result["decision"] == "create_variant"
    assert result["candidates"] == []
    assert "conflict with current profile facts" in result["reason"]
    assert result["profile_fact_rejections"][0]["artifact_id"].startswith("resume:")
    error = result["profile_fact_rejections"][0]["errors"][0]
    assert "current profile records 3.46" in error


def test_configured_track_source_resolves_equal_artifact_scores(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "library.db")
    preferred_source = tmp_path / "preferred-source.txt"
    legacy_source = tmp_path / "legacy-source.txt"
    preferred_source.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    legacy_source.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")

    for suffix, source, content in (
        (
            "preferred",
            preferred_source,
            "DATA ANALYST\nSQL and Python\nBuilt dashboards, analytics, and reporting.",
        ),
        (
            "legacy",
            legacy_source,
            "DATA ANALYST\nPython and SQL\nDelivered analytics, dashboards, and reporting.",
        ),
    ):
        text_path = tmp_path / f"validated-{suffix}.txt"
        text_path.write_text(content, encoding="utf-8")
        text_path.with_suffix(".pdf").write_bytes(f"pdf-{suffix}".encode())
        report_path = tmp_path / f"validated-{suffix}.json"
        report_path.write_text('{"status":"machine_validated"}', encoding="utf-8")
        _insert_job(
            conn,
            url=f"https://careers.example.test/{suffix}",
            title="Data Analyst",
            description="Required: SQL. Build dashboards and reporting for business decisions.",
            tailored_resume_path=str(text_path),
            tailor_source_resume_path=str(source),
            tailor_report_path=str(report_path),
            tailor_status="machine_validated",
            tailored_at="2026-08-25T00:00:00+00:00",
        )

    profile = _profile(preferred_source)
    sync_resume_library(conn, profile, tmp_path)
    result = route_resume_for_job(
        conn,
        {
            "url": "https://careers.example.test/new-data",
            "title": "Data Analyst",
            "full_description": (
                "Required: SQL. Build dashboards and reporting for business decisions."
            ),
            "eligibility_status": "eligible",
        },
        profile,
    )

    assert result["decision"] == "reuse_exact"
    assert Path(result["artifact"]["source_resume_path"]) == preferred_source
    assert result["runner_up_margin"] == 1.0
    assert "explicitly configured source" in result["reason"]


def test_exact_unchanged_job_keeps_its_machine_validated_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    history_url = _validated_history(tmp_path, conn, base)
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)
    job = dict(conn.execute("SELECT * FROM jobs WHERE url=?", (history_url,)).fetchone())

    result = route_resume_for_job(conn, job, profile)

    assert result["decision"] == "reuse_exact"
    assert result["overall_score"] == 1.0
    artifact_path = Path(result["artifact"]["text_path"])
    assert artifact_path.parent.name == "artifacts"
    assert artifact_path.name.startswith("resume-")
    assert "Example" not in artifact_path.name
    assert conn.execute("SELECT COUNT(*) FROM resume_artifact_aliases").fetchone()[0] >= 1

    projection = project_reuse_to_job(conn, job, result)
    stored = conn.execute(
        "SELECT tailored_resume_path, tailor_report_path FROM jobs WHERE url=?",
        (history_url,),
    ).fetchone()
    assert stored["tailored_resume_path"] == str(artifact_path)
    assert Path(stored["tailor_report_path"]).parent.name == "routes"
    assert projection["artifact_id"] == result["artifact_id"]

    pdf_path = artifact_path.with_suffix(".pdf")
    pdf_before = pdf_path.read_bytes()
    monkeypatch.setattr(single_job, "get_connection", lambda: conn)
    monkeypatch.setattr(single_job.config, "APP_DIR", tmp_path)
    revalidation = single_job.revalidate_tailored_resume_for_url(history_url)
    assert revalidation["status"] == "failed_revalidation"
    assert "immutable" in revalidation["error"]
    assert pdf_path.read_bytes() == pdf_before


def test_failed_job_revalidation_deactivates_same_content_library_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    from applypilot.scoring import pdf as pdf_renderer
    from applypilot.scoring import tailor as tailor_module
    from applypilot.scoring import validator as validator_module

    database_path = tmp_path / "library.db"
    conn = init_db(database_path)
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    history_url = _validated_history(tmp_path, conn, base)
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)

    monkeypatch.setattr(single_job, "get_connection", lambda: conn)
    monkeypatch.setattr(single_job, "load_profile", lambda: profile)
    monkeypatch.setattr(
        validator_module,
        "validate_tailored_resume",
        lambda *args, **kwargs: {"passed": True, "errors": []},
    )
    monkeypatch.setattr(
        tailor_module,
        "judge_tailored_resume",
        lambda *args, **kwargs: {"passed": False, "issues": ["job mismatch"]},
    )

    revalidation = single_job.revalidate_tailored_resume_for_url(history_url)

    assert revalidation["status"] == "failed_judge"
    conn = init_db(database_path)
    artifact = conn.execute(
        "SELECT active, validation_status FROM resume_artifacts WHERE kind='tailored'"
    ).fetchone()
    assert tuple(artifact) == (0, "failed_judge")

    new_job = {
        "url": "https://careers.example.test/new-data-after-failure",
        "title": "Data Analyst",
        "full_description": (
            "Required: SQL. Build dashboards and reporting for business decisions."
        ),
        "eligibility_status": "eligible",
    }
    route = route_resume_for_job(conn, new_job, profile)

    assert route["decision"] == "create_variant"
    assert route["candidates"] == []

    restore_conn = init_db(database_path)
    monkeypatch.setattr(single_job, "get_connection", lambda: restore_conn)
    monkeypatch.setattr(
        tailor_module,
        "judge_tailored_resume",
        lambda *args, **kwargs: {"passed": True, "issues": []},
    )

    def render_to_requested_path(path, output_path=None):
        output = Path(output_path)
        output.write_bytes(b"%PDF-restored-job-binding")
        return output

    monkeypatch.setattr(pdf_renderer, "convert_to_pdf", render_to_requested_path)

    restored = single_job.revalidate_tailored_resume_for_url(history_url)

    assert restored["status"] == "machine_validated"
    verify = init_db(database_path)
    artifact = verify.execute(
        "SELECT active, validation_status FROM resume_artifacts WHERE kind='tailored'"
    ).fetchone()
    assert tuple(artifact) == (1, "machine_validated")
    restored_route = route_resume_for_job(verify, new_job, profile)
    assert restored_route["decision"] == "reuse_exact"


def test_new_subtype_and_unsupported_hard_skill_create_truthful_variants(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    _validated_history(tmp_path, conn, base)
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)

    product = route_resume_for_job(
        conn,
        {
            "url": "https://careers.example.test/product",
            "title": "Product Manager",
            "full_description": "Own the product roadmap and stakeholder requirements.",
            "eligibility_status": "eligible",
        },
        profile,
    )
    unsupported = route_resume_for_job(
        conn,
        {
            "url": "https://careers.example.test/aws-data",
            "title": "Data Analyst",
            "full_description": "Required: AWS. Build dashboards.",
            "eligibility_status": "eligible",
            "fit_score": 8,
        },
        profile,
        minimum_fit_score=7,
    )

    assert product["decision"] == "create_variant"
    assert unsupported["decision"] == "create_variant"
    assert unsupported["hard_gaps"] == ["aws"]


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Allium Engineering General / AI Intern",
            "Support engineering delivery across several product workstreams.",
        ),
        (
            "General Intern",
            "Support cross-functional projects and operational coordination.",
        ),
    ],
)
def test_passing_fit_gate_routes_unclassified_job_to_factual_base_variant(
    tmp_path: Path, title: str, description: str
) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)
    job = {
        "url": "https://careers.example.test/" + title.replace(" ", "-"),
        "title": title,
        "full_description": description,
        "eligibility_status": "eligible",
        "fit_score": 8,
    }

    result = route_resume_for_job(conn, job, profile, minimum_fit_score=7)

    assert result["job_profile"]["subtype"] is None
    assert result["decision"] == "create_variant"
    assert result["decision"] != "reuse_exact"
    components = _route_components(conn, result)
    assert components["fit_gate"] == {
        "fit_score": 8,
        "minimum_fit_score": 7,
        "passed": True,
    }
    assert components["usable_base_sources"][0]["text_path"] == str(base.resolve())
    selected_path, source_route = tailor.select_resume_source(job, profile)
    assert selected_path == base.resolve()
    assert source_route == {
        "method": "configured_default_source",
        "track": "data_bi_decision_analysis",
        "score": 0,
    }


def test_no_keyword_source_selection_skips_stale_profile_facts_but_keeps_override(
    tmp_path: Path,
) -> None:
    stale = tmp_path / "stale.txt"
    stale.write_text(
        "University of Pennsylvania, Master of City Planning, GPA: 3.6",
        encoding="utf-8",
    )
    current = tmp_path / "current.txt"
    current.write_text(
        "University of Pennsylvania, Master of City Planning, GPA: 3.46",
        encoding="utf-8",
    )
    profile = {
        "education": [
            {
                "institution": "University of Pennsylvania",
                "gpa": "3.46/4.0",
                "gpa_may_be_disclosed": True,
            }
        ],
        "tailoring": {
            "resume_variants": [
                {"track": "stale", "path": str(stale), "keywords": []},
                {"track": "current", "path": str(current), "keywords": []},
            ]
        },
    }
    job = {
        "title": "General Intern",
        "full_description": "Support cross-functional delivery.",
    }

    selected, routing = tailor.select_resume_source(job, profile)

    assert selected == current.resolve()
    assert routing == {
        "method": "configured_default_source",
        "track": "current",
        "score": 0,
    }
    explicit, explicit_routing = tailor.select_resume_source(
        {**job, "tailor_source_resume_path": str(stale)},
        profile,
    )
    assert explicit == stale.resolve()
    assert explicit_routing == {"method": "job_override", "track": "explicit", "score": None}


@pytest.mark.parametrize(("fit_score", "minimum_fit_score"), [(None, 7), (6, 7), (8, None)])
def test_unclassified_job_requires_proven_configured_fit_gate(
    tmp_path: Path, fit_score: int | None, minimum_fit_score: int | None
) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)

    result = route_resume_for_job(
        conn,
        {
            "url": f"https://careers.example.test/unclassified-{fit_score}-{minimum_fit_score}",
            "title": "General Intern",
            "full_description": "Support cross-functional projects.",
            "eligibility_status": "eligible",
            "fit_score": fit_score,
        },
        profile,
        minimum_fit_score=minimum_fit_score,
    )

    assert result["decision"] == "manual_review"


def test_tailoring_pipeline_uses_configured_base_for_passing_unclassified_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = init_db(tmp_path / "library.db")
    general_base = tmp_path / "general-base.txt"
    general_base.write_text("GENERAL FACTUAL SOURCE: SQL and Python.", encoding="utf-8")
    ai_base = tmp_path / "ai-base.txt"
    ai_base.write_text("AI FACTUAL SOURCE: Python and machine learning.", encoding="utf-8")
    profile = {
        "skills_boundary": {"languages": ["Python", "SQL"]},
        "tailoring": {
            "resume_variants": [
                {
                    "track": "general_product_consulting",
                    "path": str(general_base),
                    "keywords": ["operations"],
                },
                {
                    "track": "ai_implementation_automation",
                    "path": str(ai_base),
                    "keywords": ["ai", "machine learning"],
                },
            ]
        },
    }
    job = {
        "url": "https://careers.example.test/allium-engineering-general-ai",
        "title": "Allium Engineering General / AI Intern",
        "company_name": "Allium",
        "full_description": "Support engineering delivery across several product workstreams.",
        "eligibility_status": "eligible",
        "fit_score": 8,
    }
    _insert_job(
        conn,
        url=str(job["url"]),
        title=str(job["title"]),
        description=str(job["full_description"]),
        company_name="Allium",
        fit_score=8,
    )
    output_dir = tmp_path / "tailored"
    used_sources: list[str] = []

    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "load_profile", lambda: profile)
    monkeypatch.setattr(tailor, "get_jobs_by_stage", lambda **_kwargs: [job])
    monkeypatch.setattr(tailor, "TAILORED_DIR", output_dir)

    def fake_tailor(resume_text, *_args, **_kwargs):
        used_sources.append(resume_text)
        return "Validated factual variant.", {"status": "machine_validated", "attempts": 1}

    monkeypatch.setattr(tailor, "tailor_resume", fake_tailor)

    from applypilot.scoring import pdf as scoring_pdf

    def fake_pdf(text_path: Path) -> Path:
        pdf_path = text_path.with_suffix(".pdf")
        pdf_path.write_bytes(b"synthetic-pdf")
        return pdf_path

    monkeypatch.setattr(scoring_pdf, "convert_to_pdf", fake_pdf)
    monkeypatch.setattr(
        resume_library,
        "register_tailored_artifact",
        lambda *args, **kwargs: {"artifact_id": "artifact-allium"},
    )

    result = tailor.run_tailoring(min_score=7, limit=1, validation_mode="strict")

    assert used_sources == ["AI FACTUAL SOURCE: Python and machine learning."]
    assert result["results"][0]["resume_library_decision"] == "create_variant"
    assert result["results"][0]["source_resume_path"] == str(ai_base.resolve())
    assert result["results"][0]["status"] == "machine_validated"


def _unsupported_aws_job(suffix: str) -> dict[str, str]:
    return {
        "url": f"https://careers.example.test/aws-data-{suffix}",
        "title": "Data Analyst",
        "full_description": "Required: AWS. Build dashboards and reporting.",
        "eligibility_status": "eligible",
    }


def _route_components(conn, route: dict[str, object]) -> dict[str, object]:
    row = conn.execute(
        "SELECT components_json FROM job_resume_assignments WHERE assignment_id=?",
        (route["assignment_id"],),
    ).fetchone()
    return json.loads(row["components_json"])


def test_confirmed_skill_experience_allows_variant_without_claiming_resume_coverage(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    _validated_history(tmp_path, conn, base)
    profile = _profile(base)
    profile["application_facts"] = [
        {
            "key": "aws_experience_years",
            "value": 1,
            "source": "user_confirmed",
        }
    ]
    sync_resume_library(conn, profile, tmp_path)

    result = route_resume_for_job(conn, _unsupported_aws_job("confirmed"), profile)

    components = _route_components(conn, result)
    assert result["decision"] == "create_variant"
    assert result["decision"] != "reuse_exact"
    assert result["hard_gaps"] == ["aws"]
    assert components["unsupported_required_skills"] == []
    assert components["confirmed_required_skill_facts"] == [
        {
            "confirmed_at": "",
            "fact_key": "aws_experience_years",
            "skill": "aws",
            "source": "user_confirmed",
            "value": 1,
        }
    ]
    assert result["candidates"][0]["required_coverage"] == 0.0
    assert result["candidates"][0]["missing_required"] == ["aws"]


@pytest.mark.parametrize(
    "fact",
    [
        {"key": "aws_experience_years", "value": 1, "source": "inferred"},
        {"key": "aws_experience_years", "value": 0, "source": "user_confirmed"},
        {"key": "aws_experience_years", "value": -1, "source": "user_confirmed"},
        {"key": "aws_experience_years", "value": True, "source": "user_confirmed"},
        {
            "key": "talkwalker_experience_years",
            "value": 1,
            "source": "user_confirmed",
        },
        {"key": "aws_skill_level", "value": 1, "source": "user_confirmed"},
    ],
    ids=["unconfirmed", "zero", "negative", "boolean", "unknown-skill", "wrong-key"],
)
def test_invalid_skill_experience_facts_do_not_claim_unsupported_gap_coverage(
    tmp_path: Path, fact: dict[str, object]
) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    _validated_history(tmp_path, conn, base)
    profile = _profile(base)
    profile["application_facts"] = [fact]
    sync_resume_library(conn, profile, tmp_path)

    result = route_resume_for_job(
        conn,
        {**_unsupported_aws_job("invalid-fact"), "fit_score": 8},
        profile,
        minimum_fit_score=7,
    )

    assert result["decision"] == "create_variant"
    assert result["hard_gaps"] == ["aws"]
    components = _route_components(conn, result)
    assert components["unsupported_required_skills"] == ["aws"]
    assert components["confirmed_required_skill_facts"] == []


def test_manual_selection_uses_confirmed_skill_experience_for_unsupported_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    _validated_history(tmp_path, conn, base, suffix="one")
    _validated_history(
        tmp_path,
        conn,
        base,
        suffix="two",
        content=(
            "DATA ANALYST\nSQL, Python, data analysis\n"
            "Built a dashboard and reporting workflow. Additional verified detail."
        ),
    )
    profile = _profile(base)
    profile["application_facts"] = [
        {
            "key": "aws_experience_years",
            "value": 1,
            "source": "user_confirmed",
        }
    ]
    sync_resume_library(conn, profile, tmp_path)
    monkeypatch.setattr(resume_library, "REUSE_REQUIRED_COVERAGE", 0.0)
    monkeypatch.setattr(resume_library, "REUSE_OVERALL_SCORE", 0.0)
    job = {**_unsupported_aws_job("confirmed-manual-selection"), "fit_score": 8}

    automatic = route_resume_for_job(conn, job, profile, minimum_fit_score=7)
    automatic_components = _route_components(conn, automatic)
    selected = route_resume_for_job(
        conn,
        job,
        profile,
        artifact_id=automatic["candidates"][0]["artifact_id"],
        minimum_fit_score=7,
    )

    assert automatic["decision"] == "create_variant"
    assert automatic_components["unsupported_required_skills"] == []
    assert automatic_components["confirmed_required_skill_facts"][0]["fact_key"] == (
        "aws_experience_years"
    )
    assert selected["decision"] == "manual_selection"
    assert selected["decision"] != "reuse_exact"
    assert selected["hard_gaps"] == ["aws"]
    selected_components = _route_components(conn, selected)
    assert selected_components["unsupported_required_skills"] == []
    assert selected_components["confirmed_required_skill_facts"][0]["source"] == (
        "user_confirmed"
    )


def test_manual_selection_resolves_only_a_current_qualified_candidate_tie(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    _validated_history(tmp_path, conn, base, suffix="one")
    _validated_history(
        tmp_path,
        conn,
        base,
        suffix="two",
        content=(
            "DATA ANALYST\nSQL, Python, data analysis\n"
            "Built a dashboard and reporting workflow.\nAdditional verified detail."
        ),
    )
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)
    job = {
        "url": "https://careers.example.test/tied-data",
        "title": "Data Analyst",
        "full_description": (
            "Required: SQL. Build dashboards and reporting for business decisions."
        ),
        "eligibility_status": "eligible",
        "fit_score": 8,
    }
    _insert_job(
        conn,
        url=str(job["url"]),
        title=str(job["title"]),
        description=str(job["full_description"]),
    )

    unresolved = route_resume_for_job(conn, job, profile, minimum_fit_score=7)

    assert unresolved["decision"] == "create_variant"
    assert len(unresolved["candidates"]) == 2
    selected_artifact_id = unresolved["candidates"][1]["artifact_id"]

    selected = route_resume_for_job(
        conn,
        job,
        profile,
        artifact_id=selected_artifact_id,
        minimum_fit_score=7,
    )

    assert selected["decision"] == "manual_selection"
    assert selected["artifact_id"] == selected_artifact_id
    assert Path(selected["reuse_report_path"]).is_file()
    assert project_reuse_to_job(conn, job, selected)["artifact_id"] == selected_artifact_id
    validation = conn.execute(
        """
        SELECT validation_kind, status FROM resume_validation_runs
        WHERE artifact_id=? ORDER BY recorded_at DESC LIMIT 1
        """,
        (selected_artifact_id,),
    ).fetchone()
    assert dict(validation) == {
        "validation_kind": "manual_selection_route_binding",
        "status": "machine_validated",
    }

    unsupported_job = {
        **job,
        "url": "https://careers.example.test/tied-unsupported",
        "full_description": "Required: AWS. Build dashboards.",
    }
    with pytest.raises(ValueError, match="cannot resolve"):
        route_resume_for_job(
            conn,
            unsupported_job,
            profile,
            artifact_id=selected_artifact_id,
            minimum_fit_score=7,
        )

    with pytest.raises(ValueError, match="candidate"):
        route_resume_for_job(
            conn,
            job,
            profile,
            artifact_id="resume:not-a-candidate",
            minimum_fit_score=7,
        )

    selected_pdf = Path(selected["artifact"]["pdf_path"])
    selected_pdf.write_bytes(b"changed-after-selection")
    with pytest.raises(ValueError, match="candidate"):
        route_resume_for_job(
            conn,
            job,
            profile,
            artifact_id=selected_artifact_id,
            minimum_fit_score=7,
        )


def test_changed_pdf_binding_is_not_reused(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    _validated_history(tmp_path, conn, base)
    profile = _profile(base)
    sync_resume_library(conn, profile, tmp_path)
    artifact = conn.execute(
        "SELECT pdf_path FROM resume_artifacts WHERE validation_status='machine_validated'"
    ).fetchone()
    Path(artifact["pdf_path"]).write_bytes(b"changed-after-validation")

    result = route_resume_for_job(
        conn,
        {
            "url": "https://careers.example.test/new-data",
            "title": "Data Analyst",
            "full_description": "Required: SQL. Build dashboards.",
            "eligibility_status": "eligible",
        },
        profile,
    )

    assert result["decision"] == "create_variant"


def test_tailoring_pipeline_reuses_without_calling_llm(tmp_path: Path, monkeypatch) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    _validated_history(tmp_path, conn, base)
    new_url = "https://careers.example.test/new-data"
    _insert_job(
        conn,
        url=new_url,
        title="Data Analyst",
        description="Required: SQL. Build dashboards and reporting for business decisions.",
        fit_score=8,
    )
    profile = _profile(base)
    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "load_profile", lambda: profile)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("LLM tailoring must not run for an exact reusable subtype")

    monkeypatch.setattr(tailor, "tailor_resume", fail_if_called)

    result = tailor.run_tailoring(
        min_score=0,
        limit=1,
        validation_mode="strict",
        target_url=new_url,
    )
    stored = conn.execute(
        "SELECT tailored_resume_path, tailor_status, tailor_attempts FROM jobs WHERE url=?",
        (new_url,),
    ).fetchone()

    assert result["approved"] == 1
    assert result["results"][0]["resume_library_decision"] == "reuse_exact"
    assert stored["tailor_status"] == "machine_validated"
    assert stored["tailor_attempts"] == 0


def test_exact_target_reevaluates_existing_material_and_creates_variant(
    tmp_path: Path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    previous = tmp_path / "previous.txt"
    previous.write_text("Previous material with stale facts.", encoding="utf-8")
    target_url = "https://careers.example.test/exact-refresh"
    _insert_job(
        conn,
        url=target_url,
        title="Data Analyst",
        description="Required: SQL. Build dashboards and reporting for business decisions.",
        fit_score=8,
        tailored_resume_path=str(previous),
        tailor_status="machine_validated",
    )
    profile = _profile(base)
    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "load_profile", lambda: profile)
    monkeypatch.setattr(tailor, "TAILORED_DIR", tmp_path / "tailored")
    monkeypatch.setattr(resume_library, "sync_resume_library", lambda *args, **kwargs: None)

    routed: list[str] = []

    def route_existing(_conn, job, _profile, **_kwargs):
        routed.append(job["tailored_resume_path"])
        return {
            "decision": "create_variant",
            "assignment_id": "assignment-refresh",
            "reason": "Existing material conflicts with current facts.",
        }

    monkeypatch.setattr(resume_library, "route_resume_for_job", route_existing)
    monkeypatch.setattr(tailor, "select_resume_source", lambda *args: (base, {"track": "data"}))
    monkeypatch.setattr(tailor, "read_resume_source", lambda path: Path(path).read_text())
    monkeypatch.setattr(
        tailor,
        "tailor_resume",
        lambda *args, **kwargs: (
            "DATA ANALYST\nSQL and Python dashboard delivery.",
            {"status": "machine_validated", "attempts": 1},
        ),
    )

    from applypilot.scoring import pdf as scoring_pdf

    def fake_pdf(text_path: Path) -> Path:
        pdf_path = text_path.with_suffix(".pdf")
        pdf_path.write_bytes(b"synthetic-pdf")
        return pdf_path

    monkeypatch.setattr(scoring_pdf, "convert_to_pdf", fake_pdf)
    monkeypatch.setattr(
        resume_library,
        "register_tailored_artifact",
        lambda *args, **kwargs: {"artifact_id": "artifact-refresh"},
    )

    result = tailor.run_tailoring(
        min_score=0,
        limit=1,
        validation_mode="strict",
        target_url=target_url,
    )
    stored = conn.execute(
        "SELECT tailored_resume_path, tailor_status, tailor_attempts FROM jobs WHERE url=?",
        (target_url,),
    ).fetchone()

    assert routed == [str(previous)]
    assert result["approved"] == 1
    assert result["results"][0]["resume_library_decision"] == "create_variant"
    assert stored["tailored_resume_path"] != str(previous)
    assert stored["tailor_status"] == "machine_validated"
    assert stored["tailor_attempts"] == 1


def test_batch_tailoring_still_skips_jobs_with_existing_material(
    tmp_path: Path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "library.db")
    base = tmp_path / "base.txt"
    base.write_text("MASTER SOURCE: SQL and Python.", encoding="utf-8")
    previous = tmp_path / "previous.txt"
    previous.write_text("Already tailored.", encoding="utf-8")
    _insert_job(
        conn,
        url="https://careers.example.test/batch-existing",
        title="Data Analyst",
        description="Required: SQL. Build dashboards and reporting for business decisions.",
        fit_score=8,
        tailored_resume_path=str(previous),
        tailor_status="machine_validated",
    )
    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "load_profile", lambda: _profile(base))
    monkeypatch.setattr(resume_library, "sync_resume_library", lambda *args, **kwargs: None)

    def fail_if_routed(*args, **kwargs):
        raise AssertionError("batch pending-tailor selection must keep filtering existing material")

    monkeypatch.setattr(resume_library, "route_resume_for_job", fail_if_routed)

    result = tailor.run_tailoring(min_score=0, limit=1, validation_mode="strict")

    assert result == {
        "approved": 0,
        "failed": 0,
        "errors": 0,
        "elapsed": 0.0,
    }
