# ApplyPilot Local product core

ApplyPilot Local is a private job-search operations workspace. Its product
promise is not “submit more forms”; it is “move each opportunity forward with
truthful materials, explicit authority, and durable evidence.”

## Four core functions

| Stage | User question | Product responsibility | Existing contract |
| --- | --- | --- | --- |
| Discover | Which roles are real and current? | Collect official openings, optional board results, and manual leads without collapsing source trust | Radar observations, provider states, canonical job identity |
| Decide | Which roles deserve time? | Check eligibility, enrich descriptions, score fit, and expose the reasoning needed for review | Eligibility SQL, fit score, readiness decisions |
| Prepare | What truthful material should I use? | Route validated resume variants, identify evidence gaps, tailor content, and verify generated files | Resume library, immutable artifacts, PDF validation |
| Verify | What was authorized and what actually happened? | Separate preview from authorization, isolate browser work, preserve uncertainty, and admit only decisive receipts | Batch manifests, application decisions, receipt reconciliation |

Discovery creates candidates; it does not create applications. Opening a form
is not authorization, and clicking Submit is not proof of acceptance. Those
boundaries are the core of the product rather than implementation detail.

## Frontend information architecture

The browser product follows the same four-stage workflow. The first delivered
surface is the **Opportunity Workbench**, which covers Discover and Decide with
existing read-only data:

- a compact pipeline summary for eligible, ready, scored, and strong-fit roles;
- ranked opportunities with fit evidence and source context;
- score and source-quality views for prioritization;
- client-side search and score filters;
- explicit “Open form” language that does not imply an application was sent.

The next frontend surfaces should expose Prepare and Verify by adapting existing
contracts, not by creating parallel status logic in the browser. Until that work
is deliberately scheduled, the CLI remains the authoritative interface for
resume routing, authorization, application execution, and receipt admission.

## Current frontend boundary

- The Python adapter may query existing SQLite contracts and serialize a view model.
- The packaged HTML owns layout, responsive behavior, accessibility, URL safety, and client-side filtering.
- The frontend uses no remote fonts, scripts, analytics, or network assets.
- Profiles, resumes, credentials, receipt evidence, and unrestricted job data are never included in release archives.
- No database schema, pipeline stage, application decision, or receipt rule is changed as part of the frontend work.

## Product acceptance

A mature product slice should be installable in an isolated environment, expose
one clear command, retain a local-data boundary, distinguish evidence from
claims, and let a user understand the next safe action without reading the code.
The release bundle and Opportunity Workbench are the first slice against that
standard.
