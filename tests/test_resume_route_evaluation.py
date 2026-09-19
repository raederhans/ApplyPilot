"""JD counterexamples found during the curated-library replay."""
import json
from pathlib import Path

import pytest

from applypilot import resume_library as library
from applypilot.database import init_db


@pytest.mark.parametrize("score,status", [(2, "scored"), (9, "failed"),
                                          (None, "failed"), (None, "stale"), (None, "scored"),
                                          (11, "scored"), (float("nan"), "scored"), (True, "scored")])
def test_unusable_fit_never_routes_by_confident_title(tmp_path, score, status):
    conn = init_db(tmp_path / "routes.db")
    result = library.route_resume_for_job(conn, {
        "url": "https://test.invalid/fit", "title": "Data Analyst Intern",
        "full_description": "Required: SQL. Build dashboards.",
        "fit_score": score, "score_status": status,
    }, {}, minimum_fit_score=6)
    assert result["resolution"] is None
    assert result["decision"] == "manual_review"


@pytest.mark.parametrize("title,track,subtype", [
    ("AI Application Engineering Intern", "ai_implementation", "ai_solutions"),
    ("AI Research Intern", "ai_implementation", "ai_research"),
    ("Geospatial Analytics Intern", "spatial", "geospatial"),
    ("Intern - Data Services, Market Data", "data_bi_decision", "data_analytics"),
])
def test_specific_title_is_not_overridden_by_generic_analytics(title, track, subtype):
    result = library.extract_job_profile({"title": title, "full_description": "Analyze data."})
    assert (result["track"], result["subtype"]) == (track, subtype)


@pytest.mark.parametrize("description,pages", [
    ("Submit a one-page resume.", 1), ("Submit a two page CV.", 2),
    ("CV limited to 2 pages.", None), ("Write a one-page project report.", None),
])
def test_page_requirement_is_about_resume_only(description, pages):
    assert library._requested_resume_pages(description) == pages


def test_page_maximum_does_not_force_two_pages():
    assert library._maximum_resume_pages("CV limited to 2 pages.") == 2
    assert library._maximum_resume_pages("Resume must be no more than one page.") == 1
    assert library._maximum_resume_pages("Write a two page report.") is None


def test_unsupported_gpu_stack_is_not_silently_absent():
    result = library.extract_job_profile({
        "title": "AI Engineer Intern",
        "full_description": "Required: CUDA, TensorRT and Kubernetes. PyTorch preferred.",
    })
    assert result["required_skills"] == ["cuda", "kubernetes", "tensorrt"]
    assert result["preferred_skills"] == ["pytorch"]


def test_content_tokens_do_not_include_sentence_punctuation():
    terms = library._content_terms("AI Intern", "OpenAPI. Integrations. Workflows.")
    assert "openapi" in terms and "openapi." not in terms
    assert library._evidence_contains("built integrations and workflows", "integration", inflections=True)
    assert not library._evidence_contains("research", "r", inflections=True)


def test_curated_family_beats_inherited_multi_track_for_product(tmp_path: Path):
    text = tmp_path / "resume.txt"
    text.write_text("Built product requirements and API integration for clients.", encoding="utf-8")
    profile = library.extract_job_profile({
        "title": "AI Product Management Intern",
        "full_description": "Build product requirements and API integration for clients.",
    })
    artifact = {"artifact_id": "test", "text_path": str(text), "track": "multi_track",
                "kind": "tailored", "validation_status": "machine_validated"}
    scores = []
    for family in ["产品与业务运营", "AI 工程与自动化"]:
        artifact["metadata_json"] = json.dumps({"library_family": family})
        scores.append(library._candidate_score(profile, artifact)["overall_score"])
    assert scores[0] > scores[1]


@pytest.mark.parametrize("pages", [1, 2])
def test_route_filters_wrong_page_count_before_ranking(tmp_path, monkeypatch, pages):
    # Exercise the real routing filter while isolating immutable-render setup.
    conn = init_db(tmp_path / "routes.db")
    library.ensure_resume_library_schema(conn)
    for count in (1, 2):
        path = tmp_path / f"resume-{count}.txt"
        path.write_text("Data Analyst SQL dashboards reporting", encoding="utf-8")
        conn.execute("""INSERT INTO resume_artifacts
            (artifact_id,content_sha256,kind,track,text_path,validation_status,active,
             metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,'source_only',1,?,'now','now')""",
            (str(count), str(count), "base", "data_bi_decision", str(path), json.dumps({"page_count": count})))
    result = library.route_resume_for_job(conn, {
        "url": "https://test.invalid/pages", "title": "Data Analyst",
        "full_description": f"Required: SQL. Submit a {pages}-page resume.",
    }, {})
    assert result["candidates"]
    assert {c["artifact_id"] for c in result["candidates"]} == {str(pages)}


@pytest.mark.parametrize("change", ["job", "source", "missing_source"])
def test_route_rejects_stale_bound_score(tmp_path, change):
    from applypilot.scoring.scorer import build_score_input_binding
    source = tmp_path / "source.txt"
    source.write_text("SQL and Python", encoding="utf-8")
    job = {"url": "https://test.invalid/binding", "title": "Data Analyst",
           "full_description": "Required: SQL.", "fit_score": 9, "score_status": "scored"}
    job["score_evidence_json"] = json.dumps({
        "input_binding": build_score_input_binding(job, source, source.read_text(encoding="utf-8"))})
    if change == "job":
        job["full_description"] += " AWS is required."
    elif change == "source":
        source.write_text("Unrelated content", encoding="utf-8")
    else:
        source.unlink()
    result = library.route_resume_for_job(init_db(tmp_path / "jobs.db"), job, {}, minimum_fit_score=6)
    assert result["resolution"] is None
    assert "rescore" in result["reason"]


def test_exact_tailor_cli_uses_profile_floor(monkeypatch):
    from applypilot import cli, config
    from applypilot.scoring import tailor
    seen = {}
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(config, "load_profile", lambda: {"submission_policy": {"minimum_fit_score": 8}})
    monkeypatch.setattr(tailor, "run_tailoring", lambda **kw: seen.update(kw) or {})
    cli.tailor_job_command(url="https://test.invalid/exact", validation="strict")
    assert seen["min_score"] == 8


@pytest.mark.parametrize("floor,expected", [(6, 2), (8, 1)])
def test_pipeline_counts_use_the_same_configured_floor(tmp_path, monkeypatch, floor, expected):
    from applypilot import config, eligibility
    from applypilot.storage.job_stats import get_stats
    conn = init_db(tmp_path / "stats.db")
    monkeypatch.setattr(eligibility, "refresh_job_eligibility", lambda *_: None)
    monkeypatch.setattr(config, "load_profile", lambda: {"submission_policy": {"minimum_fit_score": floor}})
    for score in [5, 6, 8]:
        conn.execute("INSERT INTO jobs (url,title,full_description,fit_score,eligibility_status) VALUES (?,?,?,?,?)",
                     (f"https://test.invalid/{score}", "Data Analyst", "SQL analysis", score, "eligible"))
    assert get_stats(conn)["untailored_eligible"] == expected
