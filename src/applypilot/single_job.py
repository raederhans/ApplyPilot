"""Quality-first preparation for one explicitly selected job."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from applypilot import config
from applypilot.config import COVER_LETTER_DIR, RESUME_PATH, load_profile
from applypilot.database import get_connection
from applypilot.eligibility import refresh_job_eligibility
from applypilot.scoring.cover_letter import (
    CoverLetterValidationError,
    generate_cover_letter_document,
    load_evidence_sources,
    read_resume_source,
)
from applypilot.scoring.scorer import score_job
from applypilot.scoring.validator import validate_cover_letter


def _safe_filename(value: str, limit: int) -> str:
    value = re.sub(r"[^\w\s-]", "", value)[:limit].strip()
    return re.sub(r"\s+", "_", value)


def import_exact_job(
    url: str,
    title: str,
    company: str,
    location: str = "Singapore",
    site: str = "linkedin",
    description: str | None = None,
    strategy: str = "exact_url",
    application_url: str | None = None,
) -> dict:
    """Register one user-selected job URL for review or normal enrichment.

    A candidate-provided description is stored as supplied and avoids an
    unnecessary follow-up fetch. It is not presented as scraper output.
    """
    if not all(str(value).strip() for value in (url, title, company, site)):
        raise ValueError("url, title, company, and site are required.")
    if not re.match(r"^https://", url, flags=re.IGNORECASE):
        raise ValueError("url must be an absolute HTTPS URL.")
    application_target = str(application_url or url).strip()
    if not re.match(r"^https://", application_target, flags=re.IGNORECASE):
        raise ValueError("application_url must be an absolute HTTPS URL.")
    if not str(strategy).strip():
        raise ValueError("strategy is required.")

    from applypilot.eligibility import evaluate_job_eligibility

    now = datetime.now(UTC).isoformat()
    description_text = str(description or "").strip()
    eligibility_status, eligibility_reason = evaluate_job_eligibility({
        "title": title,
        "description": description_text,
        "full_description": description_text,
    })
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, location, company_name, source_site, site, strategy,
            description, full_description, application_url, detail_scraped_at,
            discovered_at, eligibility_status, eligibility_reason,
            eligibility_evaluated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title,
            location=excluded.location,
            company_name=excluded.company_name,
            source_site=excluded.source_site,
            site=excluded.site,
            strategy=excluded.strategy,
            description=CASE
                WHEN excluded.description IS NOT NULL AND excluded.description != ''
                THEN excluded.description ELSE jobs.description END,
            full_description=CASE
                WHEN excluded.full_description IS NOT NULL AND excluded.full_description != ''
                THEN excluded.full_description ELSE jobs.full_description END,
            application_url=COALESCE(excluded.application_url, jobs.application_url),
            detail_scraped_at=CASE
                WHEN excluded.full_description IS NOT NULL AND excluded.full_description != ''
                THEN excluded.detail_scraped_at ELSE jobs.detail_scraped_at END,
            eligibility_status=excluded.eligibility_status,
            eligibility_reason=excluded.eligibility_reason,
            eligibility_evaluated_at=excluded.eligibility_evaluated_at
        """,
        (
            url,
            title.strip(),
            location.strip(),
            company.strip(),
            site.strip(),
            site.strip(),
            strategy.strip(),
            description_text or None,
            description_text or None,
            application_target,
            now if description_text else None,
            now,
            eligibility_status,
            eligibility_reason,
            now,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE url=?", (url,)).fetchone()
    result = dict(row)
    from applypilot.enrichment.detail import sanitize_application_url

    sanitized_application_url = sanitize_application_url(
        url,
        result.get("application_url"),
    )
    if sanitized_application_url != result.get("application_url"):
        conn.execute(
            "UPDATE jobs SET application_url=? WHERE url=?",
            (sanitized_application_url, url),
        )
        conn.commit()
        result["application_url"] = sanitized_application_url
    return {
        "url": result["url"],
        "title": result["title"],
        "company": result["company_name"],
        "location": result["location"],
        "site": result["site"],
        "strategy": result["strategy"],
        "application_url": result["application_url"],
        "eligibility_status": result["eligibility_status"],
        "eligibility_reason": result["eligibility_reason"],
        "needs_enrichment": not bool(result.get("full_description")),
        "description_source": "candidate_provided" if description_text else None,
    }


def rekey_email_job(
    url: str,
    reference: str,
    title: str,
    company: str,
    description: str,
    location: str = "Singapore",
) -> dict:
    """Rekey one generic careers-page row as a unique direct-email listing.

    The fragment is an ApplyPilot-only tracking identity and is not sent to the
    employer. ``application_url`` remains the real, fragment-free source page.
    Existing application state is preserved by updating the row in place.
    """
    source_url = str(url or "").strip()
    parsed = urlparse(source_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("url must be an absolute HTTPS URL.")
    if parsed.fragment:
        raise ValueError("url must be the fragment-free generic source page.")

    reference_text = str(reference or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,100}", reference_text):
        raise ValueError("reference must be a lowercase letters/digits/hyphens slug.")
    if not all(str(value or "").strip() for value in (title, company, description)):
        raise ValueError("title, company, and description are required.")

    tracking_url = urlunparse(parsed._replace(fragment=f"applypilot-{reference_text}"))
    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url=?", (source_url,)).fetchone()
    if row is None:
        raise ValueError(f"No discovered job matches URL: {source_url}")
    if conn.execute("SELECT 1 FROM jobs WHERE url=?", (tracking_url,)).fetchone():
        raise ValueError(f"Tracking URL already exists: {tracking_url}")

    from applypilot.eligibility import evaluate_job_eligibility

    now = datetime.now(UTC).isoformat()
    description_text = str(description).strip()
    eligibility_status, eligibility_reason = evaluate_job_eligibility({
        "title": title,
        "description": description_text,
        "full_description": description_text,
    })
    conn.execute(
        """
        UPDATE jobs
        SET url=?, title=?, company_name=?, location=?,
            source_site='candidate_provided_email', site='candidate_provided_email',
            strategy='candidate_provided_email_listing',
            description=?, full_description=?, application_url=?, detail_scraped_at=?,
            eligibility_status=?, eligibility_reason=?, eligibility_evaluated_at=?
        WHERE url=?
        """,
        (
            tracking_url,
            title.strip(),
            company.strip(),
            location.strip(),
            description_text,
            description_text,
            source_url,
            now,
            eligibility_status,
            eligibility_reason,
            now,
            source_url,
        ),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM jobs WHERE url=?", (tracking_url,)).fetchone()
    return {
        "previous_url": source_url,
        "tracking_url": tracking_url,
        "application_url": updated["application_url"],
        "title": updated["title"],
        "company": updated["company_name"],
        "strategy": updated["strategy"],
        "description_source": "candidate_provided",
        "eligibility_status": updated["eligibility_status"],
        "apply_status": updated["apply_status"],
        "applied_at": updated["applied_at"],
    }


def import_portal_listings(csv_path: Path, portal: str | None = None) -> dict:
    """Import a candidate-provided JobStreet or InternSG listing export.

    The CSV is local intake only: it makes no HTTP request and never opens a
    browser. Required columns are ``url``, ``title``, and ``company``. Optional
    columns are ``location``, ``description``, and ``portal``; ``--portal``
    supplies the last value for a single-source export.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(f"Listing CSV not found: {csv_path}")

    imported: list[dict] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = {str(field or "").strip().casefold() for field in (reader.fieldnames or [])}
        missing = {"url", "title", "company"}.difference(fields)
        if missing:
            raise ValueError(
                "Listing CSV is missing required column(s): " + ", ".join(sorted(missing))
            )

        for line_number, raw_row in enumerate(reader, start=2):
            row = {
                str(key or "").strip().casefold(): str(value or "").strip()
                for key, value in raw_row.items()
            }
            source = (portal or row.get("portal") or "").strip()
            url = row.get("url", "")
            policy = config.get_portal_policy(url)
            if policy is None:
                raise ValueError(f"Line {line_number}: URL is not a configured portal listing: {url}")

            configured_names = {
                str(policy.get("name") or "").casefold(),
                *(str(name).casefold() for name in policy.get("site_names", []) if isinstance(name, str)),
            }
            if not source or source.casefold() not in configured_names:
                raise ValueError(
                    f"Line {line_number}: portal must identify {policy.get('name')!s}."
                )

            imported.append(
                import_exact_job(
                    url=url,
                    title=row.get("title", ""),
                    company=row.get("company", ""),
                    location=row.get("location") or "Singapore",
                    site=str(policy["name"]),
                    description=row.get("description") or None,
                    strategy="candidate_provided_portal_listing",
                )
            )

    return {
        "file": str(csv_path.resolve()),
        "imported": len(imported),
        "with_description": sum(1 for item in imported if not item["needs_enrichment"]),
        "without_description": sum(1 for item in imported if item["needs_enrichment"]),
        "listings": imported,
    }


def list_portal_listings(portal: str | None = None, limit: int = 100) -> list[dict]:
    """Return imported JobStreet and InternSG listings for local review."""
    if limit < 1:
        raise ValueError("limit must be at least 1.")

    policies = config.load_portal_policies()
    selected_names: list[str] = []
    if portal:
        candidate = portal.casefold().strip()
        for policy in policies:
            aliases = [policy.get("name", ""), *policy.get("site_names", [])]
            if any(str(alias).casefold().strip() == candidate for alias in aliases):
                selected_names = [str(policy["name"])]
                break
        if not selected_names:
            raise ValueError(f"Unknown configured portal: {portal}")
    else:
        selected_names = [str(policy["name"]) for policy in policies if policy.get("name")]

    if not selected_names:
        return []
    placeholders = ", ".join("?" for _ in selected_names)
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT url, title, company_name, location, source_site, strategy,
               full_description, eligibility_status, apply_status, discovered_at
        FROM jobs
        WHERE source_site IN ({placeholders})
        ORDER BY discovered_at DESC, url
        LIMIT ?
        """,
        [*selected_names, limit],
    ).fetchall()
    return [dict(row) for row in rows]


def score_exact_job_for_url(url: str, resume_path: str | None = None) -> dict:
    """Score one exact eligible job against one explicit resume evidence source."""
    conn = get_connection()
    refresh_job_eligibility(conn)
    row = conn.execute("SELECT * FROM jobs WHERE url=?", (url,)).fetchone()
    if row is None:
        raise ValueError(f"No discovered job matches URL: {url}")
    job = dict(row)
    if job.get("eligibility_status") == "ineligible":
        raise ValueError(
            "Job is hard-excluded by eligibility screening: "
            f"{job.get('eligibility_reason') or 'unspecified reason'}"
        )
    if not job.get("full_description"):
        raise ValueError("The selected job has no enriched description.")
    if not job.get("company_name"):
        raise ValueError("The selected job has no verified employer name.")

    profile = load_profile()
    selected_resume = Path(resume_path).resolve() if resume_path else RESUME_PATH.resolve()
    if not selected_resume.exists():
        raise FileNotFoundError(f"Resume source not found: {selected_resume}")
    resume_text = read_resume_source(selected_resume)
    evidence_sources = load_evidence_sources(profile, selected_resume, resume_text)
    score_context = "\n\n".join(source["text"] for source in evidence_sources)
    score = score_job(score_context, job)
    now = datetime.now(UTC).isoformat()
    if score["score"] == 0:
        conn.execute(
            "UPDATE jobs SET fit_score=NULL, scored_at=NULL, score_status='failed', "
            "score_error=?, score_attempts=COALESCE(score_attempts,0)+1, "
            "tailor_source_resume_path=? WHERE url=?",
            (score["reasoning"], str(selected_resume), url),
        )
        conn.commit()
        raise RuntimeError(f"LLM scoring failed: {score['reasoning']}")

    conn.execute(
        "UPDATE jobs SET fit_score=?, score_reasoning=?, scored_at=?, "
        "score_status='scored', score_error=NULL, "
        "score_attempts=COALESCE(score_attempts,0)+1, "
        "tailor_source_resume_path=? WHERE url=?",
        (
            score["score"],
            f"{score['keywords']}\n{score['reasoning']}",
            now,
            str(selected_resume),
            url,
        ),
    )
    conn.commit()
    conn.close()
    return {
        "url": url,
        "title": job["title"],
        "company": job["company_name"],
        "eligibility_status": job.get("eligibility_status") or "eligible",
        "resume_source": str(selected_resume),
        "fit_score_estimate": score["score"],
        "matched_keywords": score["keywords"],
        "score_reasoning": score["reasoning"],
        "scored_at": now,
    }


def _write_json_atomic(path: Path, payload: dict, token: str) -> None:
    """Write JSON beside its destination, then atomically promote it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{token}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _quarantine_revalidation_pdf(path: Path, label: str, token: str) -> Path | None:
    """Move a PDF out of the upload path so old manifests fail closed."""
    if not path.is_file():
        return None
    rejected_dir = path.parent / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    destination = rejected_dir / f"{path.stem}_{label}_{token}.pdf"
    path.replace(destination)
    return destination


def revalidate_tailored_resume_for_url(url: str) -> dict:
    """Fail-closed revalidation and atomic PDF promotion for one exact job."""
    conn = get_connection()
    job: dict | None = None
    tailored_path: Path | None = None
    source_path: Path | None = None
    report_path: Path | None = None
    final_pdf: Path | None = None
    temporary_pdf: Path | None = None
    previous_pdf: Path | None = None
    tailored_text: str | None = None
    shared_artifact = False
    token = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE url=? OR application_url=?", (url, url)
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(f"Expected one exact tailored job, found {len(rows)}: {url}")
        job = dict(rows[0])
        raw_tailored_path = str(job.get("tailored_resume_path") or "").strip()
        raw_source_path = str(job.get("tailor_source_resume_path") or "").strip()
        if not raw_tailored_path:
            raw_report_path = str(job.get("tailor_report_path") or "").strip()
            recoverable_statuses = {
                "failed_validation",
                "failed_judge",
                "failed_render",
                "failed_revalidation",
            }
            if raw_report_path and str(job.get("tailor_status") or "") in recoverable_statuses:
                existing_report_path = Path(raw_report_path).expanduser().resolve()
                report_stem = existing_report_path.name.removesuffix("_REPORT.json")
                rejected_path = (
                    existing_report_path.parent
                    / "rejected"
                    / f"{report_stem}_REJECTED.txt"
                )
                inferred_tailored_path = existing_report_path.with_name(
                    f"{report_stem}.txt"
                )
                if rejected_path.is_file():
                    inferred_tailored_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(rejected_path, inferred_tailored_path)
                    raw_tailored_path = str(inferred_tailored_path)
                    conn.execute(
                        "UPDATE jobs SET tailored_resume_path=? WHERE url=?",
                        (raw_tailored_path, job["url"]),
                    )
                    conn.commit()
            if not raw_tailored_path:
                raise ValueError("tailored_resume_path is required for revalidation")
        if not raw_source_path:
            raise ValueError("tailor_source_resume_path is required for revalidation")
        tailored_path = Path(raw_tailored_path).expanduser().resolve()
        source_path = Path(raw_source_path).expanduser().resolve()
        report_path = Path(
            str(
                job.get("tailor_report_path")
                or tailored_path.with_name(tailored_path.stem + "_REPORT.json")
            )
        ).expanduser().resolve()
        final_pdf = tailored_path.with_suffix(".pdf")

        shared_artifact_root = (config.APP_DIR / "resume-library" / "artifacts").resolve()
        try:
            tailored_path.relative_to(shared_artifact_root)
        except ValueError:
            pass
        else:
            shared_artifact = True
            raise ValueError(
                "A shared content-addressed resume artifact is immutable. "
                "Create and validate a new job-specific variant instead of revalidating it in place."
            )

        # Remove the previously authorized bytes from the upload path first.
        # A concurrent or stale manifest therefore cannot keep using them while
        # the content is being re-audited.
        previous_pdf = _quarantine_revalidation_pdf(final_pdf, "PRE_REVALIDATION", token)
        conn.execute(
            "UPDATE jobs SET tailor_status='revalidating', "
            "tailor_error='revalidation_in_progress', tailor_report_path=NULL, "
            "tailored_at=NULL, tailor_attempts=COALESCE(tailor_attempts,0)+1 "
            "WHERE url=?",
            (job["url"],),
        )
        conn.commit()

        if not tailored_path.is_file():
            raise FileNotFoundError(f"Tailored resume text not found: {tailored_path}")
        tailored_text = tailored_path.read_text(encoding="utf-8")
        if not source_path.is_file():
            raise FileNotFoundError(f"Tailoring source resume not found: {source_path}")

        from applypilot.resume_library import record_content_revalidation
        from applypilot.scoring.pdf import convert_to_pdf
        from applypilot.scoring.tailor import judge_tailored_resume
        from applypilot.scoring.validator import validate_tailored_resume

        profile = load_profile()
        source_text = read_resume_source(source_path)
        deterministic = validate_tailored_resume(
            tailored_text,
            profile,
            original_text=source_text,
        )
        judge = None
        status = "failed_validation"
        error = "; ".join(deterministic.get("errors", [])) or "validation failed"
        pdf_path = None
        if deterministic.get("passed"):
            judge = judge_tailored_resume(
                source_text,
                tailored_text,
                str(job.get("title") or ""),
                profile,
                job_description=str(job.get("full_description") or ""),
            )
            if judge.get("passed"):
                temporary_pdf = final_pdf.with_name(
                    f".{final_pdf.stem}.{token}.tmp.pdf"
                )
                try:
                    rendered_path = Path(
                        convert_to_pdf(tailored_path, output_path=temporary_pdf)
                    )
                    if rendered_path.resolve() != temporary_pdf.resolve():
                        raise ValueError("PDF renderer returned an unexpected output path")
                    if not temporary_pdf.is_file() or temporary_pdf.stat().st_size <= 0:
                        raise ValueError("PDF renderer did not produce a non-empty file")
                    os.replace(temporary_pdf, final_pdf)
                    temporary_pdf = None
                    pdf_path = str(final_pdf)
                    status = "machine_validated"
                    error = None
                except Exception as exc:  # noqa: BLE001 - render boundary must fail closed
                    if temporary_pdf is not None:
                        _quarantine_revalidation_pdf(
                            temporary_pdf, "FAILED_RENDER", token
                        )
                        temporary_pdf = None
                    status = "failed_render"
                    error = f"Render: {exc}"
            else:
                status = "failed_judge"
                error = f"Judge: {judge.get('issues') or 'no PASS verdict'}"

        report = {
            "status": status,
            "source_resume_path": str(source_path),
            "full_validator": deterministic,
            "judge": judge,
            "render_error": (
                error.removeprefix("Render: ")
                if error and status == "failed_render"
                else None
            ),
            "quarantined_previous_pdf_path": (
                str(previous_pdf) if previous_pdf is not None else None
            ),
            "revalidated_at": datetime.now(UTC).isoformat(),
        }
        _write_json_atomic(report_path, report, token)
        record_content_revalidation(
            conn,
            text=tailored_text,
            status=status,
            job=job,
            evidence={"report_path": str(report_path), "error": error},
        )
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE jobs SET tailor_status=?, tailor_error=?, tailor_report_path=?, "
            "tailored_at=? WHERE url=?",
            (
                status,
                error,
                str(report_path),
                now if status == "machine_validated" else None,
                job["url"],
            ),
        )
        conn.commit()
        return {
            "url": job["url"],
            "status": status,
            "tailored_resume_path": str(tailored_path),
            "pdf_path": pdf_path,
            "report_path": str(report_path),
            "error": error,
        }
    except Exception as exc:  # noqa: BLE001 - revalidation boundary must fail closed
        error = f"Revalidation: {type(exc).__name__}: {exc}"
        if not shared_artifact and final_pdf is not None and final_pdf.is_file():
            try:
                _quarantine_revalidation_pdf(final_pdf, "FAILED_REVALIDATION", token)
            except OSError:
                pass
        if temporary_pdf is not None and temporary_pdf.is_file():
            try:
                _quarantine_revalidation_pdf(
                    temporary_pdf, "FAILED_REVALIDATION", token
                )
            except OSError:
                pass
        if job is not None:
            try:
                if tailored_text is not None:
                    from applypilot.resume_library import record_content_revalidation

                    record_content_revalidation(
                        conn,
                        text=tailored_text,
                        status="failed_revalidation",
                        job=job,
                        evidence={"error": error},
                    )
                conn.execute(
                    "UPDATE jobs SET tailor_status='failed_revalidation', "
                    "tailor_error=?, tailor_report_path=NULL, tailored_at=NULL "
                    "WHERE url=?",
                    (error, job["url"]),
                )
                conn.commit()
            except Exception:  # noqa: BLE001 - preserve the original failure result
                conn.rollback()
        return {
            "url": job["url"] if job is not None else url,
            "status": "failed_revalidation",
            "tailored_resume_path": (
                str(tailored_path) if tailored_path is not None else None
            ),
            "pdf_path": None,
            "report_path": None,
            "error": error,
        }
    finally:
        conn.close()


def prepare_cover_letter_for_url(
    url: str,
    company: str,
    validation_mode: str = "strict",
    resume_path: str | None = None,
) -> dict:
    """Score and generate a strictly validated cover letter for one exact URL."""
    if not url or not company:
        raise ValueError("Both url and company are required.")
    if validation_mode not in {"strict", "normal", "lenient"}:
        raise ValueError("validation_mode must be strict, normal, or lenient.")

    conn = get_connection()
    refresh_job_eligibility(conn)
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    if row is None:
        raise ValueError(f"No discovered job matches URL: {url}")

    job = dict(row)
    if job.get("eligibility_status") == "ineligible":
        raise ValueError(
            "Job is hard-excluded by eligibility screening: "
            f"{job.get('eligibility_reason') or 'unspecified reason'}"
        )
    if not job.get("full_description"):
        raise ValueError("The selected job has no enriched description.")

    source_site = job.get("source_site") or job.get("site") or ""
    job["company_name"] = company
    job["source_site"] = source_site

    profile = load_profile()
    selected_resume = Path(resume_path).resolve() if resume_path else RESUME_PATH.resolve()
    if not selected_resume.exists():
        raise FileNotFoundError(f"Resume source not found: {selected_resume}")
    resume_text = read_resume_source(selected_resume)
    evidence_sources = load_evidence_sources(profile, selected_resume, resume_text)

    score_context = "\n\n".join(source["text"] for source in evidence_sources)
    score = score_job(score_context, job)
    if score["score"] == 0:
        conn.execute(
            "UPDATE jobs SET company_name=?, source_site=?, fit_score=NULL, scored_at=NULL, "
            "score_status='failed', score_error=?, "
            "score_attempts=COALESCE(score_attempts,0)+1 WHERE url=?",
            (company, source_site, score["reasoning"], url),
        )
        conn.commit()
        raise RuntimeError(f"LLM scoring failed: {score['reasoning']}")

    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE jobs SET company_name = ?, source_site = ?, fit_score = ?, "
        "score_reasoning = ?, scored_at = ?, score_status='scored', score_error=NULL, "
        "score_attempts=COALESCE(score_attempts,0)+1 WHERE url = ?",
        (
            company,
            source_site,
            score["score"],
            f"{score['keywords']}\n{score['reasoning']}",
            now,
            url,
        ),
    )
    conn.commit()

    try:
        document = generate_cover_letter_document(
            resume_text,
            job,
            profile,
            evidence_sources=evidence_sources,
            max_retries=max(0, int(os.environ.get("APPLYPILOT_DOCUMENT_MAX_RETRIES", "3"))),
            validation_mode=validation_mode,
        )
    except CoverLetterValidationError as exc:
        conn.execute(
            "UPDATE jobs SET cover_letter_status=CASE "
            "WHEN cover_letter_path IS NULL OR cover_letter_path='' THEN 'failed_validation' "
            "ELSE cover_letter_status END, cover_letter_error=?, "
            "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
            (str(exc), url),
        )
        conn.commit()
        raise
    letter = document["text"]
    validation = document["validation"]

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    prefix = f"{_safe_filename(company, 30)}_{_safe_filename(job['title'], 60)}"
    text_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
    report_path = COVER_LETTER_DIR / f"{prefix}_CL.report.json"
    text_path.write_text(letter, encoding="utf-8")

    report = {
        "url": url,
        "title": job["title"],
        "company": company,
        "source_site": source_site,
        "resume_source": str(selected_resume),
        "evidence_sources": [source["path"] for source in evidence_sources],
        "eligibility_status": job.get("eligibility_status") or "eligible",
        "eligibility_reason": job.get("eligibility_reason"),
        "fit_score_estimate": score["score"],
        "matched_keywords": score["keywords"],
        "score_reasoning": score["reasoning"],
        "validation_mode": validation_mode,
        "validation": validation,
        "evidence_plan": document["evidence_plan"],
        "surface": document["surface"],
        "word_count": len(letter.split()),
        "text_path": str(text_path),
        "pdf_path": None,
        "pdf_note": "PDF rendering is intentionally separate; human review is required before approval.",
        "status": "machine_validated",
        "human_approval_required": True,
        "generated_at": now,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)

    conn.execute(
        "UPDATE jobs SET cover_letter_path = ?, cover_letter_at = ?, "
        "cover_letter_status='machine_validated', cover_letter_error=NULL, "
        "cover_letter_approved_at=NULL, cover_letter_approved_by=NULL, "
        "cover_letter_source_resume_path=?, cover_letter_evidence_sources=?, "
        "cover_attempts = COALESCE(cover_attempts, 0) + 1 WHERE url = ?",
        (
            str(text_path),
            now,
            str(selected_resume),
            json.dumps([source["path"] for source in evidence_sources], ensure_ascii=False),
            url,
        ),
    )
    conn.commit()
    conn.close()
    return report


def approve_cover_letter_for_url(url: str, approved_by: str = "user") -> dict:
    """Record explicit human approval for the current machine-validated letter."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    if row is None:
        raise ValueError(f"No discovered job matches URL: {url}")
    job = dict(row)
    if job.get("cover_letter_status") != "machine_validated":
        raise ValueError(
            "Cover letter must be in machine_validated state before human approval; "
            f"current state is {job.get('cover_letter_status') or 'unset'}."
        )
    path = Path(job.get("cover_letter_path") or "")
    if not path.exists():
        raise FileNotFoundError(f"Cover letter artifact not found: {path}")

    profile = load_profile()
    personal = profile.get("personal", {})
    expected_signoff = (
        personal.get("preferred_display_name")
        or personal.get("preferred_name")
        or personal.get("full_name", "")
    )
    current_employment = profile.get("current_employment", {})
    validation = validate_cover_letter(
        path.read_text(encoding="utf-8"),
        mode="strict",
        expected_signoff=expected_signoff,
        company_name=job.get("company_name"),
        expected_current_title=str(current_employment.get("title", "")).strip() or None,
        expected_current_company=str(current_employment.get("company", "")).strip() or None,
    )
    if not validation["passed"]:
        raise ValueError("Current artifact no longer passes validation: " + "; ".join(validation["errors"]))

    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE jobs SET cover_letter_status='human_approved', "
        "cover_letter_approved_at=?, cover_letter_approved_by=? WHERE url=?",
        (now, approved_by, url),
    )
    conn.commit()
    return {
        "url": url,
        "cover_letter_path": str(path),
        "status": "human_approved",
        "approved_at": now,
        "approved_by": approved_by,
        "validation": validation,
    }


def mark_cover_letter_not_required_for_url(
    url: str,
    verified_by: str = "browser_preview",
) -> dict:
    """Record that an exact, successfully previewed form has no cover-letter field."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE url = ? OR application_url = ?", (url, url)
    ).fetchall()
    if len(rows) != 1:
        conn.close()
        raise ValueError(f"Expected one exact discovered job for URL, found {len(rows)}: {url}")
    job = dict(rows[0])
    job_url = str(job["url"])
    if job.get("eligibility_status") == "ineligible":
        conn.close()
        raise ValueError("Hard-excluded jobs cannot advance to application readiness.")
    if job.get("apply_status") != "previewed":
        conn.close()
        raise ValueError(
            "Cover-letter absence may be recorded only after a successful browser preview; "
            f"current apply status is {job.get('apply_status') or 'unset'}."
        )

    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE jobs SET cover_letter_status='not_required', cover_letter_error=NULL, "
        "cover_letter_approved_at=?, cover_letter_approved_by=? WHERE url=?",
        (now, verified_by, job_url),
    )
    conn.commit()
    conn.close()
    return {
        "url": job_url,
        "status": "not_required",
        "verified_at": now,
        "verified_by": verified_by,
    }
