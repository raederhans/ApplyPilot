"""Evidence-driven, reversible company priority. Never an eligibility gate.

Events are reviewed observations, not predictions from mail subjects or apply
failures. Replaying the immutable ledger makes expiry independent of cron jobs
and prevents previously consumed evidence from restarting a cooldown.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from applypilot.discovery.diversity import _normalise_company, rank_company_diverse

KINDS = {"submitted", "rejected", "progress", "assessment", "invited", "reset", "coverage", "scope"}
DAY = timedelta(days=1)


def event_order(event):
    # Submissions precede outcomes, and positive/manual updates win ties.
    order = {"submitted": 0, "scope": 0, "rejected": 1, "coverage": 2,
             "assessment": 3, "invited": 4, "progress": 5, "reset": 5}
    return timestamp(event["occurred_at"]), order[event["kind"]], event["event_id"]


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Event dates require a timezone")
    return result.astimezone(UTC)


def load_events(conn) -> list[dict]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_priority_events'"
    ).fetchone()
    if not exists:
        return []
    return [json.loads(row[0]) for row in conn.execute(
        "SELECT payload FROM company_priority_events ORDER BY occurred_at, event_id"
    )]


def import_events(conn, events: list[dict], *, now: datetime | None = None) -> int:
    """Atomically accept evidence-backed observations; IDs are immutable/idempotent.

application_id must identify a single employer requisition across listing URLs.
Submitted events must point at an already receipt-confirmed local job.
"""
    now = now or datetime.now(UTC)
    if not isinstance(events, list):
        raise TypeError("Expected an event array")
    existing = {event["event_id"]: event for event in load_events(conn)}
    pending = {}
    for raw in events:
        event = dict(raw)
        for field in ("event_id", "company", "kind", "occurred_at", "evidence"):
            if not isinstance(event.get(field), str) or not event[field].strip():
                raise ValueError(f"Missing {field}")
        if event["kind"] not in KINDS:
            raise ValueError("Unsupported event kind")
        when = timestamp(event["occurred_at"])
        if when > now:
            raise ValueError("Future observations are not accepted")
        event["occurred_at"] = when.isoformat()
        event["company"] = _normalise_company(event["company"])
        event["scope"] = str(event.get("scope") or "").strip().casefold()
        if event["kind"] in {"scope", "invited"}:
            row = conn.execute("SELECT company_name FROM jobs WHERE url=?", (event.get("job_url"),)).fetchone()
            if not row or _normalise_company(row[0]) != event["company"]:
                raise ValueError("Scope/invitation requires an exact local job and matching company")
        if event["kind"] == "scope" and not event["scope"]:
            raise ValueError("Reviewed scope must be nonempty")
        if event["kind"] in {"submitted", "rejected", "assessment", "invited"} and (
            not isinstance(event.get("application_id"), str) or not event["application_id"].strip()
        ):
            raise ValueError("A canonical application_id is required")
        if event["kind"] == "submitted":
            row = conn.execute(
                "SELECT company_name, apply_status, applied_at FROM jobs WHERE url=?",
                (event.get("job_url"),),
            ).fetchone()
            if not row or row[1] != "applied" or not row[2]:
                raise ValueError("Submitted evidence must reference a confirmed applied job")
            if _normalise_company(row[0]) != event["company"]:
                raise ValueError("Company must match the confirmed job record")
            if abs(timestamp(row[2]) - when) > DAY:
                raise ValueError("Submission date must match the confirmed job record")
        if event["kind"] == "coverage":
            if event.get("complete") is not True or not event.get("covered_since"):
                raise ValueError("Silence requires explicitly complete feedback coverage")
            if timestamp(event["covered_since"]) > when - 60 * DAY:
                raise ValueError("Feedback coverage must include the preceding 60 days")
        prior = existing.get(event["event_id"], pending.get(event["event_id"]))
        if prior and prior != event:
            raise ValueError("Event ID already exists with different content")
        if not prior:
            pending[event["event_id"]] = event
    all_events = list(existing.values()) + list(pending.values())
    applications = {}
    for event in sorted(all_events, key=event_order):
        key = (event["company"], event.get("application_id"))
        if event["kind"] == "submitted":
            previous = applications.get(key)
            if previous and (previous["scope"], previous["occurred_at"]) != (
                event["scope"], event["occurred_at"]
            ):
                raise ValueError("Conflicting identity/date for one application")
            applications[key] = event
        elif event["kind"] in {"rejected", "assessment"}:
            submitted = applications.get(key)
            if not submitted or submitted["scope"] != event["scope"]:
                raise ValueError("Feedback requires an earlier submission in the same scope")
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS company_priority_events (
            event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, payload TEXT NOT NULL
        )""")
        conn.executemany("INSERT INTO company_priority_events VALUES (?, ?, ?)", [
            (event["event_id"], event["occurred_at"], json.dumps(event, ensure_ascii=False))
            for event in pending.values()
        ])
    return len(pending)


