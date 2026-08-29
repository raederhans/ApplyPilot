"""Pure, read-only primitives for the ApplyPilot opportunity radar.

This module intentionally has no database, network, browser, or application
automation dependency.  Adapters turn their observations into the data types
below; the caller decides how and whether those facts are persisted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class Track(StrEnum):
    """Stable top-level job-search tracks.

    These values are deliberately independent from a particular resume.  A
    later resume router can use the primary track without changing discovery.
    """

    PRODUCT_CONSULTING = "general_product_consulting"
    DATA_BI_DECISION = "data_bi_decision"
    AI_IMPLEMENTATION = "ai_implementation"
    SPATIAL = "spatial"


TRACK_SUBTRACKS: dict[Track, tuple[str, ...]] = {
    Track.PRODUCT_CONSULTING: (
        "product_management",
        "product_ops",
        "strategy_ops",
        "pre_sales_solution_consulting",
        "implementation_consulting",
    ),
    Track.DATA_BI_DECISION: (
        "data_analytics",
        "business_intelligence",
        "business_operations_analysis",
        "planning_analytics",
        "strategy_analytics",
    ),
    Track.AI_IMPLEMENTATION: (
        "ai_solutions",
        "forward_deployed_ai",
        "workflow_automation",
        "ai_product_ops",
        "technical_pre_sales_ai",
    ),
    Track.SPATIAL: (
        "urban_planning",
        "transport_planning",
        "geospatial",
        "location_intelligence",
        "digital_twin",
        "urban_technology",
    ),
}

TRACK_LABELS: dict[Track, str] = {
    Track.PRODUCT_CONSULTING: "Product, consulting, and pre-sales",
    Track.DATA_BI_DECISION: "Data, BI, and decision support",
    Track.AI_IMPLEMENTATION: "AI implementation",
    Track.SPATIAL: "Spatial, planning, and urban technology",
}

SUBTRACK_TO_TRACK = {
    subtrack: track for track, subtracks in TRACK_SUBTRACKS.items() for subtrack in subtracks
}


class SourceStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class LeadStatus(StrEnum):
    NEW = "new"
    TRIAGED = "triaged"
    AWAITING_OFFICIAL = "awaiting_official"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    EXPIRED = "expired"


LINKEDIN_WINDOWS = frozenset({"past-24h", "past-week", "past-month"})
OFFICIAL_SOURCE_KINDS = frozenset({"official_careers", "official_ats", "official_rss", "official_email"})
SOCIAL_SOURCE_KINDS = frozenset({"linkedin_post", "forum", "community", "aggregator"})
COMPANY_SEED_SOURCE_KINDS = frozenset({"company_seed"})
_TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


def normalize_text(value: object | None) -> str:
    """Return a whitespace-normalised human-readable string."""
    return " ".join(str(value or "").strip().split())


def normalize_identity(value: object | None) -> str:
    """Normalise an identity component without attempting fuzzy matching."""
    return re.sub(r"[^a-z0-9]+", "-", normalize_text(value).casefold()).strip("-")


def normalize_subtrack(value: object | None) -> str | None:
    """Return a known subtrack id, accepting human-readable separators."""
    candidate = normalize_identity(value).replace("-", "_")
    return candidate if candidate in SUBTRACK_TO_TRACK else None


def parent_track(subtrack: object | None) -> Track | None:
    """Return the stable top-level track for a recognised subtrack."""
    normalized = normalize_subtrack(subtrack)
    return SUBTRACK_TO_TRACK.get(normalized) if normalized else None


def canonicalize_url(url: object | None) -> str | None:
    """Canonicalise only safe URL differences used for exact identity.

    The function removes fragments and well-known tracking parameters.  It
    intentionally retains meaningful query parameters (including ATS filters
    and job identifiers), so it does not accidentally merge distinct jobs.
    """
    raw = normalize_text(url)
    if not raw:
        return None
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, urlencode(query, doseq=True), ""))


def build_linkedin_content_search_url(
    keywords: str,
    *,
    window: str = "past-24h",
) -> str:
    """Build a visible LinkedIn Content Search URL without accessing LinkedIn.

    LinkedIn's documented UI currently exposes these rolling windows.  This
    helper only creates a URL for candidate-operated visible review; callers
    must not treat it as permission for unattended scraping.
    """
    if window not in LINKEDIN_WINDOWS:
        raise ValueError(f"Unsupported LinkedIn content-search window: {window!r}")
    query = normalize_text(keywords)
    if not query:
        raise ValueError("LinkedIn content-search keywords cannot be empty")
    params = {
        "keywords": query,
        "origin": "FACETED_SEARCH",
        "sortBy": json.dumps(["date_posted"], separators=(",", ":")),
        "datePosted": json.dumps([window], separators=(",", ":")),
    }
    return "https://www.linkedin.com/search/results/content/?" + urlencode(params)


# Short name kept for adapter/CLI callers.  It is intentionally only a URL
# builder; no function in this module performs a LinkedIn request.
build_linkedin_search_url = build_linkedin_content_search_url


def split_linkedin_role_queries(
    role_terms: Sequence[str],
    *,
    hiring_terms: Sequence[str] = ("hiring",),
    location_terms: Sequence[str] = ("Singapore",),
    max_role_terms: int = 3,
    query_style: str = "simple",
) -> tuple[str, ...]:
    """Make small Boolean queries to avoid one opaque, over-broad search.

    Each returned query contains exactly one bounded group of role terms plus
    hiring and location intent.  Duplicate/empty role terms are discarded.
    """
    if max_role_terms < 1:
        raise ValueError("max_role_terms must be at least 1")
    unique_terms: list[str] = []
    seen: set[str] = set()
    for term in role_terms:
        cleaned = normalize_text(term)
        marker = cleaned.casefold()
        if cleaned and marker not in seen:
            seen.add(marker)
            unique_terms.append(cleaned)
    if not unique_terms:
        return ()
    hires = tuple(normalize_text(term) for term in hiring_terms if normalize_text(term))
    locations = tuple(normalize_text(term) for term in location_terms if normalize_text(term))
    if not hires or not locations:
        raise ValueError("hiring_terms and location_terms cannot be empty")
    if query_style == "simple":
        hiring = hires[0].strip('"')
        location = locations[0].strip('"')
        return tuple(
            f"{hiring} {term.strip(chr(34))} {location}" for term in unique_terms
        )
    if query_style == "hashtag_exact":
        hiring = hires[0].strip('"')
        location = locations[0].strip('"')
        return tuple(
            f'{hiring} "{term.strip(chr(34))}" {location}' for term in unique_terms
        )
    if query_style != "boolean":
        raise ValueError(f"Unsupported LinkedIn query style: {query_style!r}")
    prefix = " OR ".join(hires)
    suffix = " OR ".join(locations)
    result: list[str] = []
    for start in range(0, len(unique_terms), max_role_terms):
        terms = unique_terms[start : start + max_role_terms]
        roles = " OR ".join(
            f'"{term}"' if " " in term and not term.startswith('"') else term for term in terms
        )
        result.append(f"({prefix}) AND ({roles}) AND ({suffix})")
    return tuple(result)


def build_linkedin_query_matrix(
    config: Mapping[str, object], *, window: str | None = None
) -> tuple[dict[str, str], ...]:
    """Expand a ``linkedin_searches.yaml``-shaped mapping into small queries.

    The matrix is deliberately serialisable so a CLI can print URL review
    queues, and a database layer can store the exact query provenance, without
    giving this module any file-system, browser, or network responsibility.
    """
    defaults = _as_mapping(config.get("defaults"))
    selected_window = window or str(defaults.get("window") or "past-24h")
    max_role_terms = int(defaults.get("max_role_terms_per_query") or 3)
    query_style = str(defaults.get("query_style") or "simple")
    hiring_terms = tuple(str(item) for item in defaults.get("hiring_terms", ()) if normalize_text(item))
    location_terms = tuple(str(item) for item in defaults.get("location_terms", ()) if normalize_text(item))
    tracks = _as_mapping(config.get("tracks"))
    matrix: list[dict[str, str]] = []
    for raw_track, raw_subtracks in tracks.items():
        track = Track(str(raw_track))
        for raw_subtrack, raw_terms in _as_mapping(raw_subtracks).items():
            subtrack = normalize_subtrack(raw_subtrack)
            if not subtrack or parent_track(subtrack) is not track:
                raise ValueError(f"Unknown or mismatched radar subtrack: {raw_subtrack!r}")
            terms = (
                tuple(str(item) for item in raw_terms if normalize_text(item))
                if isinstance(raw_terms, Sequence) and not isinstance(raw_terms, str)
                else ()
            )
            for query in split_linkedin_role_queries(
                terms,
                hiring_terms=hiring_terms or ("hiring",),
                location_terms=location_terms or ("Singapore",),
                max_role_terms=max_role_terms,
                query_style=query_style,
            ):
                matrix.append(
                    {
                        "track": track.value,
                        "subtrack": subtrack,
                        "query": query,
                        "window": selected_window,
                        "url": build_linkedin_content_search_url(query, window=selected_window),
                    }
                )
    return tuple(matrix)


def classify_job_subtracks(
    title: object | None,
    query_config: Mapping[str, object],
) -> tuple[str, ...]:
    """Classify an official listing from conservative title phrase matches.

    The same role vocabulary powers the LinkedIn query queue and official-site
    filtering. Description text is intentionally excluded: generic mentions
    of products, data, or AI in a job body otherwise create unrelated matches.
    """
    normalized_title = re.sub(
        r"[^a-z0-9]+", " ", normalize_text(title).casefold()
    ).strip()
    if not normalized_title:
        return ()
    padded_title = f" {normalized_title} "
    tracks = _as_mapping(query_config.get("tracks"))
    matched: list[str] = []
    for track, subtracks in TRACK_SUBTRACKS.items():
        configured = _as_mapping(tracks.get(track.value))
        for subtrack in subtracks:
            raw_terms = configured.get(subtrack, ())
            if isinstance(raw_terms, str) or not isinstance(raw_terms, Sequence):
                continue
            terms = {
                re.sub(r"[^a-z0-9]+", " ", normalize_text(term).casefold()).strip()
                for term in raw_terms
            }
            if any(term and f" {term} " in padded_title for term in terms):
                matched.append(subtrack)
    return tuple(matched)


@dataclass(frozen=True, slots=True)
class SourceRun:
    """Health and completeness facts from one source collection attempt."""

    source_id: str
    status: SourceStatus | str
    source_kind: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pages_seen: int = 0
    observations_seen: int = 0
    error: str | None = None
    expected_pages: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", normalize_identity(self.source_id))
        object.__setattr__(self, "status", SourceStatus(self.status))
        object.__setattr__(
            self,
            "source_kind",
            normalize_identity(self.source_kind).replace("-", "_") or None,
        )
        object.__setattr__(self, "error", normalize_text(self.error) or None)
        if not self.source_id:
            raise ValueError("SourceRun.source_id cannot be empty")
        if self.pages_seen < 0 or self.observations_seen < 0:
            raise ValueError("SourceRun counts cannot be negative")

    @property
    def is_complete(self) -> bool:
        return self.status is SourceStatus.COMPLETE

    @property
    def zero_is_meaningful(self) -> bool:
        """A source can truthfully report zero only after a complete run."""
        return self.is_complete


@dataclass(frozen=True, slots=True)
class JobObservation:
    """A normalised fact observed at a company site, ATS, or other source."""

    source_id: str
    source_kind: str
    source_url: str
    title: str | None = None
    company: str | None = None
    official_job_url: str | None = None
    requisition_id: str | None = None
    location: str | None = None
    employment_type: str | None = None
    subtracks: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    is_official_publisher: bool = False
    is_verified_recruiter: bool = False
    official_target_open: bool | None = None
    discovered_at: datetime | None = None
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", normalize_identity(self.source_id))
        object.__setattr__(self, "source_kind", normalize_identity(self.source_kind).replace("-", "_"))
        object.__setattr__(self, "source_url", canonicalize_url(self.source_url) or "")
        object.__setattr__(self, "official_job_url", canonicalize_url(self.official_job_url))
        for attribute in ("title", "company", "requisition_id", "location", "employment_type"):
            object.__setattr__(self, attribute, normalize_text(getattr(self, attribute)) or None)
        subtracks = tuple(
            dict.fromkeys(normalized for item in self.subtracks if (normalized := normalize_subtrack(item)))
        )
        object.__setattr__(self, "subtracks", subtracks)
        source_ids = tuple(
            sorted(dict.fromkeys(
                normalized
                for item in (self.source_id, *self.source_ids)
                if (normalized := normalize_identity(item))
            ))
        )
        object.__setattr__(self, "source_ids", source_ids)
        if not self.source_id or not self.source_kind or not self.source_url:
            raise ValueError("JobObservation needs source_id, source_kind, and source_url")

    @property
    def primary_track(self) -> Track | None:
        return parent_track(self.subtracks[0]) if self.subtracks else None

    @property
    def source_count(self) -> int:
        return len(self.source_ids)


@dataclass(frozen=True, slots=True)
class OpportunityLead:
    """A social/community lead which remains distinct from a verified job."""

    observation: JobObservation
    status: LeadStatus | str = LeadStatus.NEW
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", LeadStatus(self.status))
        object.__setattr__(self, "reason", normalize_text(self.reason) or None)


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """The deterministic result of evaluating an observation for promotion."""

    status: LeadStatus
    reason: str

    @property
    def is_verified_job(self) -> bool:
        return self.status is LeadStatus.PROMOTED


def promote_observation(observation: JobObservation) -> PromotionDecision:
    """Decide whether a source observation is a verified listing or a lead.

    Official careers/ATS/RSS/email listings are promotable when they identify a
    company, title, and official target URL.  Social and forum material cannot
    self-promote: an official post must point to an open official job.  A
    verified recruiter post is useful, but remains a lead until that check.
    """
    if not observation.title or not observation.company:
        return PromotionDecision(LeadStatus.REJECTED, "missing company or title")
    official_url = observation.official_job_url or (
        observation.source_url if observation.source_kind in OFFICIAL_SOURCE_KINDS else None
    )
    if observation.source_kind in OFFICIAL_SOURCE_KINDS and official_url:
        return PromotionDecision(LeadStatus.PROMOTED, "official company or ATS listing")
    if observation.source_kind == "linkedin_post":
        if (
            observation.is_official_publisher
            and observation.official_target_open is True
            and observation.official_job_url
        ):
            return PromotionDecision(LeadStatus.PROMOTED, "official LinkedIn post linked to an open official job")
        if observation.is_verified_recruiter:
            return PromotionDecision(LeadStatus.AWAITING_OFFICIAL, "verified recruiter post awaits official listing")
        return PromotionDecision(LeadStatus.AWAITING_OFFICIAL, "LinkedIn post requires official-job verification")
    if observation.source_kind in SOCIAL_SOURCE_KINDS:
        return PromotionDecision(
            LeadStatus.AWAITING_OFFICIAL,
            "non-official discovery source requires official-job verification",
        )
    return PromotionDecision(LeadStatus.AWAITING_OFFICIAL, "source requires official-job verification")


def promote_lead(lead: OpportunityLead | JobObservation | Mapping[str, object]) -> PromotionDecision:
    """Compatibility entry point for a lead/observation record from a CLI or DB.

    This returns a decision only.  The persistence layer remains responsible
    for recording a promoted job or updating a lead state.
    """
    if isinstance(lead, OpportunityLead):
        return promote_observation(lead.observation)
    return promote_observation(_coerce_observation(lead))


def conservative_dedupe_key(observation: JobObservation) -> str | None:
    """Return one strong identity key, avoiding fuzzy merges of different jobs.

    The returned title/location key deliberately requires every component and
    is only a *candidate* key.  Persistence code must retain distinct listings
    when identifiers conflict.
    """
    company = normalize_identity(observation.company)
    requisition = normalize_identity(observation.requisition_id)
    if company and requisition:
        return f"req:{company}:{requisition}"
    official_url = canonicalize_url(observation.official_job_url)
    if official_url:
        return f"url:{official_url}"
    title = normalize_identity(observation.title)
    location = normalize_identity(observation.location)
    employment_type = normalize_identity(observation.employment_type)
    if company and title and location and employment_type:
        return f"candidate:{company}:{title}:{location}:{employment_type}"
    return None


def group_by_conservative_dedupe_key(observations: Iterable[JobObservation]) -> dict[str, list[JobObservation]]:
    """Group only observations with a safe key; unkeyed data stays distinct."""
    grouped: dict[str, list[JobObservation]] = {}
    for index, observation in enumerate(observations):
        key = conservative_dedupe_key(observation) or f"unkeyed:{index}:{observation.source_url}"
        grouped.setdefault(key, []).append(observation)
    return grouped


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _coerce_source_run(value: SourceRun | Mapping[str, object]) -> SourceRun:
    if isinstance(value, SourceRun):
        return value
    data = _as_mapping(value)
    raw_status = str(data.get("status") or SourceStatus.SKIPPED).casefold()
    derived_error = str(data.get("error") or "") or None
    if raw_status in {"failed", "running"}:
        raw_status = SourceStatus.PARTIAL
        derived_error = derived_error or "run unfinished"
    if raw_status == SourceStatus.COMPLETE and data.get("pagination_complete") is False:
        raw_status = SourceStatus.PARTIAL
        derived_error = derived_error or "pagination incomplete"
    return SourceRun(
        source_id=str(data.get("source_id") or data.get("source") or ""),
        status=raw_status,
        source_kind=data.get("source_kind") if isinstance(data.get("source_kind"), str) else data.get("kind"),
        started_at=data.get("started_at") if isinstance(data.get("started_at"), datetime) else None,
        finished_at=data.get("finished_at") if isinstance(data.get("finished_at"), datetime) else None,
        pages_seen=int(data.get("pages_seen") or data.get("pages") or 0),
        observations_seen=int(data.get("observations_seen") or data.get("count") or 0),
        error=derived_error,
        expected_pages=int(data["expected_pages"]) if data.get("expected_pages") is not None else None,
    )


def _coerce_observation(value: JobObservation | Mapping[str, object]) -> JobObservation:
    if isinstance(value, JobObservation):
        return value
    data = _as_mapping(value)
    raw_subtracks = data.get("subtracks") or data.get("subtrack") or ()
    if isinstance(raw_subtracks, str):
        raw_subtracks = (raw_subtracks,)
    raw_source_ids = data.get("source_ids") or ()
    if isinstance(raw_source_ids, str):
        raw_source_ids = (raw_source_ids,)
    return JobObservation(
        source_id=str(data.get("source_id") or data.get("source") or ""),
        source_kind=str(data.get("source_kind") or data.get("kind") or ""),
        source_url=str(data.get("source_url") or data.get("url") or ""),
        title=data.get("title") if isinstance(data.get("title"), str) else None,
        company=data.get("company") if isinstance(data.get("company"), str) else None,
        official_job_url=data.get("official_job_url") if isinstance(data.get("official_job_url"), str) else None,
        requisition_id=data.get("requisition_id") if isinstance(data.get("requisition_id"), str) else None,
        location=data.get("location") if isinstance(data.get("location"), str) else None,
        employment_type=data.get("employment_type") if isinstance(data.get("employment_type"), str) else None,
        subtracks=tuple(str(item) for item in raw_subtracks if normalize_text(item)),
        source_ids=tuple(str(item) for item in raw_source_ids if normalize_text(item)),
        is_official_publisher=bool(data.get("is_official_publisher")),
        is_verified_recruiter=bool(data.get("is_verified_recruiter")),
        official_target_open=data.get("official_target_open")
        if isinstance(data.get("official_target_open"), bool)
        else None,
        discovered_at=data.get("discovered_at") if isinstance(data.get("discovered_at"), datetime) else None,
        published_at=data.get("published_at") if isinstance(data.get("published_at"), datetime) else None,
    )


def _coerce_lead(value: OpportunityLead | Mapping[str, object]) -> OpportunityLead:
    if isinstance(value, OpportunityLead):
        return value
    data = _as_mapping(value)
    observation_value = data.get("observation")
    if not isinstance(observation_value, (JobObservation, Mapping)):
        observation_value = data
    return OpportunityLead(
        _coerce_observation(observation_value),
        status=str(data.get("status") or LeadStatus.NEW),
        reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
    )


def _display_job(observation: JobObservation) -> str:
    title = observation.title or "Untitled role"
    company = observation.company or "Unknown company"
    location = f" — {observation.location}" if observation.location else ""
    url = observation.official_job_url or observation.source_url
    tags = f" [{', '.join(observation.subtracks)}]" if observation.subtracks else ""
    lineage = ""
    if observation.source_count > 1:
        lineage = f" | {observation.source_count} sources: {', '.join(observation.source_ids)}"
    return f"- {title} | {company}{location}{tags} | {url}{lineage}"


def _display_lead(observation: JobObservation) -> str:
    """Render the candidate URL without losing the portal evidence URL."""
    provenance = f" | lead source: {observation.source_id}"
    if (
        observation.official_job_url
        and observation.source_url != observation.official_job_url
    ):
        provenance += f" ({observation.source_url})"
    return _display_job(observation) + provenance


def _merge_source_lineage(group: Sequence[JobObservation]) -> JobObservation:
    """Keep every exact-source lineage label when rendering one deduped job."""
    source_ids = tuple(
        sorted(dict.fromkeys(source_id for item in group for source_id in item.source_ids))
    )
    return replace(group[0], source_ids=source_ids)


def render_daily_report(
    *,
    source_runs: Sequence[SourceRun | Mapping[str, object]],
    observations: Sequence[JobObservation | Mapping[str, object]] = (),
    leads: Sequence[OpportunityLead | Mapping[str, object]] = (),
    company_seeds: Sequence[Mapping[str, object]] = (),
    applied_exclusions: Sequence[Mapping[str, object]] = (),
    applied_snapshot: Mapping[str, object] | None = None,
    report_date: str | None = None,
) -> str:
    """Render a daily, side-effect-free report with explicit coverage truth.

    A zero-count claim is emitted only when every relevant source run completed.
    Partial or blocked sources are labelled ``unavailable`` rather than zero.
    """
    raw_runs = [_as_mapping(run) for run in source_runs]
    normalized_runs = [_coerce_source_run(run) for run in source_runs]
    normalized_observations = [_coerce_observation(item) for item in observations]
    normalized_leads = [_coerce_lead(item) for item in leads]
    normalized_seeds = [_as_mapping(item) for item in company_seeds]
    official_runs = [
        run
        for run in normalized_runs
        if run.source_kind in OFFICIAL_SOURCE_KINDS
        or run.source_id.startswith("official-")
        or run.source_id.startswith("ats-")
    ]
    seed_runs = [
        run for run in normalized_runs if run.source_kind in COMPANY_SEED_SOURCE_KINDS
    ]
    lead_runs = [
        run
        for run in normalized_runs
        if run not in official_runs and run not in seed_runs
    ]
    official_complete = bool(official_runs) and all(run.is_complete for run in official_runs)
    lead_complete = bool(lead_runs) and all(run.is_complete for run in lead_runs)
    promoted_candidates = [
        item for item in normalized_observations if promote_observation(item).is_verified_job
    ]
    promoted = [
        _merge_source_lineage(group)
        for group in group_by_conservative_dedupe_key(promoted_candidates).values()
    ]
    explicit_lead_urls = {lead.observation.source_url for lead in normalized_leads}
    implicit_leads = [
        OpportunityLead(item, decision.status, decision.reason)
        for item in normalized_observations
        if (decision := promote_observation(item)).status not in {LeadStatus.PROMOTED, LeadStatus.REJECTED}
        and item.source_url not in explicit_lead_urls
    ]
    non_promoted = [
        lead
        for lead in [*normalized_leads, *implicit_leads]
        if lead.status not in {LeadStatus.PROMOTED, LeadStatus.REJECTED, LeadStatus.EXPIRED}
    ]
    unavailable_official = [run for run in official_runs if not run.is_complete]
    unavailable_leads = [run for run in lead_runs if not run.is_complete]

    heading = f"# Daily opportunity radar{f' — {report_date}' if report_date else ''}"
    lines = [
        heading,
        "",
        "> Read-only discovery evidence; this is not an application or published recommendation list.",
        "",
        "## Source coverage",
        "",
    ]
    if not normalized_runs:
        lines.append("- unavailable: no source runs recorded")
    else:
        for run in normalized_runs:
            detail = f"; {run.error}" if run.error else ""
            lines.append(
                f"- {run.source_id}: {run.status.value} "
                f"({run.observations_seen} observations, {run.pages_seen} pages{detail})"
            )

    lines.extend(["", "## Exclusions", ""])
    exclusions = [
        (
            str(raw.get("source_id") or raw.get("source") or "unknown"),
            int(raw.get("filtered") or 0),
            int(raw.get("location_title_filtered") or 0),
            int(raw.get("track_filtered") or 0),
        )
        for raw in raw_runs
        if int(raw.get("filtered") or 0) > 0
    ]
    linkedin_applied_count = sum(
        1 for item in applied_exclusions if item.get("exclusion_source") == "linkedin_applied"
    )
    local_application_count = sum(
        1 for item in applied_exclusions if item.get("exclusion_source") == "local_application"
    )
    snapshot = _as_mapping(applied_snapshot)
    applied_complete = (
        snapshot.get("completeness") == "complete"
        and snapshot.get("integrity_valid") is True
        and snapshot.get("fresh") is True
    )
    if applied_complete:
        lines.append(
            "- LinkedIn Applied coverage: complete "
            f"({int(snapshot.get('declared_total') or 0)} records, "
            f"{int(snapshot.get('pages_read') or 0)} pages)"
        )
        lines.append(
            f"- LinkedIn Applied exclusions: {linkedin_applied_count} matched listings"
        )
        lines.append(
            "- LinkedIn Applied snapshot: "
            f"{snapshot.get('snapshot_id')} | observed {snapshot.get('observed_at')} | "
            f"imported {int(snapshot.get('imported_count') or 0)}, "
            f"updated {int(snapshot.get('updated_count') or 0)}, "
            f"skipped {int(snapshot.get('skipped_count') or 0)}"
        )
    else:
        lines.append(
            "- unavailable: LinkedIn Applied exclusion completeness is not freshly verified"
        )
        if linkedin_applied_count:
            lines.append(
                f"- LinkedIn Applied: {linkedin_applied_count} observed matches; zero/total unavailable"
            )
    lines.append(
        f"- Local application exclusions: {local_application_count} matched listings"
    )
    if exclusions:
        lines.extend(
            f"- {source_id}: {count} excluded "
            f"({location_title_count} by location/title; {track_count} by track)"
            for source_id, count, location_title_count, track_count in exclusions
        )
    else:
        lines.append("- 0 recorded source-policy exclusions")

    lines.extend(["", "## Verified jobs by track", ""])
    if promoted:
        grouped_tracks: dict[Track | None, list[JobObservation]] = {}
        for item in promoted:
            grouped_tracks.setdefault(item.primary_track, []).append(item)
        for track in Track:
            items = grouped_tracks.get(track, [])
            lines.extend([f"### {TRACK_LABELS[track]}", ""])
            if items:
                lines.extend(_display_job(item) for item in items)
            elif official_complete:
                lines.append("- 0 verified jobs in this track")
            else:
                lines.append("- unavailable: no verified item observed, but official coverage is incomplete")
            lines.append("")
        if unclassified := grouped_tracks.get(None, []):
            lines.extend(["### Unclassified verified listings", ""])
            lines.extend(_display_job(item) for item in unclassified)
        if unavailable_official:
            lines.append("- Coverage: unavailable for some official sources; this is not a complete zero/total.")
    elif official_complete:
        lines.append("- 0 verified jobs (all relevant official source runs complete)")
    else:
        names = ", ".join(run.source_id for run in unavailable_official) or "no official source run"
        lines.append(f"- unavailable: verified-job zero cannot be claimed ({names})")

    lines.extend(["", "## Leads awaiting official verification", ""])
    if non_promoted:
        for lead in non_promoted:
            reason = f"; {lead.reason}" if lead.reason else ""
            lines.append(f"{_display_lead(lead.observation)} [{lead.status.value}{reason}]")
        if unavailable_leads:
            lines.append("- Coverage: lead discovery is non-exhaustive or unavailable for some sources.")
    elif lead_complete:
        lines.append("- 0 actionable leads (all lead source runs complete)")
    else:
        names = ", ".join(run.source_id for run in unavailable_leads) or "no lead source run"
        lines.append(f"- unavailable: lead zero cannot be claimed ({names})")

    lines.extend(["", "## Company seeds awaiting official careers verification", ""])
    if normalized_seeds:
        for seed in sorted(
            normalized_seeds,
            key=lambda item: normalize_text(item.get("company_name")).casefold(),
        ):
            company_name = normalize_text(seed.get("company_name")) or "Unknown company"
            location = normalize_text(seed.get("location"))
            sectors = ", ".join(
                normalize_text(item)
                for item in seed.get("sectors", [])
                if normalize_text(item)
            )
            target_url = normalize_text(
                seed.get("careers_url") or seed.get("official_url")
            )
            source_ids = [
                normalize_text(item)
                for item in seed.get("source_ids", [])
                if normalize_text(item)
            ]
            details = [item for item in (location, sectors, target_url) if item]
            lineage = (
                f"; {len(source_ids)} sources: {', '.join(source_ids)}"
                if source_ids
                else ""
            )
            lines.append(
                f"- {company_name} | {' | '.join(details) or 'no official careers URL yet'} "
                f"[{normalize_text(seed.get('status')) or 'awaiting_official_careers'}{lineage}]"
            )
        if any(not run.is_complete for run in seed_runs):
            lines.append(
                "- Coverage: company directories are non-exhaustive; this is a seed queue, not a company total."
            )
    elif seed_runs and all(run.is_complete for run in seed_runs):
        lines.append("- 0 company seeds (all configured seed source runs complete)")
    else:
        names = ", ".join(run.source_id for run in seed_runs) or "no company-seed source run"
        lines.append(f"- unavailable: company-seed zero cannot be claimed ({names})")
    return "\n".join(lines) + "\n"
