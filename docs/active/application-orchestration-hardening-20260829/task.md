# Task

## Current status

The P0 and P1 implementation slices are complete in the current working tree. P0 removes the three ambient-environment assumptions behind the current Linux matrix failures; an isolated baseline snapshot with only those three test patches passes `905` tests. P1 adds an Application Actor/Policy/Recovery kernel and two bounded production consumers: `run_job` persists the typed decision into the existing event/checkpoint and HumanRequest control plane, while the real worker requires `retry_new_session` before the existing one-shot Edge-to-Cloak fallback. The final combined tree passes `947` tests and full Ruff. The remote Python 3.11/3.12/3.13 matrix has not been rerun, and the legacy path retains all browser, ledger, write, and Submit authority. The two implementation tasks did not commit or push; `/root` is the sole integration owner. No framework migration, worker-count increase, or live submission occurred.

## Checklist

- [x] Define and enforce application-surface taxonomy with LinkedIn entry, native, and verified external routes.
- [x] Share one frozen-candidate admission decision between authorization and acquisition.
- [x] Report requested, executable, blocked, and effective workers.
- [x] Reject missing materials and exhausted attempts before Agent/browser startup.
- [x] Classify post-submit evidence independently of filenames and Agent assertions, including receipt/duplicate conflicts.
- [x] Implement one bounded production specialist and deterministic reducer.
- [x] Persist journal/idempotency/replay and proposal lifecycle telemetry.
- [x] Add resource-aware scheduling, final-submit serialization, and safe MCP startup reductions.
- [x] Run focused regression and shared-contract validation.
- [x] Run a controlled two-worker fill-only dry-run under one coordinator; no batch reservation or receipt was produced.
- [x] Run supervised real application attempts with receipt accounting; HP `UNI4131` produced one target-ATS-confirmed, locally reconciled browser receipt.
- [x] Recognize a same-job LinkedIn authwall before Apply without treating it as an application URL or ATS handoff.
- [x] Dismiss the exact non-application LinkedIn app promo and revalidate target/root/job immediately before Apply.
- [x] Add bounded acquisition, submit-lane, Agent, and observer performance metrics with thread-safe run aggregation.
- [x] Split ordinary ATS sign-in, credential relay, account creation, and existing Google-session reuse into separately auditable authentication capabilities; persist the user's standing login authorization without weakening material-answer or submit gates.
- [x] Harden Workday segmented-date guidance and localized boolean resolution after the connected-browser form exposed both failures.
- [x] Record the HP live-run rule that target-ATS state and durable receipts outrank third-party extension status; no extension status is application proof.
- [x] Record exact-scope user-confirmed `No` answers for the HP government-employment and conflict/government-influence questions, without extrapolating them to other screening questions.
- [x] Require calendar interaction when a Workday segmented/composite date control exposes a calendar; the typed fallback corrupted a live year value before calendar recovery.
- [x] Record standing authorization for routine final Submit after all exact-job/material/preflight/submission gates pass, without weakening CAPTCHA/assessment/identity/financial/unsupported-answer/submission-uncertain stops.
- [ ] Reconcile uncertain/duplicate outcomes without counting false success.
- [ ] Prepare safe integration handoff without overwriting the dirty parent.
- [ ] Confirm the P0 patch on remote Python 3.11/3.12/3.13 CI. The exact fixes and local portability gates are complete; no push or remote rerun was authorized in this wave.
- [x] Add and test a pure Application Actor transition kernel with typed autonomy decisions, human interruptions, bounded recovery actions, and schema-v1 durable parsing.
- [x] Connect bounded non-submit production paths to the new Actor/Policy/Recovery result and prove through real `run_job` and worker entries that the result is consumed; no dead import, default-off no-op flag, or log-only scaffold is accepted.
- [x] Prove the two slices compose without duplicating existing events, checkpoints, task journal, human requests, failure taxonomy, or submission authority.
- [ ] Define replay comparison for the first prepare/recover production migration; do not wire it until P0 is green.
- [ ] Design provider-neutral session and browser-profile leases plus high-level page-versioned operations after Actor replay parity exists.
- [ ] Run matched throughput cohorts only after session/profile continuity and exception parking are implemented; retain two-worker production cap meanwhile.

## Validation evidence