def episodes(events: list[dict], now: datetime) -> dict[tuple[str, str], dict]:
    """Replay event-time decisions. Old evidence cannot restart an expired episode."""
    states = {}
    for event in sorted(events, key=event_order):
        when = timestamp(event["occurred_at"])
        if when > now:
            continue
        key = (event["company"], event.get("scope", ""))
        state = states.setdefault(key, {"apps": {}, "used": set(), "episode": None})
        kind = event["kind"]
        if kind == "scope":
            continue
        app_id = event.get("application_id")
        if kind in {"progress", "reset"}:
            # Company-wide observations clear all scopes; scoped ones clear their
            # own pool and any unknown-scope fallback pool, not unrelated teams.
            for other_key, other in states.items():
                if other_key[0] == key[0] and (not key[1] or other_key[1] in {"", key[1]}):
                    other["used"].update(other["apps"])
                    other["episode"] = None
            continue
        if kind == "submitted":
            state["apps"].setdefault(app_id, {"at": when, "rejected": False, "advanced": False})
        elif kind in {"rejected", "assessment"} and app_id in state["apps"]:
            state["apps"][app_id]["rejected" if kind == "rejected" else "advanced"] = True
            if kind == "assessment" and state["episode"]:
                state["episode"]["assessment"] = True
        elif kind == "invited":
            continue  # Exact-job exemption is applied separately during ranking.

        current = state["episode"]
        if current and when < current["start"] + 14 * DAY:
            continue
        available = {k: a for k, a in state["apps"].items() if k not in state["used"]}
        recent = {k: a for k, a in available.items() if when - 14 * DAY <= a["at"] <= when}
        reason, penalty, evidence_ids = None, 0.0, set()
        if (kind == "rejected" and len(recent) >= 3
                and sum(a["rejected"] for a in recent.values()) >= 2
                and not any(a["advanced"] for a in recent.values())):
            reason, penalty, evidence_ids = "recent_rejections", 0.25, set(recent)
        if kind == "coverage":
            mature = {k: a for k, a in available.items()
                      if when - 60 * DAY <= a["at"] <= when - 30 * DAY
                      and not a["rejected"] and not a["advanced"]}
            advanced = any(a["advanced"] and a["at"] >= when - 60 * DAY
                           for a in state["apps"].values())
            if len(mature) >= 4 and not advanced:
                reason, penalty, evidence_ids = "reviewed_silence", 0.20, set(mature)
        if reason:
            state["episode"] = {"start": when, "penalty": penalty, "reason": reason,
                                "assessment": False, "trigger": event["event_id"]}
            state["used"].update(evidence_ids)
    return {key: state["episode"] for key, state in states.items() if state["episode"]}


def rank_with_company_priority(conn, jobs: list[dict], *, recent_companies=(),
                               now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    events = load_events(conn)
    active = episodes(events, now)
    scopes = {e["job_url"]: e["scope"] for e in sorted(events, key=lambda e: (e["occurred_at"], e["event_id"]))
              if e["kind"] in {"scope", "submitted"} and timestamp(e["occurred_at"]) <= now}
    ranked = []
    for original in jobs:
        job = dict(original)
        company = _normalise_company(job.get("company_name") or job.get("company"))
        scope = str(job.get("priority_scope") or scopes.get(job.get("url")) or "").strip().casefold()
        best, details = 0.0, None
        for (employer, pool), episode in active.items():
            if employer != company or (scope and pool and scope != pool):
                continue
            age = (now - episode["start"]).total_seconds() / 86400
            factor = max(0.0, min(1.0, (14 - age) / 7))
            penalty = episode["penalty"] * factor
            if not scope or not pool:
                penalty *= 0.5
            if episode["assessment"]:
                penalty *= 0.5
            if penalty > best:
                best = penalty
                details = {"reason": episode["reason"], "trigger": episode["trigger"],
                           "started_at": episode["start"].isoformat(),
                           "restores_at": (episode["start"] + 14 * DAY).isoformat(),
                           "scope": pool, "scope_uncertain": not scope or not pool}
        invited = any(e["kind"] == "invited" and e["company"] == company
                      and e.get("job_url") == job.get("url") and job.get("url")
                      and now - 14 * DAY <= timestamp(e["occurred_at"]) <= now for e in events)
        if invited:
            best, details = 0.0, {"reason": "explicit_job_invitation"}
        job["company_priority"] = {"penalty": round(best, 6), "details": details}
        score = job.get("fit_score")
        job["application_priority_score"] = None if score is None else round(float(score) * (1 - best), 6)
        ranked.append(job)
    # Diversity remains a tie breaker. Its input/output scores are never rewritten.
    ranked = rank_company_diverse(ranked, recent_companies=recent_companies)
    ranked.sort(key=lambda j: (j["application_priority_score"] is None,
                              -(j["application_priority_score"] or 0)))
    return ranked
