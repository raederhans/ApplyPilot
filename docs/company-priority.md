# Two-week company priority

This is an enabled soft ranking policy, not an eligibility or authorization rule.
The original 1–10 `fit_score`, application statuses and submission gates remain
unchanged. Broad standing-authorization selection and worker job acquisition rank
by `application_priority_score` before selecting candidates. Explicit exact-job
requests take priority over this soft policy. Discovery continues to retain leads;
unscored leads are not excluded by employer history.

## Policy

* Within one reviewed company/region/role-family scope: at least three distinct
  confirmed applications in 14 days, at least two explicitly rejected, and no
  advancement in that cohort: 25% penalty.
* At least four distinct applications submitted 30–60 days ago, no rejection or
  advancement for those applications, and no advancement in that pool's preceding
  60 days: 20% penalty, only after a complete feedback review covering 60 days.
* Hold for seven elapsed days, then linearly recover over seven days. At fourteen
  elapsed days the penalty is zero. Do not add penalties together.
* Unknown scope: half penalty. Known unrelated scopes: no spillover. Company
  identity only normalizes case/whitespace; do not infer shared pools from an ATS
  provider, related brand name or parent-company ownership.
* Human recruiter contact/interview/explicit advancement (`progress`) or user
  override (`reset`) clears that scope and unknown-scope fallback immediately.
  An empty scope on those events explicitly means company-wide. All prior evidence
  in the cleared pool is consumed; old rejections cannot immediately retrigger it.
* An uncertain/selectivity-unknown assessment invitation halves the current
  penalty once. Explicit invitation to a particular job exempts only that job for
  fourteen days. Automatic receipts, job alerts and default In Process statuses
  are not progress events.
* Event replay consumes the cohort that triggered each episode. Expiry never
  creates a new episode by itself. New evidence is required.

## Feedback ingestion and activation

No new background monitor or mailbox classifier is installed. At the start of a
batch, the coordinating agent reviews relevant available email/ATS updates and
imports only unambiguous, evidence-backed events. Then use the preview command.
Existing mail monitors may use the same import command when their authorized work
finds new feedback. If access/history is incomplete, do not create `coverage`.
No recorded reply does not establish no reply.

`run.ps1 company-priority-import --file reviewed-events.json`

The file is a JSON array. Each event has `event_id` (stable source observation ID),
`company`, `scope` (for example `sg:ai-data-intern`, or empty if unknown), `kind`,
`occurred_at` (ISO timestamp with timezone), and `evidence` (mail ID, portal record
or explicit user instruction with a concise reason). Use event time, not sync time.
Do not put credentials or complete mail bodies in this ledger.

Supported kinds:

| Kind | Additional fields | Evidence requirement |
|---|---|---|
| submitted | application_id, job_url | Existing jobs row must be applied with a matching date/company |
| rejected | application_id | Earlier submitted event in the same scope; explicit rejection |
| assessment | application_id | Earlier submitted event; invitation, selectivity unknown |
| progress | optional application_id | Human contact/interview/explicit advancement |
| reset | none | Explicit user priority override |
| invited | application_id, job_url | Exact local job, matching company, explicit invitation |
| coverage | complete: true, covered_since | Relevant feedback sources actually reviewed over preceding 60 days |
| scope | job_url | Exact local job and reviewed country/role-family classification |

`application_id` is the employer's canonical requisition identity, not a LinkedIn
or Indeed listing ID when multiple listings point to the same requisition. The
same application is counted once even if multiple evidence events are imported.
Import is atomic; identical event IDs are idempotent and conflicting IDs rejected.
Do not fabricate IDs or feedback to activate a penalty. Scope events also let the
agent classify new candidates without changing jobs or fit scores.

`run.ps1 company-priority-preview --limit 20`

Returns original fit, adjusted priority, reason, trigger ID and recovery date.
`--at 2026-10-05T00:00:00+00:00` simulates a date without changing feedback or jobs.
Normal ranking recomputes expiry on every run; no scheduled expiry job is needed.
Preview is not a submission or readiness check. Check the best remaining penalized
opportunities before finishing a batch, especially approaching deadlines, without
requiring a quota. Resume wording changes alone do not clear a penalty.
