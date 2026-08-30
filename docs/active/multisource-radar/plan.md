# Plan

## Goal

Upgrade ApplyPilot into a read-only, official-company-first multi-source job radar for Singapore. It must collect, normalize, verify, deduplicate, classify, and report daily opportunities across the existing four career tracks plus product, pre-sales, and planning subtracks.

## Scope

- Add official careers/ATS/RSS/JobPosting discovery adapters and a Singapore company watchlist.
- Add a LinkedIn content-search URL queue for 24-hour, weekly, and monthly windows; LinkedIn content remains a lead and is never scraped or promoted without verification.
- Add source-run, observation, lead, and job-source lineage storage while preserving the existing jobs/application schema.
- Add an isolated radar CLI path for collect, import-lead, and report operations.
- Produce an auditable daily report with source health, verified jobs, leads, exclusions, and unavailable-vs-zero semantics.
- Exercise multiple real source, track, prompt/query, failure, duplicate, and backfill scenarios and improve the implementation from observed failures.

## Sources of truth

- Current repository code and tests under `src/applypilot` and `tests`.
- Existing user-owned working-tree edits in `database.py`, `cli.py`, apply modules, `tests/test_local_compat.py`, and `tools/`; these must be preserved.
- Official ATS/API/RSS contracts and live official careers endpoints.
- The existing LinkedIn Applied fail-closed recommendation boundary and local portal policy.

## Stages

- [x] Stage 1: Baseline audit, durable task record, and non-overlapping ownership.
- [x] Stage 2: Unified radar schema and ingestion contract.
- [x] Stage 3: Official company adapters and watchlist.
- [x] Stage 4: LinkedIn query queue, lead promotion, taxonomy, and reporting.
- [x] Stage 5: CLI integration and compatibility migration.
- [x] Stage 6: Focused unit and integration verification.
- [x] Stage 7: Multiple live, directionally varied source/query/scenario runs and evidence-led refinement.
- [x] Stage 8: Final regression verification and handoff.
- [x] Stage 9: Inspect the existing daily automation and re-verify its complete LinkedIn Applied prerequisite.
- [x] Stage 10: Connect Applied import, official collection, LinkedIn lead queue, and radar reporting to automation-7.
- [x] Stage 11: Run repeated production-data radar collections and compare increment/dedupe/source-health results.
- [x] Stage 12: Refine the automation and implementation from live evidence, then complete regression review.
- [x] Stage 13: Bind each daily report to the exact complete Applied snapshot created in that run.
- [x] Stage 14: Add a discovery-only command/path allowlist and remove unattended LinkedIn browsing.
- [x] Stage 15: Complete post-hardening independent review and final handoff.

## Acceptance criteria

- Discovery/report execution cannot apply, fill forms, message, or upload a resume.
- The daily automation invokes only `run-radar.ps1`; the wrapper rejects non-discovery commands before starting ApplyPilot and confines imports/reports to radar data directories.
- Every due source run is complete, partial, blocked, or skipped-with-reason; only complete runs can assert zero results.
- Official careers/ATS/RSS observations can become verified jobs; LinkedIn/forum observations remain leads until an official open listing is verified.
- The same requisition seen in multiple sources forms one job with multiple observations; different requisition IDs are not automatically merged.
- The four existing top-level tracks remain stable and new product, pre-sales, and planning directions are represented as subtracks.
- Daily output separates verified jobs, actionable leads, exclusions, and source gaps and can be recomputed from stored run/observation data.
- In the daily automation, LinkedIn Applied incompleteness stops synchronization, source collection, published recommendations, and recommendation-history writes.
- A published daily report must bind the exact snapshot ID returned by that run's sync and validate observed-time freshness, completeness, page/count integrity, and zero skipped rows.
- Multiple real tests cover different providers, tracks, query prompts, duplicate/repost cases, partial/blocked sources, and temporal windows.
- All existing user-owned changes remain present.

## Non-goals

- No job application, form filling, messages, account creation, or resume upload.
- No autonomous LinkedIn crawling, infinite scrolling, hidden API calls, or private-group access.
- No resume rewriting or new top-level resume variants in this phase.
- No production deployment or external notification subscription.

## Risks and constraints

- The working tree is already dirty and overlaps database/CLI integration files.
- Workday and custom careers pages are less stable than structured ATS/RSS sources.
- LinkedIn search is personalized and non-exhaustive, so its coverage cannot be reported as complete.
- Live endpoints may rate-limit, change schema, or be unavailable; live tests must use bounded read-only requests and record unavailable rather than zero.
- Long/live verification has one owner and must not share the production ApplyPilot database.
