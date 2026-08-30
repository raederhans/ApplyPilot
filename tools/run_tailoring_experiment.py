"""Run supervised, isolated LLM resume-tailoring experiments on real jobs.

This tool reads jobs from an existing ApplyPilot database, routes each job to
one explicitly registered resume variant, and never launches a browser or
submits an application. Rejected drafts are retained only as audit evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from applypilot import config
from applypilot.scoring.cover_letter import read_resume_source
from applypilot.scoring.tailor import select_resume_source, tailor_resume

SCENARIOS = [
    {
        "name": "ai_implementation",
        "track": "ai_implementation_automation",
        "url": "https://www.linkedin.com/jobs/view/4443923172",
    },
    {
        "name": "data_bi",
        "track": "data_bi_decision_analysis",
        "url": "https://sg.indeed.com/viewjob?jk=8662f811ac1a8eb5",
    },
    {
        "name": "product_consulting",
        "track": "general_product_consulting",
        "url": "https://www.linkedin.com/jobs/view/4414710793",
    },
    {
        "name": "spatial_ai",
        "track": "ai_implementation_automation",
        "url": "https://www.linkedin.com/jobs/view/4453216373",
    },
]

STOPWORDS = {
    "about", "after", "also", "and", "are", "but", "for", "from", "have",
    "into", "its", "job", "our", "role", "that", "the", "their", "this",
    "through", "with", "will", "work", "you", "your",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--max-retries", type=int, default=0)
    parser.add_argument("--validation", choices=("strict", "normal"), default="strict")
    parser.add_argument("--only", action="append", choices=[item["name"] for item in SCENARIOS])
    return parser.parse_args()


def _safe_name(value: str) -> str:
    return re.sub(r"[^\w\s-]", "", value)[:70].strip().replace(" ", "_")


def _jd_overlap(text: str, job_description: str) -> dict:
    jd_tokens = {
        token
        for token in re.findall(r"[a-z0-9+#.]+", job_description.casefold())
        if len(token) >= 4 and token not in STOPWORDS
    }
    resume_tokens = set(re.findall(r"[a-z0-9+#.]+", text.casefold()))
    shared = sorted(jd_tokens & resume_tokens)
    return {
        "matched_unique_tokens": len(shared),
        "jd_unique_tokens": len(jd_tokens),
        "coverage": round(len(shared) / len(jd_tokens), 4) if jd_tokens else 0.0,
        "sample": shared[:30],
    }


def _load_jobs(source_db: Path) -> dict[str, dict]:
    uri = f"file:{source_db.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        urls = [item["url"] for item in SCENARIOS]
        placeholders = ",".join("?" for _ in urls)
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE url IN ({placeholders})",
            urls,
        ).fetchall()
    return {row["url"]: dict(row) for row in rows}


def main() -> int:
    args = _parse_args()
    if "data-experiments" not in str(config.APP_DIR).casefold():
        raise RuntimeError("Refusing tailoring experiment outside data-experiments.")
    config.load_env()
    config.ensure_dirs()
    profile = config.load_profile()
    jobs = _load_jobs(args.source_db)
    selected = [item for item in SCENARIOS if not args.only or item["name"] in args.only]

    started = datetime.now(UTC)
    run_id = started.strftime("%Y%m%d-%H%M%S")
    results: list[dict] = []
    for index, scenario in enumerate(selected, start=1):
        job = jobs.get(scenario["url"])
        if job is None:
            results.append({**scenario, "status": "missing_job"})
            continue
        if job.get("eligibility_status") == "ineligible":
            results.append({**scenario, "status": "excluded_ineligible"})
            continue
        source_path, routing = select_resume_source(job, profile)
        if routing.get("track") != scenario["track"]:
            results.append({
                **scenario,
                "status": "route_mismatch",
                "actual_routing": routing,
                "source_resume_path": str(source_path),
            })
            continue
        if not source_path.exists():
            results.append({**scenario, "status": "missing_resume_variant"})
            continue

        print(json.dumps({
            "event": "scenario_started",
            "index": index,
            "total": len(selected),
            "scenario": scenario["name"],
            "job": job.get("title"),
            "company": job.get("company_name"),
            "track": scenario["track"],
        }, ensure_ascii=False), flush=True)
        source_text = read_resume_source(source_path)
        scenario_started = time.perf_counter()
        tailored_text, report = tailor_resume(
            source_text,
            job,
            profile,
            max_retries=max(0, args.max_retries),
            validation_mode=args.validation,
            source_resume_path=str(source_path),
        )
        report["resume_routing"] = routing

        prefix = f"{scenario['name']}_{_safe_name(job.get('company_name') or '')}_{_safe_name(job.get('title') or '')}"
        report_path = config.LOG_DIR / f"{prefix}_{run_id}_REPORT.json"
        success = report.get("status") == "machine_validated"
        validated_text_path = config.TAILORED_DIR / f"{prefix}.txt"
        if success:
            text_path = validated_text_path
        else:
            rejected_dir = config.LOG_DIR / "rejected"
            rejected_dir.mkdir(parents=True, exist_ok=True)
            text_path = rejected_dir / f"{prefix}_{run_id}_REJECTED.txt"
            if validated_text_path.exists():
                stale_path = rejected_dir / (
                    f"{prefix}_PREVIOUSLY_VALIDATED_"
                    f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.txt"
                )
                validated_text_path.replace(stale_path)
                report["quarantined_previous_path"] = str(stale_path)
        if tailored_text:
            text_path.write_text(tailored_text, encoding="utf-8")

        supervised = {
            "source_words": len(source_text.split()),
            "tailored_words": len(tailored_text.split()),
            "compression_ratio": round(
                len(tailored_text.split()) / max(1, len(source_text.split())), 4
            ),
            "jd_overlap_before": _jd_overlap(source_text, job.get("full_description") or ""),
            "jd_overlap_after": _jd_overlap(tailored_text, job.get("full_description") or ""),
            "evidence_mappings": len(report.get("evidence_map") or []),
            "validator_passed": bool((report.get("validator") or {}).get("passed")),
            "full_validator_passed": bool((report.get("full_validator") or {}).get("passed")),
            "judge_passed": bool((report.get("judge") or {}).get("passed")),
        }
        report["supervised_metrics"] = supervised
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result = {
            **scenario,
            "job_title": job.get("title"),
            "company_name": job.get("company_name"),
            "source_resume_path": str(source_path),
            "resume_routing": routing,
            "status": report.get("status"),
            "attempts": report.get("attempts"),
            "elapsed_seconds": round(time.perf_counter() - scenario_started, 2),
            "text_path": str(text_path) if tailored_text else None,
            "report_path": str(report_path),
            "supervised_metrics": supervised,
        }
        results.append(result)
        print(json.dumps({"event": "scenario_finished", **result}, ensure_ascii=False), flush=True)

    summary = {
        "scope": {
            "real_jobs": True,
            "isolated_app_dir": str(config.APP_DIR),
            "source_db": str(args.source_db.resolve()),
            "validation": args.validation,
            "max_retries": max(0, args.max_retries),
            "browser_launched": False,
            "submission_attempted": False,
        },
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "machine_validated": sum(item.get("status") == "machine_validated" for item in results),
        "failed": sum(
            item.get("status") not in {"machine_validated"}
            for item in results
        ),
        "results": results,
    }
    summary_path = config.LOG_DIR / f"tailoring-experiment-{run_id}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"event": "experiment_finished", "summary_path": str(summary_path), **summary}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
