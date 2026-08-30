# Context

## Current truth

- Repository root: `C:/Users/raede/Desktop/简历/applypilot-local/source`.
- Existing uncommitted user changes are present in `src/applypilot/apply/launcher.py`, `src/applypilot/apply/prompt.py`, `src/applypilot/cli.py`, `src/applypilot/database.py`, untracked `tests/test_local_compat.py`, and `tools/`.
- Current discovery order is JobSpy, Workday, then SmartExtract; current pipeline also contains downstream tailoring/application-material stages.
- Implementation is constrained to read-only radar behavior. Application code is out of scope except for compatibility with existing user changes.

## Decisions and deviations

| Time | Evidence or decision | Impact |
| --- | --- | --- |
| 2026-08-24 | User approved implementation and required repeated real tests across prompts, tracks, and scenarios. | Work proceeds through implementation, live validation, and evidence-led iteration. |
| 2026-08-24 | Official company careers are the authoritative source; LinkedIn posts and forums are leads. | Data model separates observations, leads, and verified jobs. |
| 2026-08-24 | LinkedIn UI exposed past-24h, past-week, and past-month content-search windows. | Radar generates bounded search URLs but does not crawl LinkedIn. |
| 2026-08-24 | Existing database/CLI files are dirty. | Main agent owns careful incremental integration; workers own only new files. |
| 2026-08-24 | Fresh radar installs previously inherited the package's US search example. | Added a separate Singapore radar fallback; existing user searches.yaml remains honoured. |
| 2026-08-24 | Official-site global feeds contain many unrelated roles. | Official listings must pass location, title, and shared subtrack-title classification before formal job ingestion. |
| 2026-08-24 | A daily report is discovery evidence, not a recommendation publication. | Applied-set completeness is not bypassed; no recommendation history or application state is written. |
| 2026-08-24 | First live Stripe run collapsed 23 accepted listings into one job because its ATS exposed `See opening ID` as a shared placeholder. | Requisition placeholders are rejected as identities; provider/company/external ID remains the fallback. Fresh rerun stored 5/5 distinct target listings. |
| 2026-08-24 | Generic Remote locations admitted US-only Databricks listings in the first disposable run. | Radar now requires a configured geographic scope such as Singapore/APAC; ambiguous Remote/Hybrid is excluded by default. |
| 2026-08-24 | Four complex Boolean LinkedIn prompts all produced visible zero-result pages; four simple prompts produced visible posts. | Default content queries are now `hiring + one role term + Singapore`; Boolean syntax remains explicit opt-in. |
| 2026-08-24 | ST Engineering returned 406 for the narrow Accept header and its feed is latest-only. | Accept includes a low-priority wildcard; live retry read 10 items but the run stays partial/non-exhaustive. |
| 2026-08-24 | Final review reproduced promotion from a stale historical official observation. | Lead import no longer auto-promotes; only an explicit, finished, fresh official run that observed the exact URL can promote it. |
| 2026-08-24 | Multi-source observations survived database dedupe but their lineage was hidden in the report. | Deduped report rows now show source count and every source ID. |
| 2026-08-24 | Provider-owned JSON pagination could point at another origin or keep emitting unique pages. | Pagination now permits only same-origin HTTPS successors and stops at a bounded page limit. |
| 2026-08-24 | Fresh isolated OpenAI verification after the fixes. | Import stayed awaiting first; the next live Ashby run saw the exact URL and promoted one lead. |
| 2026-08-24 | User authorised integration with the existing daily task and repeated real ApplyPilot runs. | Reuse and update automation-7; do not create a second schedule. Production radar DB writes are now in scope, but application actions remain prohibited. |
| 2026-08-24 | Logged-in LinkedIn Job Tracker was re-read before production integration. | Applied showed 66 records across seven actual pages; all 66 stable job IDs were collected for local ApplyPilot import. |
| 2026-08-24 | Applied exports need proof of completeness rather than a bare list. | Added durable exclusion snapshots with observed total, page count, timestamp, freshness, and fail-closed report semantics. |
| 2026-08-24 | The first production collection admitted 28 candidates, including clearly senior titles. | Added a default seniority exclusion policy while retaining ordinary Product Manager, pre-sales, implementation, and planning discovery terms. |
| 2026-08-24 | Older observations remained visible after a stricter later collection. | Daily snapshots now use only the latest finished run per source in the requested window. |
| 2026-08-24 | Three identical production reruns produced no duplicate jobs. | Radar ingestion and report identity are idempotent on the current official-source matrix. |
| 2026-08-24 | Singapore LinkedIn post tests showed plain/quoted hiring prompts were dominated by global noise. | The default query format is now `#hiring + exact role + #singaporejobs`; same-post role/location/publisher/official-link evidence is mandatory before import. |
| 2026-08-24 | Existing `automation-7` was updated through the Codex automation surface. | Its schedule, active state, model, project, and working directories remain unchanged; the prompt now enforces Applied sync, official collect, candidate-visible LinkedIn review, report generation, and a narrow write boundary. |
| 2026-08-24 | Independent automation review found that freshness and no-submit behavior still depended too heavily on prompt wording. | Added exact Applied snapshot binding and the `run-radar.ps1` runtime capability allowlist before final approval. |
| 2026-08-24 | A newly imported snapshot was previously fresh by import time even if its observation was old. | Freshness now uses timezone-aware `observed_at`, a six-hour limit, count conservation, page evidence, zero skipped rows, and exact snapshot identity. |
| 2026-08-24 | General bootstrap created application-related directories and loaded `.env`. | Radar commands now use a narrow bootstrap that creates only the database/import/report lanes and does not load application or LLM credentials. |
| 2026-08-24 | A cron task cannot truthfully guarantee the candidate is present for LinkedIn content review. | Unattended automation now only emits bounded search URLs; post inspection and lead import require a separate attended-review invocation. |
| 2026-08-24 | Security review reproduced last-value override through repeated Typer options. | The wrapper now rejects repeated single-value options; Python validates final parsed source identity and file extensions. Both independent reviewers approved the repaired contract. |

