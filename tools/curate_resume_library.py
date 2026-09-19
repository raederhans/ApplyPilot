"""Stage source-preserving editorial editions; promote only inspected editions.

Run with APPLYPILOT_DIR configured. This never updates jobs or source documents.
Stage is read-only for the live database; each candidate gets a unique run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pypdf import PdfReader

from applypilot import config
from applypilot.config import load_profile
from applypilot.resume_curation import apply_reviewed_edits, refine_existing_text
from applypilot.resume_library import (
    _record_artifact_health,
    _record_validation,
    _register_artifact,
    assess_resume_artifact_health,
    ensure_resume_library_schema,
    extract_job_profile,
)
from applypilot.resume_versions import finish_resume_run, profile_fact_snapshot, start_resume_run, write_record
from applypilot.scoring.cover_letter import read_resume_source
from applypilot.scoring.pdf import convert_to_pdf
from applypilot.scoring.validator import validate_tailored_resume


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parent_binding(artifact):
    return {key: artifact[key] for key in (
        "artifact_id", "active", "validation_status", "updated_at", "content_sha256",
        "text_path", "pdf_path", "pdf_sha256", "pdf_size",
    )}


def stage(report_dir: Path, only: str | None = None):
    profile = load_profile()
    conn = sqlite3.connect(config.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sources = [Path(v["path"]) for v in profile["tailoring"]["resume_variants"]]
    source_texts = {str(p): read_resume_source(p) for p in sources}
    refinements_path = report_dir / "editorial-refinements.json"
    refinements = json.loads(refinements_path.read_text(encoding="utf-8")) if refinements_path.exists() else {}
    artifacts = [dict(r) for r in conn.execute(
        "SELECT * FROM resume_artifacts WHERE active=1 AND kind='tailored' AND validation_status='machine_validated'"
    )]
    records = []
    if only:
        previous = json.loads((report_dir / "staged-editions.json").read_text(encoding="utf-8"))
        records = [r for r in previous["records"] if r["parent_artifact_id"] != only]
        artifacts = [a for a in artifacts if a["artifact_id"] == only]
    else:
        selection = report_dir / "selected-artifacts.json"
        if selection.exists():
            selected = set(json.loads(selection.read_text(encoding="utf-8")))
            artifacts = [a for a in artifacts if a["artifact_id"] in selected]
    for artifact in artifacts:
        old_path = Path(artifact["text_path"])
        old_text = old_path.read_text(encoding="utf-8")
        metadata = json.loads(artifact["metadata_json"] or "{}")
        row = conn.execute("SELECT * FROM jobs WHERE url=?", (metadata.get("registered_from_job"),)).fetchone()
        if row is None:
            row = conn.execute("""SELECT jobs.* FROM jobs JOIN resume_coverage_cells c
                ON jobs.url=c.evidence_job_url WHERE c.artifact_id=?
                ORDER BY jobs.fit_score DESC, jobs.url LIMIT 1""", (artifact["artifact_id"],)).fetchone()
        job = dict(row) if row else {}
        jp = extract_job_profile(job, profile)
        terms = list(dict.fromkeys([*jp.get("required_skills", []), *jp.get("preferred_skills", []),
                                   *jp.get("features", {}).get("content_terms", [])]))
        supplemental = "\n\n".join(source_texts.values())
        variant = next((v for v in profile["tailoring"]["resume_variants"]
                        if str(v.get("track", "")).startswith(str(jp.get("track") or artifact["track"]))),
                       profile["tailoring"]["resume_variants"][0])
        editorial_options = dict(refinements.get(artifact["artifact_id"], {}))
        refresh_evidence = editorial_options.pop("refresh_evidence", False)
        reviewed_edits = editorial_options.pop("reviewed_replacements", [])
        layout_options = {**profile.get("tailoring", {}).get("resume_layout", {}),
                          **editorial_options.pop("layout_override", {})}
        proposal = refine_existing_text(old_text, relevance_terms=terms,
                                         supplemental_text=source_texts[variant["path"]],
                                         **editorial_options)
        if refresh_evidence:
            proposal["changes"].append({"operation": "refresh_source_evidence", "claims_changed": False})
        if reviewed_edits:
            reviewed = apply_reviewed_edits(proposal["text"], reviewed_edits)
            proposal["text"] = reviewed["text"]
            proposal["changes"].extend(reviewed["changes"])
            proposal["claims_preserved"] = False
        record = {"parent_artifact_id": artifact["artifact_id"], "track": artifact["track"],
                  "parent_binding": parent_binding(artifact),
                  "job_url": job.get("url"), "job_title": job.get("title"),
                  "old_text_path": str(old_path), "old_text_sha256": fingerprint(old_path),
                  "old_pdf_path": artifact["pdf_path"], "old_pdf_sha256": fingerprint(artifact["pdf_path"]),
                  "changes": proposal["changes"]}
        if not proposal["changes"]:
            record["status"] = "unchanged"
            records.append(record)
            continue
        run = start_resume_run(config.APP_DIR, job, kind="library_editorial_curation")
        text_path = run / "resume.txt"
        text_path.write_text(proposal["text"], encoding="utf-8")
        source_path = Path(artifact.get("source_resume_path") or old_path)
        source_text = read_resume_source(source_path) if source_path.is_file() else ""
        # Old validated claims remain intact; additions must be verbatim source evidence.
        evidence = old_text + "\n\nSUPPLEMENTAL CANDIDATE EVIDENCE\n" + supplemental
        validation = validate_tailored_resume(proposal["text"], profile, original_text=evidence, selection_source_text=old_text)
        record.update(text_path=str(text_path), run_dir=str(run), source_resume_path=str(source_path),
                      validation=validation, status="needs_review")
        try:
            pdf_path = convert_to_pdf(text_path, layout_override=layout_options)
            pages = PdfReader(pdf_path).pages
            if len(pages) > 2:
                raise ValueError(f"Expected at most two pages, got {len(pages)}")
            extracted = "\n".join(page.extract_text() for page in pages)
            import re
            compact = lambda text: re.sub(r"\W", "", text).casefold()
            if compact(proposal["text"]) != compact(extracted):
                raise ValueError("PDF text differs from staged text; inspect extraction/reading order")
            record.update(pdf_path=str(pdf_path), pages=len(pages), text_roundtrip=True,
                          status="awaiting_visual_review" if validation["passed"] else "needs_content_review")
        except Exception as exc:  # noqa: BLE001 - preserve per-edition render failures for review
            record["render_error"] = str(exc)
        record["report_path"] = str(finish_resume_run(run, {
            "status": record["status"], "source_resume_path": str(source_path),
            "parent_artifact_id": artifact["artifact_id"], "validator": validation,
            "claims_preserved": proposal["claims_preserved"], "changes": proposal["changes"],
            "render_error": record.get("render_error"),
            "pages": record.get("pages"), "text_roundtrip": record.get("text_roundtrip", False),
            "layout_validation_options": layout_options,
        }, source_text=source_text, supplemental_evidence=supplemental))
        records.append(record)
        print(artifact["artifact_id"], record["status"], record.get("render_error", ""), flush=True)
    report = {"created_at": datetime.now(UTC).isoformat(),
              "source_hashes": {str(p): fingerprint(p) for p in sources}, "records": records}
    if only:
        order = {r["parent_artifact_id"]: i for i, r in enumerate(previous["records"])}
        records.sort(key=lambda r: order[r["parent_artifact_id"]])
    report_dir.mkdir(parents=True, exist_ok=True)
    target = report_dir / "staged-editions.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.close()
    print(target)


def promote(report_dir: Path):
    report = json.loads((report_dir / "staged-editions.json").read_text(encoding="utf-8"))
    reviewed = json.loads((report_dir / "visual-review.json").read_text(encoding="utf-8"))
    accepted = {r["parent_artifact_id"]: r for r in reviewed["accepted"]}
    for path, digest in report["source_hashes"].items():
        if fingerprint(path) != digest:
            raise ValueError(f"Source changed during curation: {path}")
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    backup_path = report_dir / ("before-promotion-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".db")
    with sqlite3.connect(backup_path) as backup:
        conn.backup(backup)
    before_jobs = [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY url")]
    profile = load_profile()
    ensure_resume_library_schema(conn)
    promoted = []
    merged = []
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        for r in report["records"]:
            if r["parent_artifact_id"] not in accepted:
                continue
            review = accepted[r["parent_artifact_id"]]
            parent_row = conn.execute("SELECT * FROM resume_artifacts WHERE artifact_id=?", (r["parent_artifact_id"],)).fetchone()
            if (not parent_row or not parent_row["active"]
                    or parent_row["validation_status"] != "machine_validated"
                    or parent_binding(dict(parent_row)) != r.get("parent_binding")):
                raise ValueError("Parent edition changed after staging; stage and inspect the current edition again")
            if not r.get("text_roundtrip") or not r["validation"]["passed"]:
                raise ValueError("Cannot promote an edition with unresolved content/render checks")
            for key in ("text", "pdf"):
                if fingerprint(r[f"{key}_path"]) != review[f"{key}_sha256"]:
                    raise ValueError("Reviewed edition changed after inspection")
                if fingerprint(r[f"old_{key}_path"]) != r[f"old_{key}_sha256"]:
                    raise ValueError("Historical edition changed")
            verdict_path = Path(r["run_dir"]) / "promotion-validation.json"
            preliminary = json.loads(Path(r["report_path"]).read_text(encoding="utf-8"))
            kind = ("curation_source_grounded_edits" if any(c.get("claims_changed") for c in r["changes"])
                    else "curation_preserved_claims")
            write_record(verdict_path, {**preliminary, "status": "machine_validated",
                "prior_validation_report": r["report_path"], "visual_review": review,
                "validation_kind": kind, "not_a_new_independent_factual_audit": True})
            artifact_id, _ = _register_artifact(
                conn, text_path=Path(r["text_path"]), kind="tailored", track=r["track"],
                source_resume_path=r["source_resume_path"], validation_status="machine_validated",
                report_path=str(verdict_path), metadata={
                    "parent_artifact_id": r["parent_artifact_id"], "curation_review": str(report_dir / "visual-review.json"),
                    "fact_snapshot": profile_fact_snapshot(profile), "registered_from_job": r["job_url"],
                    "supplemental_evidence_path": str(Path(r["run_dir"]) / "supplemental.txt"),
                },
            )
            successor = conn.execute(
                "SELECT active, validation_status FROM resume_artifacts WHERE artifact_id=?", (artifact_id,),
            ).fetchone()
            if not successor or not successor["active"] or successor["validation_status"] != "machine_validated":
                raise ValueError("Cannot promote retired content; retain the current parent and explicitly review restoration")
            _record_validation(conn, artifact_id=artifact_id, validation_kind=kind,
                               status="machine_validated", job_profile={}, evidence={
                                   "report_path": str(verdict_path), "visual_review": review,
                                   "parent_artifact_id": r["parent_artifact_id"],
                                   "not_a_new_independent_factual_audit": True,
                               })
            # Coverage transfers describe inherited evidence, not new JD validation runs.
            conn.execute("""INSERT OR IGNORE INTO resume_coverage_cells
                SELECT ?, taxonomy_version, track, subtype, evidence_job_url,
                       evidence_job_fingerprint, validated_at
                FROM resume_coverage_cells WHERE artifact_id=?""", (artifact_id, r["parent_artifact_id"]))
            if artifact_id != r["parent_artifact_id"]:
                parent = conn.execute("SELECT metadata_json FROM resume_artifacts WHERE artifact_id=?", (r["parent_artifact_id"],)).fetchone()
                meta = json.loads(parent[0] or "{}")
                meta["superseded_by"] = artifact_id
                conn.execute("UPDATE resume_artifacts SET active=0, validation_status='superseded_editorial', metadata_json=? WHERE artifact_id=?",
                             (json.dumps(meta), r["parent_artifact_id"]))
            current = dict(conn.execute("SELECT * FROM resume_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone())
            health = assess_resume_artifact_health(current, profile)
            if health["status"] != "eligible":
                raise ValueError(f"Edited edition not eligible: {health}")
            _record_artifact_health(conn, artifact_id=artifact_id, assessment=health)
            promoted.append({"parent": r["parent_artifact_id"], "successor": artifact_id, "pdf": current["pdf_path"]})
        merge_plan = report_dir / "merge-plan.json"
        successors = {item["parent"]: item["successor"] for item in promoted}
        if merge_plan.exists():
            for item in json.loads(merge_plan.read_text(encoding="utf-8")):
                destination = successors.get(item["to"], item["to"])
                donor = conn.execute("SELECT * FROM resume_artifacts WHERE artifact_id=? AND active=1",
                                     (item["from"],)).fetchone()
                target = conn.execute("SELECT * FROM resume_artifacts WHERE artifact_id=? AND active=1",
                                      (destination,)).fetchone()
                if donor is None or target is None or destination == item["from"]:
                    raise ValueError("Merge requires distinct active donor and destination editions")
                if assess_resume_artifact_health(dict(target), profile)["status"] != "eligible":
                    raise ValueError("Merge destination must be eligible")
                for key in ("text", "pdf"):
                    if fingerprint(donor[f"{key}_path"]) != item[f"{key}_sha256"]:
                        raise ValueError("Merge donor changed since editorial review")
                conn.execute("""INSERT OR IGNORE INTO resume_coverage_cells
                    SELECT ?, taxonomy_version, track, subtype, evidence_job_url,
                           evidence_job_fingerprint, validated_at
                    FROM resume_coverage_cells WHERE artifact_id=?""", (destination, item["from"]))
                meta = json.loads(donor["metadata_json"] or "{}")
                meta.update(superseded_by=destination, merged_into=destination, merge_reason=item["reason"],
                            merge_review=str(merge_plan))
                conn.execute("UPDATE resume_artifacts SET active=0, validation_status='superseded_editorial', metadata_json=? WHERE artifact_id=?",
                             (json.dumps(meta), item["from"]))
                merged.append({"from": item["from"], "into": destination, "reason": item["reason"]})
        if before_jobs != [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY url")]:
            raise ValueError("Curation must not alter application history")
    conn.close()
    (report_dir / "promotion.json").write_text(json.dumps({"backup": str(backup_path), "promoted": promoted, "merged": merged,
        "historical_job_rows_unchanged": True, "source_bytes_unchanged": True}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Promoted {len(promoted)} editions, merged {len(merged)} duplicates; preserved job rows and source bytes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "promote"))
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--only", help="Restage one failed edition, preserving other staged results")
    args = parser.parse_args()
    if args.action == "stage":
        stage(args.report_dir.resolve(), only=args.only)
    else:
        promote(args.report_dir.resolve())