| Command or check | Result |
| --- | --- |
| Initial worktree status | Clean at `d53ae6e` |
| Isolated-worktree focused orchestration/runtime/submission baseline | `118 passed in 5.27s` at `d53ae6e` |
| Historical duplicate evidence tests | `6 passed`; positive receipt, duplicate wording, uncertain form, no retry/repair/count, and neutral screenshot coverage |
| Runtime contract after evidence change | `84 passed`; combined evidence/runtime run `90 passed in 2.83s` |
| Package registry check | Playwright MCP `0.0.79`; Gmail MCP `1.1.11` |
| Independent evidence review | `REQUEST CHANGES`; reproduced receipt-vs-duplicate conflict and non-success retention metadata misclassification |
| Policy/admission hardening | `243 passed`; Ruff passed; committed as `da0f410` after canonical policy commit `75d3a6f` |
| Orchestration focused suite | `48 passed in 3.68s` |
| Material/evidence regression | `33 passed, 72 deselected` |
| Admission/gate/worker regression | `28 passed, 78 deselected` |
| Broad shared-contract suite | `407 passed in 17.05s` after updating schema expectations for the two durable journal tables |
| Touched Python lint | Ruff `All checks passed` |
| Patch integrity | `git diff --check` exit 0; only checkout line-ending warnings |
| Live status semantics | Raw prepared `8`; canonical admission `2` after HP/Shopee material projection; CLI now shows both instead of calling the raw value ready |
| Infineon interrupted test attempt | Recovered `1` pre-submit attempt, `0` submission-uncertain; `submit_started=0`, no gate/consumption/receipt, no application counted |
| HP LinkedIn-to-Workday preparation | Automatic `reuse_exact`, required coverage `1.0`, score `0.957143`; projected machine-validated artifact |
| Shopee LinkedIn-to-official preparation | Explicit qualified tie selection, required coverage `1.0`, score `0.95`; projected the matching product-management artifact |
| Keppel preparation gate | Blocked: required named skill `machine learning` unsupported; no projection or browser run |
| Grab strict revalidation | Blocked as `failed_judge` for unsupported/inflated claims; same byte content must be invalidated in the reusable artifact library |
| Isolated full regression | `744 passed in 24.84s` with `APPLYPILOT_DIR=C:\Users\raede\AppData\Local\Temp\applypilot-tests-20260829-p0p2` |
| Final isolated full regression | `876 passed in 54.51s` with the dedicated test data root; full Ruff passed; `git diff --check` passed with line-ending warnings only. |
| Full Ruff and patch integrity | Ruff `All checks passed`; `git diff --check` passed with line-ending warnings only |
| HP live LinkedIn entry test | LinkedIn Apply reached HP Workday, credential relay ran, and resume upload was attempted twice; no submit gate or receipt. Failed pre-submit after 228 seconds with `resume_upload`, showing early external handoff persistence currently occurs too late. |
| LinkedIn three-path and early-handoff checkpoint | `linkedin_apply_entry` resolves through native Easy Apply or `linkedin_to_official_ats`; prompt emits the early `LINKEDIN_EXTERNAL_HANDOFF` marker, worker observes the bound external page, sanitizes/rebinds the URL, releases the stale attempt, and does not reserve or count a submission. |
| Codex App Browser login bridge | App Browser reaches the LinkedIn login page, but no supported session/tool bridge currently transfers that authenticated state into the isolated Playwright/Codex worker; login continuity remains manual-review blocked. |
| Controlled two-worker dry-run | One coordinator exercised two isolated preparation workers with final-submit capacity one. It used fill-only preview semantics, created no batch reservation, and produced `0` receipt-confirmed applications; the run was slower than the single-worker baseline. |
| Fill-only preview contract | Preview can populate and inspect the real form but cannot click final Submit/Send/Finish or send email. The launcher restores preview state and keeps `previewed`, authorization, final action, and receipt-confirmed counts distinct. |
| Runtime performance telemetry | Per-turn evidence captures `process_spawn_ms`, `turn_setup_ms`, `prompt_build_ms`, `first_output_ms`, `first_tool_ms`, `last_tool_ms`, `tool_call_count`, and `unique_tool_count`; these are the comparison fields for future worker-scaling tests. |
| LinkedIn authwall/app-promo hardening | `34 passed`; post-Dismiss job-drift regression fails closed before login inspection, Apply, or attestation; independent re-review `ACCEPT`. |
| Focused performance-observation regression | `344 passed in 24.30s`; touched Ruff `All checks passed`. Empty/acquired attempts have separate counts, and a controlled clock proves two lane-hold segments total exactly once before terminal-attempt metrics are backfilled. |
| Independent performance-observation re-review | `ACCEPT`; acquired/empty/blocked/error attempts are separately observable, terminal lane metrics retain the complete hold, and telemetry remains advisory to authorization, receipt admission, and single-writer decisions. |
| Isolated v3/v4 login tests | Both reached the expected login-only surface and stopped at Google identifier with `LOGIN_ISSUE`; neither clicked a second Apply, filled an ATS form, submitted, or created a receipt. |
| Connected Edge route proof | Signed-in LinkedIn job `4455274411` opened exact HP Workday requisition `UNI4131-1`; this proved the company-site path before the later authenticated continuation. |
| Standing login authorization rule | Private runtime profile enables ordinary ATS sign-in, credential relay, account creation, and existing Google-session reuse. Versioned example defaults all four capabilities to `false`; explicit new keys override the legacy account-creation fallback. Final focused compatibility suite: `179 passed in 16.12s`; touched Ruff and `git diff --check` passed. |
| Connected Edge HP continuation | Existing Workday credentials were reused without exposing the password. Steps 1 and 2 were saved, exact resume artifact `resume-9a6170ac3be39d3ad0cdc849.pdf` was uploaded, and step 3 retained only evidence-backed answers. No final submit or receipt occurred. |
| Workday date/localization hardening | Bulk fill now explicitly excludes Workday segmented/composite dates; calendar or verified per-segment entry owns them. Boolean resolution recognizes bounded Chinese labels such as `是`, `否`, `不是`, `同意`, `不同意`, and `不适用` while preserving the visible option and the confirmed-fact gate. Combined focused suites: `212 passed in 14.36s`; touched Ruff and `git diff --check` passed. |
| HP supervised continuation | Official HP ATS showed `UNI4131-1` as an unsubmitted draft while Simplify claimed an Aug 30 application; the official state governs and no receipt exists. Edge restored the existing login through saved credentials without password inspection. Steps 3-4 used the user's exact-scope government-employment/conflict confirmations, then the browser reached 6/6 Review; no final Submit was clicked. |
| Workday segmented-date live evidence | Per-segment typed fallback lost focus and corrupted the graduation year to `2031`; calendar navigation restored the intended value. Where the control exposes a calendar, use it exclusively and verify the visible result before continuing. |
| HP final submission and receipt reconciliation | Submit clicked exactly once. HP Workday showed `已提交申请` / `您的申请已成功提交`, routed to `/jobTasks/completed/application`, and listed `UNI4131` as `In Progress`. `reconcile-receipts` changed the exact LinkedIn job row to `applied` with evidence `browser_receipt:hp-workday-UNI4131-completed-application-20260830T1029+0800` and confidence `durable_receipt_reconciled`. |
| Current remote CI baseline | GitHub Actions run `33296366807`: Windows clean-install smoke passed; Python 3.11, 3.12, and 3.13 each failed the same three tests with `3 failed, 902 passed`. |
| Shared-chat extraction and repository reconciliation | Full assistant proposal was read from the exact user-shared Edge tab. Local main shows several recommended primitives already implemented, so the upgrade plan is incremental rather than a wholesale rewrite. |
| Parallel execution dispatch | P0 task `01a051b6-3da7-7db1-9a4c-379847e5c34d` and Actor vertical-slice task `01a051b6-6fef-7d71-88a3-2836d602f2ec` completed with no commit/push/live-run authority. |
| P0 isolated baseline proof | Starting from `1da96ad`, applying only the three P0 test patches produced `905 passed in 34.22s`; full Ruff passed. Additional probes passed without `cloakbrowser`, without Codex on `PATH`, and under Typer `0.27.2`. |
| Actor production-consumption gate | `106 passed` over pure actor, real `run_job`, worker fallback, submit-once, receipt uncertainty, control-plane and HUMAN_ONLY cases; scoped Ruff and `git diff --check` passed. |
| Final combined working tree | Pre-push rerun `.venv\\Scripts\\python.exe -m pytest -q`: `947 passed in 31.57s`; `.venv\\Scripts\\python.exe -m ruff check .`: `All checks passed!`; `git diff --check`: exit 0 with line-ending warnings only. |