## Live process ownership

| Process | Owner | Log path | State |
| --- | --- | --- | --- |
| Targeted unit tests for official adapters | `/root/official_adapters` | test output returned to main agent | complete: 4 passed |
| Targeted unit tests for radar pure logic | `/root/radar_core` | test output returned to main agent | complete: 23 passed |
| Shared integration and live source/query matrix | `/root` | `.artifacts/multisource-radar/live/` | complete; no process running |
| Independent final review | `/root/final_radar_review` | review returned to main agent | complete: APPROVE |
| Automation-7 production integration runs | `/root` | `../data/applypilot.db` and `../data/reports/` | complete: five serial collections including guarded rerun; no process running |

### Live test contract

- Workdir: `C:/Users/raede/Desktop/简历/applypilot-local/source`.
- Disposable state: `.artifacts/multisource-radar/live/data/applypilot.db`; production `../data/applypilot.db` is out of scope.
- Commands: three separate `python -m applypilot radar collect --company ...` runs; LinkedIn URL generation for AI, product/pre-sales, and spatial/planning across daily/weekly/monthly windows; local lead import; report rendering; direct bounded RSS/Ashby/Lever probes.
- Logs: `official-cli.log`, `query-matrix.log`, `lead-import.log`, `provider-probes.log`, and `daily-report.md` under the live directory.
- Success: only read-only GETs; every source records complete/partial/blocked; different tracks produce distinct URLs; social rows remain leads; report has one job per identity, all source health, exclusions, and unavailable-not-zero semantics.
- Stop conditions: any application/form/message path, any write outside disposable state/logs, repeated rate limiting, or evidence that an endpoint is not an official public source.

### Automation integration live contract

- Owner: `/root`; no subagent may start, poll, retry, stop, or interpret these production-data runs.
- Workdir: `C:/Users/raede/Desktop/简历/applypilot-local/source`.
- Shared state: `../data/applypilot.db`; ApplyPilot's existing local production data is now explicitly in scope for additive Applied/radar records.
- Serial commands: Applied sync, five `radar collect` runs including one discovery-wrapper run, then exact-snapshot `radar report`; no apply command is permitted.
- Durable output: `../data/radar-imports/linkedin-applied-2026-08-24.json` and `../data/reports/daily-radar-2026-08-24.md`.
- Success: 66 Applied IDs import without loss; first official run records source health; later runs add zero duplicate jobs; report preserves unavailable-vs-zero truth.
- Stop: any database integrity failure, application/form/message path, repeated live-provider failure, or write outside the approved ApplyPilot data paths.

## Handoff

Main agent integrates database, CLI, and pipeline boundaries. Workers must not edit existing dirty files. Live tests will use a disposable `APPLYPILOT_DIR` and one owner.

## Next step

The active automation, guarded discovery wrapper, exact Applied snapshot gate, live results, and remaining source-coverage limitations are ready for handoff.
