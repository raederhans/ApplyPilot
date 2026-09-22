# Changelog

All notable changes to Job Apply Pilot will be documented in this file.

## [Unreleased]

## [0.6.0] - 2026-09-22

### Added

- Ordered, atomic top-level database migrations with explicit version checks,
  legacy upgrades, newer-schema fail-closed behavior, and rollback coverage.
- Shared transaction ownership and savepoint helpers across job, runtime,
  semantic-write, task-journal, and submission-ledger storage paths.
- Explicit runtime authority boundaries for preflight, application facts, job
  acquisition/results, agent processes, submission authority, and worker cells.
- Privacy-safe browser-tool duration samples and per-tool aggregates for
  diagnosing interaction latency without persisting field values or page data.

### Changed

- Renamed the public product identity from **CapyPilot** to **Job Apply Pilot**
  for clearer search visibility and immediate product recognition. Technical
  identifiers and runtime contracts remain unchanged.
- Replaced the mascot-led identity with a direct job-card, application-route,
  and verified-receipt system across the README and read-only dashboard.
- Rewrote the bilingual project overview around concrete capabilities, the
  Discover → Decide → Prepare → Verify workflow, and the product-contract
  differences from the original Pickle-Pixel/ApplyPilot project.
- Moved retired candidate facts into explicit user-owned lineage policy and
  shortened CI feedback while retaining Python 3.11/3.12/3.13, Chromium, and
  Windows clean-install coverage.
- Reduced redundant Lever browser round trips by grouping independent native
  selects behind one shared verification readback, while retaining individual
  handling for custom, conditional, multi-select, rerendered, or failed fields.

### Fixed

- Expired browser scope leases are reaped before exact-scope release, avoiding
  stale ownership that could block later application attempts.
- Preserved submission, receipt, duplicate, stale-attempt, and stale-page
  fail-closed behavior across the extracted runtime modules.

## [0.5.2] - 2026-09-21

### Added

- Immutable resume content, render, generation, and validation histories, with
  guarded curation, rollback, and exact input bindings for scoring and routing.
- A supervised attended-application path that binds browser observations,
  reviewed materials, authorization, single-submit intent, and receipt outcomes
  to the same durable application attempt.
- Evidence-backed two-week company prioritization with bounded penalties,
  recovery, progress resets, and explicit coverage requirements. Eligibility,
  fit scores, and submission gates remain unchanged.
- Bounded, value-free attended-host queue/action/observation diagnostics through
  `host.metrics()`, with explicit unavailable measurements and inspection coverage.
- Node contract tests and offline Chromium form checks in the existing Python
  core, browser, and Windows CI tiers. See
  [attended observation performance](docs/attended-observation-performance.md).

### Changed

- Resume routing now prefers healthy, evidence-dense reusable variants, preserves
  current-first experience ordering, and uses source-grounded targeted repairs
  instead of repeatedly regenerating complete documents.
- Attended form preparation now handles complex controls, open Shadow DOM,
  upload deltas, bounded form pagination, and selected-display verification while
  retaining fresh identity and submission-safety checks.
- Reuse one immediate, validated form readback in attended in-app-browser prepare
  replies while retaining current visible DOM. Explicit observations, uploads,
  screenshots, structural changes and submit-phase observations stay on the full
  path. No CAPTCHA, external-browser or production-concurrency policy changes.

### Added

- Bounded, value-free attended-host queue/action/observation diagnostics through
  `host.metrics()`, with explicit unavailable measurements and inspection coverage.
- Node contract tests and offline Chromium form checks in the existing Python
  core, browser and Windows CI tiers. See
  [attended observation performance](docs/attended-observation-performance.md).

### Fixed

- Job-description, selected-source, profile-fact, and supplemental-evidence
  changes now invalidate stale unsubmitted scoring and preparation projections
  without rewriting submitted or uncertain application history.
- Do not report an earlier control's persistence as current after a full refresh
  observes a changed value; explicit refreshes do not carry old control results.

## [0.5.1] - 2026-09-06

### Fixed

- Install Chromium in the tagged-release runner before executing browser tests.
  The v0.5.0 tag failed verification because the runner lacked Chromium; no
  v0.5.0 release artifacts were published. v0.5.1 includes the brand changes below.

## [0.5.0] - 2026-09-06

### Changed

- Renamed the public product identity to **CapyPilot** while retaining the
  `applypilot-local` distribution, `applypilot` package and CLI,
  `APPLYPILOT_*` environment variables, local storage identifiers, and
  repository URLs for compatibility.

### Added

- CapyPilot visual identity and bilingual product documentation.
- Expanded employer discovery with independent employer review before board
  leads enter the verified opportunity pipeline.
- Qualified resume reuse and safeguards against stale profile facts.
- Browser runtime and receipt reconciliation improvements that preserve
  application progress across verification and feedback failures.

### Fixed

- Manual `apply --mark-applied` and `--mark-failed` updates now accept either
  the canonical job URL or one uniquely matching application URL, and fail
  clearly instead of reporting success when no database row was updated.

## [0.4.0] - 2026-08-26

### Changed

- Established the independent **ApplyPilot Local** product identity while
  retaining the `applypilot` import package and CLI for compatibility.
- Reframed the product around local-first storage, explicit authorization,
  source provenance, validated resume reuse, and receipt-backed application
  status.
- Moved broad third-party job-board discovery behind the optional
  `jobboards` extra so the core product no longer inherits JobSpy's pinned
  NumPy/Pandas dependency chain.
- Added a single capability preflight before JobSpy expands its search matrix,
  with explicit Python 3.11-3.12 compatibility guidance.
- Replaced per-row Pandas Series construction in JobSpy ingestion and location
  filtering with record/list operations for substantially lower batch overhead.
- Restricted source distributions to an explicit release file set so local
  research notes, experiments, receipts, and runtime data cannot be packaged.
- Enabled automatic CI for pushes and pull requests, with lint, full tests,
  clean package builds, and dependency caching.
- Added a non-interactive, file-based onboarding path and browser-free
  dashboard generation for deterministic clean-workspace checks.
- Added a Windows release smoke job covering wheel install, initialization,
  setup diagnosis, and packaged dashboard generation.
- Gated tagged publishing on version parity before creating a GitHub Release
  with wheel, source archive, verified bundle, and checksums; PyPI publishing
  remains an explicit repository opt-in until its trusted publisher is ready.

### Added

- Fork provenance and AGPL network-deployment guidance in `NOTICE.md`.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `applypilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `applypilot apply`
- **Dry-run mode** - `applypilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/applypilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`applypilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging
