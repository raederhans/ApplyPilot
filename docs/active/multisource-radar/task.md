# Task

## Current status

The standalone radar and its integration into the existing daily `automation-7` are complete and independently approved. The Applied gate was re-verified against 66 records across seven pages, the production data path was exercised five times, and the automation remains active on its original schedule.

## Checklist

- [x] Record user goal, scope, safety boundary, and existing dirty state.
- [x] Map database, CLI, configuration, dedupe, and location-filter integration points.
- [x] Implement radar schema and unified ingestion functions.
- [x] Implement official-source adapters and Singapore watchlist.
- [x] Implement LinkedIn URL/query queue, taxonomy, promotion, and report logic.
- [x] Integrate radar CLI without entering application stages.
- [x] Add focused migration, adapter, promotion, dedupe, reporting, and CLI tests.
- [x] Run bounded real official-source tests in multiple directions.
- [x] Run multiple LinkedIn query-generation/browser-visible scenarios without crawling or side effects.
- [x] Refine code and configuration from test evidence.
- [x] Run final regression suite and document remaining gaps.
- [x] Inspect automation-7 without creating a duplicate schedule.
- [x] Re-read the full logged-in LinkedIn Applied set before integration testing.
- [x] Import the verified Applied exclusion set into ApplyPilot production data.
- [x] Update automation-7 with the ApplyPilot radar sequence and narrowed write boundary.
- [x] Run and compare at least three serial real radar collections.
- [x] Apply evidence-led refinements and rerun focused/full regression checks.
- [x] Bind report publication to the exact snapshot ID returned by the current Applied sync.
- [x] Add a discovery-only wrapper that blocks application commands and constrains file capabilities.
- [x] Remove `.env` loading and application-output directory creation from radar bootstrap.
- [x] Make the unattended automation generate LinkedIn review URLs without opening or importing posts.
- [x] Obtain post-hardening independent contract/security approval.

## Validation evidence

| Command or check | Result |
| --- | --- |
| `git status --short` before edits | Existing dirty files recorded; no cleanup or reset performed. |
| `pytest -q tests/test_official_discovery.py tests/test_radar.py tests/test_radar_database.py` | 52 passed after classification, fresh-run-only lead reconciliation, lineage rendering, pagination guards, report, and dry-run refinements. |
| Focused legacy compatibility and portal-policy selection | 2 passed, 107 deselected; JobSpy company/source regression repaired. |
| Fresh isolated official-source matrix | Databricks 10, Stripe 5, Grab 3, OpenAI 5, MongoDB 3, Datadog 2; Cloudflare and Anthropic complete with 0 accepted; ST Engineering partial/latest-only. |
| Same-source rerun on Databricks and Stripe | 0 new, 10+5 existing; one retained job per identity. |
| Candidate-reviewed LinkedIn lead import | 3 leads, 0 jobs created from the social source. |
| Exact official-link reconciliation | Imported one social lead pointing to an existing OpenAI URL; 1 promoted, jobs remained 28, one secondary source link retained, final report showed the job once and omitted the promoted lead from awaiting verification. |
| Fresh-run promotion chain in a new isolated database | Lead import first returned `promoted: 0`; a subsequent live OpenAI Ashby refresh read 753 listings, accepted 5, and promoted exactly 1 matching lead from that current run. |
| Visible LinkedIn content search | Complex Boolean prompts: 4/4 zero; simplified AI/product/pre-sales/planning prompts: 4/4 visible-result pages with correct latest/time filters. |
| Inactive Palantir safety check | Live `--include-inactive` blocked with exit 2; `--dry-run` inspection succeeded with no bootstrap/write. |
| `ruff` targeted full rules plus `E9,F` on legacy integration files | All checks passed. |
| `python -m compileall -q src/applypilot` | Passed. |
| `pytest -q` | 161 passed. |
| Independent final read-only review | APPROVE; no blocking findings after stale-listing and multi-source-lineage fixes. |
| Logged-in LinkedIn Applied refresh | 66 unique records across seven actual pages; enriched title, company, location, URL/job ID export marked complete only after count reconciliation. |
| Production Applied synchronization | 66 LinkedIn rows updated with no loss; complete/fresh exclusion snapshot recorded. |
| First production `radar collect` | 28 new verified official candidates across Databricks, Stripe, Grab, OpenAI, MongoDB, and Datadog; Cloudflare/Anthropic complete-zero; ST Engineering partial/latest-only. |
| Second and third production reruns | Every accepted listing resolved as existing and produced zero duplicates. |
| Fourth production run after seniority-policy refinement | 16 current candidates, all existing: product/pre-sales 11, data 3, AI 2, spatial unavailable because official coverage is incomplete. |
| LinkedIn visible-query comparison | Plain and quoted prompts were globally noisy; `#hiring + exact role + #singaporejobs` produced more Singapore-specific results. No weak/ineligible post was imported. |
| Automation update and read-back | Updated `automation-7` in place; active status, daily 09:30 schedule, model, project, and both working directories were preserved. |
| Pre-hardening full regression | `pytest -q`: 167 passed; `compileall`: passed. Scoped radar lint passed after import ordering cleanup. |
| Repository-wide lint | `ruff check src tests` remains non-green with 120 broader findings; no unrelated bulk cleanup was performed. |
| Final production database audit | `PRAGMA quick_check`: ok; 133 jobs, 74 applied records, 45 recorded radar runs, two Applied snapshots. |
| Runtime guard negative/positive checks | `run-radar.ps1 apply` blocked before ApplyPilot; report output outside `data/reports` blocked; a spatial past-week query queue succeeded. |
| Bound production Applied sync | 0 inserted, 66 updated, 0 skipped; complete snapshot `e82c8ea9444446bc856fa3196668579a` recorded from seven pages. |
| Fifth production collection through discovery wrapper | 16 accepted current official rows, all existing; zero duplicate jobs; ST Engineering remained explicitly partial. |
| Bound production report | Exact current snapshot ID accepted and embedded in the report; a wrong ID returned non-zero and created no output file. |
| Duplicate-option adversarial checks | Repeated `--output`, `--source-id`, and `--file` all failed in the wrapper before Python; Python also validates the final parsed source identity and extensions. |
| Post-hardening full regression | `pytest -q`: 175 passed; scoped full-rule radar lint and CLI `E9,F,I` passed; `compileall` passed. |
| Post-hardening independent reviews | Automation contract: APPROVE; discovery guard/security: APPROVE after the repeated-option override was closed. |

## Open risks and remaining work

- LinkedIn remains candidate-visible and non-exhaustive; no unattended post crawl or completeness claim is implemented.
- ST Engineering exposes only a latest-items feed, so it permanently reports partial coverage.
- Official title matching is deliberately conservative and may miss novel titles until the shared vocabulary is updated.
- Clearly senior/lead titles are excluded by the radar policy, but some unlabelled Solutions Architect and enterprise pre-sales roles may still be too experienced; the daily automation performs the final resume/eligibility review rather than publishing all discovery rows.
- A social lead imported after an official run deliberately waits for the next fresh official refresh before promotion.
- Current spatial/planning output remains unavailable rather than zero because ST Engineering is latest-only and no verified spatial item appeared in the current window.
- The discovery wrapper is a capability guard for the scheduled workflow, not an operating-system sandbox; a process with unrestricted shell access could still bypass it by invoking another executable directly.
