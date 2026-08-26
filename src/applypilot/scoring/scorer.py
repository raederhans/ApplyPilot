"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from applypilot import config as _config
from applypilot.config import load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.cover_letter import read_resume_source
from applypilot.scoring.tailor import select_resume_source

log = logging.getLogger(__name__)

# Kept as a public compatibility alias for callers that historically patched
# this value. Scoring now selects a per-job source through ``select_resume_source``.
RESUME_PATH = _config.RESUME_PATH


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response.strip()

    if not reasoning:
        return {
            "score": 0,
            "keywords": "",
            "reasoning": "Empty LLM response; the model may need a larger output-token budget.",
        }

    # Accept a JSON object when an OpenAI-compatible model chooses to return one
    # despite the requested line-oriented format.
    try:
        json_text = response.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", json_text, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            json_text = fenced.group(1)
        payload = json.loads(json_text)
        if isinstance(payload, dict) and "score" in payload:
            parsed_score = max(1, min(10, int(payload["score"])))
            return {
                "score": parsed_score,
                "keywords": str(payload.get("keywords", "")),
                "reasoning": str(payload.get("reasoning", "")),
            }
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    for line in response.split("\n"):
        line = line.strip()
        normalized = re.sub(r"^[#*\-\s]+", "", line)
        if re.match(r"(?i)^score\s*:", normalized):
            try:
                score = int(re.search(r"\d+", normalized).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif re.match(r"(?i)^keywords\s*:", normalized):
            keywords = re.split(r":", normalized, maxsplit=1)[1].strip()
        elif re.match(r"(?i)^reasoning\s*:", normalized):
            reasoning = re.split(r":", normalized, maxsplit=1)[1].strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job.get('company_name') or 'Unknown employer'}\n"
        f"SOURCE BOARD: {job.get('source_site') or job.get('site') or 'Unknown'}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        # Reasoning-capable models can consume a small token budget before
        # emitting the requested structured answer, leaving `content` empty.
        # Scoring is a short structured task. A large generation budget caused
        # reasoning models to spend minutes on a three-line answer during the
        # broad real-network experiment.
        max_tokens = int(os.environ.get("APPLYPILOT_SCORE_MAX_TOKENS", "1024"))
        response = client.chat(
            messages,
            max_tokens=max_tokens,
            temperature=0.2,
            thinking={"type": "disabled"},
        )
        result = _parse_score_response(response)
        if result["score"] == 0 and not response.strip():
            meta = dict(getattr(client, "last_response_meta", {}) or {})
            if meta:
                result["reasoning"] += (
                    " Response metadata: "
                    f"finish_reason={meta.get('finish_reason')}, "
                    f"content_chars={meta.get('content_chars')}, "
                    f"reasoning_chars={meta.get('reasoning_chars')}, "
                    f"completion_tokens={meta.get('completion_tokens')}."
                )
        return result
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    profile = load_profile()
    conn = get_connection()
    from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility
    refresh_job_eligibility(conn)

    if rescore:
        query = f"SELECT * FROM jobs WHERE full_description IS NOT NULL AND {ELIGIBLE_SQL}"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        try:
            source_path, routing = select_resume_source(job, profile)
            resume_text = read_resume_source(source_path)
            result = score_job(resume_text, job)
            result["source_resume_path"] = str(source_path)
            result["resume_routing"] = routing
        except Exception as exc:
            log.error("Resume routing failed for job '%s': %s", job.get("title", "?"), exc)
            result = {
                "score": 0,
                "keywords": "",
                "reasoning": f"Resume routing failed: {exc}",
                "source_resume_path": None,
                "resume_routing": None,
            }
        result["url"] = job["url"]
        completed += 1

        if result["score"] == 0:
            errors += 1

        results.append(result)

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    # Write scores to DB. Provider/parser failures remain NULL so they are
    # distinguishable from a genuine low score and can be retried later.
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        if r["score"] == 0:
            conn.execute(
                "UPDATE jobs SET fit_score = NULL, scored_at = NULL, "
                "score_status = 'failed', score_error = ?, "
                "score_attempts = COALESCE(score_attempts, 0) + 1, "
                "tailor_source_resume_path = ? WHERE url = ?",
                (r["reasoning"], r.get("source_resume_path"), r["url"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?, "
                "score_status = 'scored', score_error = NULL, "
                "score_attempts = COALESCE(score_attempts, 0) + 1, "
                "tailor_source_resume_path = ? WHERE url = ?",
                (
                    r["score"],
                    f"{r['keywords']}\n{r['reasoning']}",
                    now,
                    r["source_resume_path"],
                    r["url"],
                ),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
