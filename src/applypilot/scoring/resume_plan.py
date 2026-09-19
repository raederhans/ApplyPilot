"""Deterministic content-allocation plans for resume tailoring.

The plan is deliberately advisory: it gives the writer an auditable page and
bullet budget before prose generation, while the factual and rendered-layout
gates remain authoritative.  Presets express stable role-family defaults and
the entry budgets are then adjusted from the JD and selected artifact.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from applypilot.scoring.pdf import parse_entries, parse_resume

CONTENT_PLAN_VERSION = "resume-content-plan-v2"

_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

CONTENT_PLAN_PRESETS: dict[str, dict[str, object]] = {
    "technical_project": {
        "page_intent": "one_page_preferred",
        "summary_words": [30, 44],
        "skill_rows": [3, 4],
        "primary_experience_bullets": [3, 4],
        "secondary_experience_bullets": [1, 3],
        "primary_project_bullets": [2, 3],
        "secondary_project_bullets": [1, 2],
        "target_total_bullets": [14, 18],
        "target_words": [500, 760],
    },
    "analytical_balanced": {
        "page_intent": "one_page_preferred",
        "summary_words": [30, 44],
        "skill_rows": [3, 4],
        "primary_experience_bullets": [3, 4],
        "secondary_experience_bullets": [1, 3],
        "primary_project_bullets": [2, 3],
        "secondary_project_bullets": [1, 2],
        "target_total_bullets": [13, 18],
        "target_words": [480, 740],
    },
    "experience_led": {
        "page_intent": "one_page_preferred",
        "summary_words": [32, 46],
        "skill_rows": [2, 4],
        "primary_experience_bullets": [3, 4],
        "secondary_experience_bullets": [2, 3],
        "primary_project_bullets": [1, 2],
        "secondary_project_bullets": [1, 1],
        "target_total_bullets": [13, 18],
        "target_words": [500, 760],
    },
    "early_career_balanced": {
        "page_intent": "one_page_preferred",
        "summary_words": [28, 42],
        "skill_rows": [3, 4],
        "primary_experience_bullets": [3, 4],
        "secondary_experience_bullets": [1, 2],
        "primary_project_bullets": [2, 3],
        "secondary_project_bullets": [1, 2],
        "target_total_bullets": [13, 17],
        "target_words": [460, 700],
    },
    "evidence_dense_two_page": {
        "page_intent": "two_pages_allowed_when_both_are_substantive",
        "summary_words": [34, 48],
        "skill_rows": [3, 5],
        "primary_experience_bullets": [3, 4],
        "secondary_experience_bullets": [2, 3],
        "primary_project_bullets": [2, 4],
        "secondary_project_bullets": [2, 3],
        "target_total_bullets": [20, 27],
        "target_words": [780, 1050],
    },
}


def _terms(job_profile: Mapping[str, object]) -> set[str]:
    result: set[str] = set()
    for key in ("required_skills", "preferred_skills", "deliverables"):
        values = job_profile.get(key, [])
        if isinstance(values, list):
            result.update(str(value).casefold() for value in values if str(value).strip())
    features = job_profile.get("features", {})
    if isinstance(features, Mapping):
        values = features.get("content_terms", [])
        if isinstance(values, list):
            result.update(str(value).casefold() for value in values if str(value).strip())
    return result


def entry_recency_key(entry: Mapping[str, object]) -> tuple[int, int, int]:
    """Return a sortable key from the entry subtitle without inventing dates."""
    subtitle = str(entry.get("subtitle") or "")
    is_current = int(bool(re.search(r"\b(?:present|current)\b", subtitle, re.IGNORECASE)))
    dated: list[tuple[int, int]] = []
    for month, year in re.findall(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})\b",
        subtitle,
        re.IGNORECASE,
    ):
        dated.append((int(year), _MONTH_NUMBERS[month[:3].casefold()]))
    if not dated:
        dated.extend((int(year), 0) for year in re.findall(r"\b(20\d{2})\b", subtitle))
    latest_year, latest_month = max(dated, default=(0, 0))
    return is_current, latest_year, latest_month


def order_entries_by_recency(entries: list[dict]) -> list[dict]:
    """Order current/recent entries first while preserving ties stably."""
    indexed = list(enumerate(entries))
    return [
        entry
        for _index, entry in sorted(
            indexed,
            key=lambda item: (*entry_recency_key(item[1]), -item[0]),
            reverse=True,
        )
    ]


def _entry_score(entry: Mapping[str, object], terms: set[str], index: int) -> float:
    text = " ".join(
        [
            str(entry.get("title") or ""),
            str(entry.get("subtitle") or ""),
            *[str(item) for item in entry.get("bullets", [])],
        ]
    ).casefold()
    hits = sum(1 for term in terms if term and term in text)
    numeric_evidence = min(2, len(re.findall(r"(?<![A-Za-z])\d", text)))
    recent_bonus = 1.5 if index == 0 else max(0.0, 0.5 - index * 0.1)
    return round(hits * 2.0 + numeric_evidence * 0.25 + recent_bonus, 3)


def _choose_preset(
    job_profile: Mapping[str, object],
    *,
    experience_count: int,
    project_count: int,
    source_bullets: int,
    route_context: Mapping[str, object],
) -> tuple[str, list[str]]:
    track = str(job_profile.get("track") or "")
    employment_type = str(job_profile.get("employment_type") or "")
    reasons: list[str] = []
    if employment_type == "internship":
        preset = "early_career_balanced"
        reasons.append("internship_or_early_career_role")
    elif track in {"ai_implementation", "technical_engineering"}:
        preset = "technical_project"
        reasons.append("technical_build_role")
    elif track in {"data_bi_decision", "spatial"}:
        preset = "analytical_balanced"
        reasons.append("analysis_or_spatial_role")
    else:
        preset = "experience_led"
        reasons.append("delivery_or_stakeholder_role")

    candidates = route_context.get("candidates", [])
    top_score = 0.0
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], Mapping):
        top_score = float(candidates[0].get("overall_score") or 0)
    if (
        experience_count >= 5
        and project_count >= 3
        and source_bullets >= 24
        and top_score >= 0.70
    ):
        preset = "evidence_dense_two_page"
        reasons.append("large_high_coverage_evidence_pool")
    return preset, reasons


def _entry_budgets(
    entries: list[dict],
    *,
    terms: set[str],
    primary_range: list[int],
    secondary_range: list[int],
    target_total: int,
) -> list[dict[str, object]]:
    """Allocate a relevance-informed bullet budget without hard recency hierarchy constraints.

    Relevance controls bullet allocation across entries: high-relevance entries
    receive more detail regardless of age, with no hard newer>=older or oldest-shorter
    constraints.
    """
    if not entries:
        return []
    scored = [(_entry_score(entry, terms, index), index, entry) for index, entry in enumerate(entries)]
    primary_index = max(scored, key=lambda item: (item[0], -item[1]))[1]
    entry_count = len(entries)
    max_bullets = int(primary_range[1])
    min_bullets = max(1, int(secondary_range[0]))

    budgets: list[dict[str, object]] = []
    for score, index, entry in scored:
        is_primary = (index == primary_index)
        initial = int(primary_range[0]) if is_primary else min_bullets
        initial = min(initial, max_bullets)
        budgets.append(
            {
                "header": str(entry.get("title") or ""),
                "source_index": index,
                "recency_rank": index + 1,
                "relevance_score": score,
                "priority": "primary" if is_primary else "supporting",
                "bullet_budget": initial,
                "recency_cap": max_bullets,
                "retirement_allowed": index != 0,
            }
        )

    current_total = sum(int(item["bullet_budget"]) for item in budgets)
    # Distribute remaining bullets in order of relevance (highest relevance first)
    by_relevance = sorted(
        range(entry_count),
        key=lambda i: (float(budgets[i]["relevance_score"]), -i),
        reverse=True,
    )
    while current_total < target_total:
        changed = False
        for i in by_relevance:
            if int(budgets[i]["bullet_budget"]) < max_bullets:
                budgets[i]["bullet_budget"] = int(budgets[i]["bullet_budget"]) + 1
                current_total += 1
                changed = True
                if current_total >= target_total:
                    break
        if not changed:
            break

    return sorted(budgets, key=lambda item: int(item["source_index"]))


def build_content_plan(
    resume_text: str,
    job_profile: Mapping[str, object],
    route_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return a preset-guided, JD-adjusted allocation plan for one source resume."""
    route_context = dict(route_context or {})
    parsed = parse_resume(resume_text)
    sections = parsed.get("sections", {})
    experience_text = ""
    projects_text = ""
    for k, v in sections.items():
        k_upper = k.strip().upper()
        if not experience_text and ("EXPERIENCE" in k_upper or "HISTORY" in k_upper):
            experience_text = v
        if not projects_text and "PROJECTS" in k_upper:
            projects_text = v
    experience = order_entries_by_recency(parse_entries(experience_text))
    projects = parse_entries(projects_text)
    source_bullets = sum(len(entry.get("bullets", [])) for entry in [*experience, *projects])
    preset_name, reasons = _choose_preset(
        job_profile,
        experience_count=len(experience),
        project_count=len(projects),
        source_bullets=source_bullets,
        route_context=route_context,
    )
    preset = dict(CONTENT_PLAN_PRESETS[preset_name])
    terms = _terms(job_profile)
    target_min, target_max = [int(value) for value in preset["target_total_bullets"]]
    total_entries = max(1, len(experience) + len(projects))
    target_total = min(target_max, max(target_min, total_entries + 8))
    experience_share = 0.68 if preset_name == "experience_led" else 0.60
    experience_target = round(target_total * experience_share) if experience else 0
    project_target = target_total - experience_target if projects else 0
    if experience and not projects:
        experience_target = target_total
    if projects and not experience:
        project_target = target_total

    experience_budgets = _entry_budgets(
        experience,
        terms=terms,
        primary_range=list(preset["primary_experience_bullets"]),
        secondary_range=list(preset["secondary_experience_bullets"]),
        target_total=experience_target,
    )
    project_budgets = _entry_budgets(
        projects,
        terms=terms,
        primary_range=list(preset["primary_project_bullets"]),
        secondary_range=list(preset["secondary_project_bullets"]),
        target_total=project_target,
    )
    # Projects may freely reorder by relevance: present project budgets sorted by relevance descending
    project_budgets = sorted(
        project_budgets,
        key=lambda item: (float(item["relevance_score"]), -int(item["source_index"])),
        reverse=True,
    )

    def retirement_candidate(items: list[dict[str, object]]) -> str | None:
        eligible = [item for item in items if item["retirement_allowed"]]
        if len(items) < 4 or not eligible:
            return None
        weakest = min(eligible, key=lambda item: (float(item["relevance_score"]), -int(item["source_index"])))
        return str(weakest["header"]) if float(weakest["relevance_score"]) <= 0.5 else None

    return {
        "version": CONTENT_PLAN_VERSION,
        "preset": preset_name,
        "selection_reasons": reasons,
        "page_intent": preset["page_intent"],
        "summary_words": preset["summary_words"],
        "skill_rows": preset["skill_rows"],
        "target_words": preset["target_words"],
        "target_total_bullets": preset["target_total_bullets"],
        "allocation_policy": {
            "entry_order": "experience_reverse_chronological_projects_relevance",
            "experience_order": "reverse_chronological_by_actual_dates",
            "project_order": "freely_reordered_by_relevance",
            "bullet_allocation": "relevance_driven_no_recency_hierarchy_constraint",
            "recent_entries_receive_equal_or_more_detail": False,
            "oldest_entry_must_be_shorter_than_newest_when_three_or_more": False,
            "relevance_controls_fact_selection_not_recency_inversion": True,
        },
        "experience": experience_budgets,
        "projects": project_budgets,
        "retirement_candidates": {
            "experience": retirement_candidate(experience_budgets),
            "projects": retirement_candidate(project_budgets),
        },
        "job_profile": {
            "track": job_profile.get("track"),
            "subtype": job_profile.get("subtype"),
            "employment_type": job_profile.get("employment_type"),
        },
    }


