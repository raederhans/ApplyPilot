# CapyPilot product core

CapyPilot is a private job-search operations workspace. Its product
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

The browser product follows the same four-stage workflow. The **Discovery
Workbench** exposes persisted source truth without collecting or promoting
records:

- official listings, unresolved leads, and listings without radar lineage stay
  visibly distinct;
- the latest persisted provider run reports coverage, pagination, and partial
  status without exposing raw provider errors or payloads;
- source search, evidence filtering, sorting, and bounded pagination remain
  entirely client-side;
- opening a listing or reviewing a lead does not promote, score, authorize, or
  apply to it.

The **Opportunity Workbench** covers Decide with existing read-only data:

- a compact pipeline summary for eligible, ready, scored, and strong-fit roles;
- ranked opportunities with fit evidence and source context;
- score and source-quality views for prioritization;
- client-side search and score filters;
- explicit “Open form” language that does not imply an application was sent.

The **Preparation Workbench** adds a read-only Prepare surface to the same local
page. It shows persisted resume validation, cover-letter resolution, current
resume-route evidence, and recorded gaps without reading material contents or
exposing local paths. A path alone is never treated as validation. The browser
does not route, generate, revalidate, approve, authorize, or submit materials;
the CLI remains authoritative for those actions.

The **Verification Workbench** adds a read-only Verify surface. It keeps batch
reservations, runtime authorization, execution observations, reported platform
state, browser confirmation, and reconciled durable receipts visibly separate.
A reservation is not active authorization; a preview, final click, security
code, or stored status is not a receipt. Runtime-only authorization is never
reconstructed. The CLI remains authoritative for authorization, execution, and
receipt admission.

## Current frontend boundary

- The Python adapter may query existing SQLite contracts and serialize a view model.
- Dashboard generation does not initialize, refresh, or mutate database state; a missing or unreadable database becomes an actionable frontend state.
- The packaged HTML owns layout, responsive behavior, accessibility, URL safety, and client-side filtering.
- The complete ranked dataset remains searchable in the page payload, while the DOM renders at most 60 matching cards at a time.
- Discover, Decide, Prepare, and Verify share one accessible keyboard-navigable workflow, and only the active queue is rendered into the DOM.
- Every stage has isolated search, evidence filters, sorting, pagination, and actionable empty/error states; the URL hash preserves the selected stage without serializing private filter content.
- Suggested CLI commands can be copied locally, but the browser never executes them.
- Prepare serializes status, timestamps, safe filenames, and bounded evidence summaries; resume content, hashes, full local paths, and validation evidence payloads stay out of the page.
- Verify serializes only bounded status, timing, boolean observation, and ledger summaries; raw manifest data, receipt identifiers and text, ledger evidence payloads, local paths, and security codes stay out of the page.
- The frontend uses no remote fonts, scripts, analytics, or network assets.
- Profiles, resumes, credentials, receipt evidence, and unrestricted job data are never included in release archives.
- No database schema, pipeline stage, application decision, or receipt rule is changed as part of the frontend work.

## Product acceptance

A mature product slice should be installable in an isolated environment, expose
one clear command, retain a local-data boundary, distinguish evidence from
claims, and let a user understand the next safe action without reading the code.
The release bundle and four-stage local workbench are the first complete slice
against that standard.