## Open risks and remaining work

- External profile migration must not erase user-specific answers or resume bindings.
- Real providers may expose CAPTCHA, assessment, expired listings, historical duplicates, or rate limits.
- The P0/P1 diff is integrated in the sole registered `main` worktree and targets `fork/main`; remote CI remains a separate post-push gate.
- LinkedIn external route persistence is now on the early marker/observer path and remains guarded by the reservation-time gate. The marker is a control-plane transition, not a submission receipt; its no-fill guarantee still depends on the Agent obeying the immediate host-check prompt and the runtime stopping before the next external form action.
- HP Workday resume upload now has connected-browser proof, but the live segmented-date corruption shows the calendar-mandatory rule must be enforced in the adapter/runtime before unattended Workday scaling. Localized yes/no labels remain bounded to confirmed facts and exact visible options.
- Two-worker scaling is currently safe only inside one coordinator with independent preparation pages and a single final submit writer. Multiple launcher processes sharing Edge/Cloak profiles remain unsupported.
- Four-worker production scaling is not yet accepted: no matched 1/2/4 cohort exists, and isolated browser identities cannot reproduce the connected Edge login state. The next scaling gate requires the new acquisition/lane/phase metrics plus unchanged receipt, uncertainty, CAPTCHA/login, upload, and observer quality rates.
- Ordinary first-party ATS sign-in, existing authenticated sessions, configured credential relay, already-signed-in Google basic identity/email SSO, and routine final Submit after all configured gates pass no longer require repeated confirmation. CAPTCHA, MFA/security challenges, recovery, abnormal OAuth scopes, identity/financial material, unsupported material answers, and submission-uncertain states remain separate stop conditions.
- The new Actor kernel is genuinely consumed by control-plane persistence/HumanRequest projection and the bounded pre-submit browser fallback decision. It is still only a vertical slice: checkpoint replay, same-task resume, durable exception dispatch and a production `AgentRuntime` adapter remain unimplemented.
- Clean local Python 3.12 evidence will not by itself prove the Python 3.11/3.13 GitHub matrix; remote CI remains a separate integration boundary.
