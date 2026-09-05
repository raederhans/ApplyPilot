"""Soft company-diversity ranking for scored job candidates."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from itertools import groupby
from typing import Any


def _normalise_company(value: Any) -> str:
    """Normalise only case and whitespace; do not infer brand aliases."""
    if value is None:
        return ""
    return " ".join(str(value).split()).casefold()


def _company_key(job: dict, index: int) -> str:
    company = job.get("company_name")
    if company is None or not str(company).strip():
        company = job.get("company")
    normalised = _normalise_company(company)
    # Missing company data is not evidence that two jobs share an employer.
    return normalised or f"\0unknown:{index}"


def _round_robin(group: list[tuple[int, dict]]) -> list[dict]:
    by_company: dict[str, deque[dict]] = defaultdict(deque)
    company_order: list[str] = []
    for index, job in group:
        company = _company_key(job, index)
        if company not in by_company:
            company_order.append(company)
        by_company[company].append(job)

    ranked: list[dict] = []
    while by_company:
        for company in company_order:
            queue = by_company.get(company)
            if queue is None:
                continue
            ranked.append(queue.popleft())
            if not queue:
                del by_company[company]
    return ranked


def recent_handled_companies(conn: Any, days: int = 14) -> set[str]:
    """Return companies attempted or applied to within the requested window."""
    if days < 0:
        raise ValueError("days must be non-negative")
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT company_name
        FROM jobs
        WHERE company_name IS NOT NULL
          AND TRIM(company_name) != ''
          AND (
            (COALESCE(apply_attempts, 0) > 0
             AND last_attempted_at IS NOT NULL
             AND julianday(last_attempted_at) >= julianday(?))
            OR
            (applied_at IS NOT NULL
             AND julianday(applied_at) >= julianday(?))
          )
        """,
        (cutoff, cutoff),
    ).fetchall()
    return {
        normalised
        for row in rows
        if (normalised := _normalise_company(row[0]))
    }


def rank_company_diverse(
    jobs: list[dict], *, recent_companies: Iterable[str] = ()
) -> list[dict]:
    """Rank jobs by fit score, using company diversity only for equal scores.

    Jobs from companies absent from ``recent_companies`` lead each equal-score
    group. Within each recent/non-recent partition, companies take turns while
    preserving the input order of jobs from the same company. All jobs and the
    original dictionaries are retained. Jobs without a fit score sort last.
    """
    recent = {
        normalised
        for company in recent_companies
        if (normalised := _normalise_company(company))
    }
    indexed = list(enumerate(jobs))
    indexed.sort(
        key=lambda item: (
            item[1].get("fit_score") is None,
            -(item[1].get("fit_score") or 0),
            item[0],
        )
    )

    ranked: list[dict] = []
    for _, score_group_iter in groupby(
        indexed, key=lambda item: item[1].get("fit_score")
    ):
        score_group = list(score_group_iter)
        new_company_jobs: list[tuple[int, dict]] = []
        recent_company_jobs: list[tuple[int, dict]] = []
        for item in score_group:
            index, job = item
            target = (
                recent_company_jobs
                if _company_key(job, index) in recent
                else new_company_jobs
            )
            target.append(item)
        ranked.extend(_round_robin(new_company_jobs))
        ranked.extend(_round_robin(recent_company_jobs))
    return ranked
