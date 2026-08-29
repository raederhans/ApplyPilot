# Task

## Current status

Implementation hardening is complete in the isolated worktree and the latest LinkedIn authwall/app-promo findings are independently closed. The complete orchestration, specialist feedback, receipt, LinkedIn route, and bounded performance-observation change set has final full-gate evidence and remains contained on the isolated worktree/branch; it has not been integrated into the dirty parent or pushed. Real connected Edge testing reached the exact HP Workday application login page. No receipt-confirmed live submission has been recorded in this task.

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
- [x] Run supervised real application attempts with receipt accounting; receipt-confirmed applications remain `0`.
- [x] Recognize a same-job LinkedIn authwall before Apply without treating it as an application URL or ATS handoff.
- [x] Dismiss the exact non-application LinkedIn app promo and revalidate target/root/job immediately before Apply.
- [x] Add bounded acquisition, submit-lane, Agent, and observer performance metrics with thread-safe run aggregation.
- [ ] Reconcile uncertain/duplicate outcomes without counting false success.
- [ ] Prepare safe integration handoff without overwriting the dirty parent.

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
| Connected Edge route proof | Signed-in LinkedIn job `4455274411` opened HP Workday requisition `UNI4131-1`; current tab is paused at existing-account login before sending credentials. |

## Open risks and remaining work

- External profile migration must not erase user-specific answers or resume bindings.
- Real providers may expose CAPTCHA, assessment, expired listings, historical duplicates, or rate limits.
- Parent and isolated worktree integration method remains pending until final diff and parent overlap are inspected.
- LinkedIn external route persistence is now on the early marker/observer path and remains guarded by the reservation-time gate. The marker is a control-plane transition, not a submission receipt; its no-fill guarantee still depends on the Agent obeying the immediate host-check prompt and the runtime stopping before the next external form action.
- HP Workday resume-upload visibility needs a focused adapter/runtime check after the early-handoff fix; the first live attempt did not reach a submission gate.
- Two-worker scaling is currently safe only inside one coordinator with independent preparation pages and a single final submit writer. Multiple launcher processes sharing Edge/Cloak profiles remain unsupported.
- Four-worker production scaling is not yet accepted: no matched 1/2/4 cohort exists, and isolated browser identities cannot reproduce the connected Edge login state. The next scaling gate requires the new acquisition/lane/phase metrics plus unchanged receipt, uncertainty, CAPTCHA/login, upload, and observer quality rates.
- Continuing the HP Workday test requires action-time confirmation before transmitting configured login credentials; final Submit requires a second action-time confirmation after form review.