def format_content_plan(plan: Mapping[str, object]) -> str:
    """Render a concise prompt block without turning advisory targets into facts."""
    lines = [
        f"PRESET: {plan.get('preset')}",
        f"PAGE INTENT: {plan.get('page_intent')}",
        f"SUMMARY WORD GUIDANCE: {plan.get('summary_words')}",
        f"SKILL ROW GUIDANCE: {plan.get('skill_rows')}",
        f"TOTAL WORD GUIDANCE: {plan.get('target_words')}",
        "ENTRY BULLET BUDGETS (guidance; factual support remains mandatory):",
    ]
    for section in ("experience", "projects"):
        for item in plan.get(section, []):
            lines.append(
                f"- {section.upper()} | {item['header']} | priority={item['priority']} | "
                f"recency_rank={item['recency_rank']} | relevance={item['relevance_score']} | "
                f"bullets={item['bullet_budget']} | recency_cap={item['recency_cap']}"
            )
    retirements = plan.get("retirement_candidates", {})
    if isinstance(retirements, Mapping):
        lines.append(
            "OPTIONAL RETIREMENT CANDIDATES: "
            f"experience={retirements.get('experience') or 'none'}; "
            f"projects={retirements.get('projects') or 'none'}"
        )
    lines.append(
        "EXPERIENCE entries must remain in reverse chronological order (newest/current first) using preserved dates. "
        "PROJECTS entries may freely reorder by relevance to the target role. "
        "Relevance controls detail and bullet allocation; more relevant entries may receive more bullets "
        "without hard recency hierarchy constraints. Treat budgets as directional: never invent or pad "
        "content to hit a number. Actual PDF fit and factual evidence override the preset."
    )
    return "\n".join(lines)
