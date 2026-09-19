# Resume library curation

Prefer a healthy existing variant, then reorder or patch it for a material evidence gap. The active library has a ceiling of 50 variants, not a growth target. Routing retains its coverage and relevance gates and records the reason for a proposed patch/new variant. At the configured `max_variants` ceiling, routes requiring another variant go to review for consolidation or replacement. Existing exact reuse remains available.

Use `data/resume-library/CATALOG.md` or `catalog.json` to select by evidence family, actual page count and intended use. One-page and two-page variants should differ in useful depth; changing a job title or summary alone does not justify a separate active version.

## JD scoring and routing evaluation (2026-09-19)

Candidate fit (1–10, configured admission floor) and resume coverage (0–1) are separate decisions. The routing score is not an interview probability. Explicit low, failed, stale or malformed assessments must not authorize editing. `tailor-job` now uses the profile floor instead of bypassing it with zero.

New score evidence binds the JD fingerprint, selected source text/path, confirmed profile facts and scoring prompt revision. Exact-job scoring also binds all supplemental evidence actually supplied to the model. Changes to bound inputs require rescoring at route time. Each scoring entry point checks those inputs again before writing a result, so a concurrent JD change or newer assessment cannot be overwritten by an older run. JD changes invalidate the current score/tailor projection, cover-letter approval and application-readiness review for unsubmitted jobs; submitted and uncertain attempts retain their records. Scores predating input bindings remain legacy evidence, not retroactively proven current. Different selected/scored content is reported as `score_alignment`; candidate-wide fit must not be presented as a score of the final attachment.

Ranking version `resume-ranking-v2` uses reviewed library families ahead of inherited track labels, normalizes sentence punctuation and limited noun plurals, and distinguishes several missing AI/research/spatial/data-service titles and GPU-stack requirements. Named missing required skills prevent direct reuse even when a master source can support adding them. Explicit one-/two-page resume requirements filter candidates; a maximum of two pages still allows one page. Unknown page metadata cannot prove compliance. Family labels currently match the curated catalogue and must be updated alongside it if renamed.

Replay without live data writes or model calls:

```powershell
python tools/evaluate_resume_routes.py --workspace ../data --output ../data/reports/my-route-replay
```

The tool backs the read-only live DB into memory and redirects route reports to the output directory. `--score-repeat 2` explicitly enables real configured model calls for six selected cases; load the intended workspace environment before using it. `--score-results <live-scores.json>` feeds saved real assessments back through routing without calling the provider again. The September 19 evidence is under `../data/reports/routing-evaluation-20260919/README.md`.

## Content, layout and evidence

- Content identity derives from normalized resume text. A new PDF for the same text creates a separate immutable render and updates the current render pointer without erasing history.
- `resume-library/renders/<content-id>/<pdf-hash>/` holds frozen text/PDF pairs and immutable validation evidence. Imported layouts without provenance are `legacy-unknown`.
- Each `resume-runs/<timestamp-uuid>/` stores input, source snapshots, `generation.json` and `validation.json` separately. Generation records include the renderer fingerprint. Failed attempts keep their own evidence.
- Revalidation creates a new run; submitted, applied and uncertain-submission jobs cannot have their historical attachments replaced by this path.
- Superseded editorial variants remain available for history and are excluded from future selection. Historical sync must not reactivate them or roll back their current successors.

## Editorial policy

Experience entries stay newest/current first by dates; older relevant entries can have more detail. Projects follow relevance and need not be chronological. A summary is optional. Add projects only from actual source evidence, with accurate personal ownership. One or two readable pages is acceptable; do not omit useful projects solely to fit one page.

The source-inspired template uses Arial, black text, thin rules and right-aligned dates. Main body defaults to 10.5pt with 1.35 line height; compact rendering is at least 10pt with 1.25 line height and 0.5in margins. Inspect rendered pages and extracted reading order, not just page count.

## Review and revision propagation

`applypilot resume-library-review` reports current health and possible editorial improvements. Source-byte changes, changed profile facts actually used in text, missing evidence or broken TXT/PDF bindings require review before reuse. This is deliberately a lightweight dependency check, not a semantic guarantee that every paraphrase of a changed fact is detected. Maintain the human candidate-facts document and runtime profile separately.

Review affected variants, make source-grounded corrections, validate and render them, then promote the reviewed successor. Do not bulk rewrite every variant, edit source originals, or replace attachments in past applications. Layout-only changes can retain content identity; content changes retain a parent/successor relationship.

## Batch curation

With `APPLYPILOT_DIR` set to the intended data directory:

```powershell
python tools/curate_resume_library.py stage --report-dir <report-directory>
python tools/curate_resume_library.py promote --report-dir <report-directory>
```

Stage is read-only against the live DB and writes proposals to distinct run directories. Optional `editorial-refinements.json` maps parent IDs to explicitly reviewed refinements: omitted summary, exact source project selection/consolidation, precise summary replacements, layout options, or evidence refresh. `--only <parent-id>` restages one candidate while retaining the manifest order.

`reviewed_replacements` supports exact, uniquely matched passages with a reason and source evidence for each edit. `selected-artifacts.json` can limit a batch to explicitly chosen parents. A reviewed `merge-plan.json` lists donor/destination IDs, donor TXT/PDF hashes and reasons. Promotion resolves destinations to their reviewed successors, requires healthy active destinations, preserves historical donor bytes, and retires duplicate donors in the same transaction. Inherited coverage is provenance, not a new exact-JD validation result.

Promotion requires `visual-review.json` with an `accepted` array containing each parent ID and SHA-256 of the exact reviewed TXT and PDF. Reviewers must inspect every accepted final rendering. Promotion verifies source and historical-file identity, backs up SQLite, validates health, records final evidence, and transactionally preserves job rows. It rechecks the staged parent state under a write transaction and rejects a changed/retired parent or a retired successor; old manifests without the parent snapshot must be staged and reviewed again. New files can remain if a transaction fails; they are not proof of promotion. Read `promotion.json` and current DB state for completion.

Editorial validation preserves inherited evidence and checks explicit source-grounded changes. It is not a fresh independent factual audit of every legacy statement. Past overwritten reports cannot be reconstructed; unknown provenance must remain unknown.
