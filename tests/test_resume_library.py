"""Contracts for content-addressed resume reuse and append-only provenance."""

from __future__ import annotations

from pathlib import Path

from applypilot import single_job
from applypilot.database import init_db
from applypilot.resume_library import (
    extract_job_profile,
    library_status,
    project_reuse_to_job,
    route_resume_for_job,
    sync_resume_library,
)
from applypilot.scoring import tailor


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


def _validated_history(tmp_path: Path, conn, base_resume: Path, *, suffix: str = "one") -> str:
    text_path = tmp_path / f"validated-{suffix}.txt"
    text_path.write_text(
        "DATA ANALYST\nSQL, Python, data analysis\nBuilt a dashboard and reporting workflow.",
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


def test_new_subtype_creates_variant_and_unsupported_hard_skill_needs_review(
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
        },
        profile,
    )

    assert product["decision"] == "create_variant"
    assert unsupported["decision"] == "manual_review"
    assert unsupported["hard_gaps"] == ["aws"]


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
