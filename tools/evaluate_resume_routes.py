"""Replay saved and controlled JDs against a read-only library snapshot.

No LLM requests, live assignments, application updates or resume writes.
Each run keeps full candidate explanations for human inspection.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

from applypilot import resume_library as library


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--score-repeat", type=int, default=0,
                        help="Opt in to real configured LLM scoring of six cases, repeated N times.")
    parser.add_argument("--score-results", type=Path,
                        help="Replay previously recorded real assessments through the route gate.")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect((workspace / "applypilot.db").as_uri() + "?mode=ro", uri=True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    source.backup(conn)
    source.close()
    profile = json.loads((workspace / "profile.json").read_text(encoding="utf-8"))
    catalogue = json.loads((workspace / "resume-library/catalog.json").read_text(encoding="utf-8"))
    labels = {item["artifact_id"]: item for item in catalogue["artifacts"]}
    jobs = [dict(row) for row in conn.execute(
        "SELECT * FROM jobs WHERE length(full_description)>400 "
        "ORDER BY CASE WHEN tailored_resume_path IS NOT NULL THEN 0 ELSE 1 END, rowid DESC LIMIT 60"
    )]
    cases = [{"case": f"saved-{i:02}", "kind": "saved_jd", "job": job}
             for i, job in enumerate(jobs, 1)]
    scenarios = [
        ("ai_api", "AI Application Engineering Intern", "Required: Python, REST and OpenAPI. Build LLM applications, retrieval workflows, tool calling and API integrations. Test retries and deliver working prototypes to clients."),
        ("ai_research", "AI Research Intern", "Required: Python and machine learning. Evaluate models with cross-validation, held-out datasets and reproducible experiments. Compare generalization across events and document limitations."),
        ("data", "Data Analyst Intern", "Required: SQL and Python. Clean datasets, perform data analysis, build dashboards and communicate findings to business stakeholders."),
        ("product", "AI Product Management Intern", "Work with clients to identify requirements, design user workflows, coordinate frontend and API integration, conduct user testing and translate feedback into product improvements."),
        ("spatial", "Geospatial Analytics Intern", "Required: Python and GeoPandas. Analyze transport accessibility, spatial data and site selection. Build maps and compare policy scenarios."),
        ("qa", "Software Quality Assurance Intern", "Test web applications and APIs, create regression tests, reproduce bugs, verify error handling and maintain test documentation."),
        ("unknown_skill", "AI Engineering Intern", "Required: CUDA, TensorRT and Kubernetes. Implement distributed GPU training and production inference infrastructure."),
        ("missing_jd", "Data Analyst Intern", ""),
        ("nonmatching", "Clinical Nursing Intern", "Required: registered nursing qualification. Provide bedside patient care and administer medication in the intensive care unit."),
        ("alternatives", "Data Analyst Intern", "Required: Python or R. Perform data analysis and report findings. Tableau is preferred, not required."),
    ]
    for name, title, description in scenarios:
        variants = [("normal", {})]
        if name in {"ai_api", "data", "product"}:
            variants += [("one_page", {"full_description": description + " Submit a one-page resume."}),
                         ("two_page", {"full_description": description + " Submit a two-page resume."}),
                         ("low_fit", {"fit_score": 2}),
                         ("failed_score", {"fit_score": 9, "score_status": "failed"})]
        if name == "unknown_skill":
            variants += [("high_fit", {"fit_score": 9})]
        for variant, changes in variants:
            job = {"url": f"https://evaluation.invalid/{name}/{variant}", "title": title,
                   "full_description": description, "eligibility_status": "eligible", **changes}
            cases.append({"case": f"{name}-{variant}", "kind": "controlled_jd", "job": job})
    holdouts = [
        ("research_scored", "AI Research Intern", "Required: Python and machine learning. Evaluate generalization with train-fold preprocessing, leave-one-event-out validation and reproducible experiments.", {"fit_score": 8}),
        ("quality", "Software Quality Assurance Intern", "Test frontend failure and retry paths, typed API contracts, regression tests and CI workflows.", {"fit_score": 8}),
        ("market", "Market Data Intern", "Required: SQL and Python. Validate data freshness, data contracts and public-data dashboard reporting.", {"fit_score": 8}),
        ("product_cv_cap", "Product Management Intern", "Collect client requirements, prototype user flows and conduct user trials. CV limited to 2 pages.", {"fit_score": 8}),
        ("data_cv_cap", "Data Analyst Intern", "Required: SQL. Build dashboards. Resume must be no more than one page.", {"fit_score": 8}),
        ("optional_gpu", "AI Application Engineering Intern", "Required: Python. Build retrieval workflows and OpenAPI integrations. CUDA and Kubernetes are optional.", {"fit_score": 8}),
        ("mandatory_gpu_low", "AI Application Engineering Intern", "Required: CUDA and Kubernetes for distributed inference.", {"fit_score": 2}),
        ("ineligible", "Data Analyst Intern", "Required: SQL and Python.", {"fit_score": 9, "eligibility_status": "ineligible"}),
        ("missing_scored", "Product Management Intern", "", {"fit_score": 9}),
        ("report_not_resume", "Data Analyst Intern", "Required: SQL. Build dashboards and write a two-page report.", {"fit_score": 8}),
        ("alternative_r", "Data Analyst Intern", "Required: Python or R. Analyze public datasets and deliver dashboards.", {"fit_score": 8}),
        ("changed_status", "AI Engineer Intern", "Required: Python. Implement RAG tools and API integrations.", {"fit_score": 9, "score_status": "stale"}),
    ]
    for name, title, description, changes in holdouts:
        cases.append({"case": "holdout-" + name, "kind": "holdout_jd", "job": {
            "url": f"https://evaluation.invalid/holdout/{name}", "title": title,
            "full_description": description, "eligibility_status": "eligible", **changes,
        }})
    results = []
    cached_read = lru_cache(maxsize=256)(library.read_resume_source)
    # Only the route report destination is redirected; health and scoring use
    # the real registered artifacts, unchanged, against the memory DB.
    with patch.object(library, "library_root", return_value=output / "isolated-library"), \
            patch.object(library, "read_resume_source", cached_read):
        for case in cases:
            result = library.route_resume_for_job(conn, case["job"], profile, minimum_fit_score=6)
            artifact = result.pop("artifact", {})
            result["selected_label"] = labels.get(result.get("artifact_id"), {})
            result["selected_kind"] = artifact.get("kind")
            for candidate in result["candidates"]:
                candidate["label"] = labels.get(candidate["artifact_id"], {}).get("label")
                candidate["pages"] = labels.get(candidate["artifact_id"], {}).get("pages")
            results.append({**case, "result": result})
        if args.score_results:
            from applypilot.scoring.scorer import build_score_input_binding
            scored_routes = []
            for assessment in json.loads(args.score_results.read_text(encoding="utf-8")):
                original = next(row for row in results if row["case"] == assessment["case"])
                job = {**original["job"], "fit_score": assessment["score"]["score"], "score_status": "scored"}
                selected = labels[assessment["artifact_id"]]
                source_path = Path(selected["text_path"])
                binding = build_score_input_binding(job, source_path, source_path.read_text(encoding="utf-8"))
                binding["prompt_revision"] = assessment["score"].get("prompt_revision")
                job["score_evidence_json"] = json.dumps({"input_binding": binding})
                route = library.route_resume_for_job(conn, job, profile, minimum_fit_score=6)
                scored_routes.append({"case": assessment["case"], "repeat": assessment["repeat"],
                                      "fit_score": job["fit_score"], "route": route})
            (output / "scored-routes.json").write_text(
                json.dumps(scored_routes, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.close()
    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"cases": len(results), "decisions": dict(Counter(r["result"]["decision"] for r in results)),
               "resolutions": dict(Counter(str(r["result"]["resolution"]) for r in results))}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    for row in results:
        r = row["result"]
        print(row["case"], row["job"]["title"], r["resolution"], r["overall_score"],
              r["selected_label"].get("label"), r["selected_label"].get("pages"), r["hard_gaps"])
    if args.score_repeat:
        from applypilot.config import load_env
        from applypilot.scoring.scorer import score_job_with_review
        load_env()
        scored = []
        selected_cases = {"saved-01", "saved-55", "ai_api-normal", "data-normal",
                          "product-normal", "unknown_skill-high_fit"}
        for repeat in range(args.score_repeat):
            for row in results:
                if row["case"] not in selected_cases:
                    continue
                selected = row["result"]["selected_label"]
                if not selected:
                    continue
                score = score_job_with_review(
                    Path(selected["text_path"]).read_text(encoding="utf-8"), row["job"],
                    profile=profile, review_allowed=False,
                )
                scored.append({"case": row["case"], "repeat": repeat + 1,
                               "artifact_id": selected["artifact_id"], "score": score})
                (output / "live-scores.json").write_text(
                    json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
                print("LIVE_SCORE", row["case"], repeat + 1, score["score"], flush=True)


if __name__ == "__main__":
    main()
