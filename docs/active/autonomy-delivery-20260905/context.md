# Context

## Current truth

- Follow-up authorization (2026-09-05): user requested several user-visible comparison-test tasks, local optimizations based on results, real submissions if qualification passes, then merge and push. Prior modest-budget preference remains: normal 5-10-job runs, a small live cohort, no broad paid benchmark.
- Root remains sole owner of real application data/browser/Submit, full regression, commits, merge and push. Independent fixture tests may own separate temporary browser profiles/ports/DBs. No shared live browser access by delegates.
- Target push remote freshly confirmed as fork (raederhans/ApplyPilot); origin is upstream Pickle-Pixel and is not the delivery destination.

- User authorized implementation with user-visible tasks and subagents, with model/effort selected by root according to task needs.
- Main source at C:/Users/raede/Desktop/简历/applypilot-local/source is clean, HEAD 2d1323e.
- Existing remaining-architecture worktree is clean, HEAD ad81f1c; preserved unchanged.
- Integration worktree: C:/Users/raede/.codex/worktrees/applypilot-autonomy-delivery-20260905, branch codex/autonomy-delivery-20260905, base ad81f1c.
- Source/main and actual data/profile/materials remain outside delegated write ownership.

## Decisions and deviations

| Time | Evidence or decision | Impact |
| --- | --- | --- |
| 2026-09-05 | Existing local candidate has useful unmerged work; use it as the new isolated base. | Avoid reimplementing or dropping prior changes. |
| 2026-09-05 | Static Supervisor has two-second stall windows and CLI lacks steer. | Prioritize waiting-state reproduction and repair before runtime canaries. |
| 2026-09-05 | Browser CLI effort is high by default; App Server effort is not consistently forwarded. | Align contracts and measurement without changing user defaults. |

## Live process ownership

Root owns all combined/full tests, builds and any real browser, Codex, MCP or live application process. Delegates may run only short isolated unit tests with their own temporary APPLYPILOT_DIR and PYTHONPATH pointing at their own src. No shared test process has been started.

Root combined check 1 (2026-09-05): cwd=C:/Users/raede/.codex/worktrees/applypilot-autonomy-delivery-20260905. Executable=C:/Users/raede/Desktop/简历/applypilot-local/.venv/Scripts/python.exe. Environment: PYTHONPATH=<cwd>/src; APPLYPILOT_DIR=C:/Users/raede/AppData/Local/Temp/applypilot-autonomy-root-check1. Command: python -m pytest -q tests/test_application_supervisor_loop.py tests/test_launcher_durable_runtime.py tests/test_apply_capabilities.py tests/test_runtime_settings.py tests/test_runtime_cell.py tests/test_codex_app_server.py tests/test_app_server_runtime_wiring.py tests/test_apply_submission_contract.py tests/test_submission_admission.py tests/test_material_specialist.py tests/test_recipe_experience.py tests/test_recipe_experience_wiring.py tests/test_provider_recipe_shadow.py. Output log=<APPLYPILOT_DIR>/combined.log. Root sole owner; only local mocks/temp SQLite/files. Success=all selected tests pass; stop on process completion or unexpected external process/live-state dependency, no real models/browser. Session/process ID will be recorded from the command result.

## Handoff

- Comparison runtime: 01a06faf-09fe-7d73-ac4c-162fa644c705, gpt-5.6-sol/medium, worktree applypilot-autonomy-runtime-compare-20260905 at a7cb610; owns isolated event/config comparison and necessary runtime-module fixes, not launcher.
- Comparison ATS: 01a06faf-16f4-7b32-a9c8-05b2b77d370e, gpt-5.6-sol/high, worktree applypilot-autonomy-ats-compare-20260905 at a7cb610; owns synthetic browser profile, optional CDP 9556, fixture lifecycle and routine helper/adapter fixes, no live accounts or real ATS.
- Comparison batch/material: 01a06faf-26fc-77f0-8508-b633e8293a34, gpt-5.6-terra/medium, worktree applypilot-autonomy-batch-compare-20260905 at a7cb610; owns isolated batch/material comparison and narrow fixes, not launcher/CLI wiring.
- Root verified installed entry currently resolves source/main; live candidate must explicitly inherit PYTHONPATH=<integration>/src through the operational run.ps1 wrapper. Read-only candidate scan found usable browser cases; actual resolved PDFs were independently extracted and checked without publishing applicant facts. Receipt counts checked before choosing live canaries.

- Supervisor task 01a06f8b-e75d-7871-bd0f-2dc0c97c1a4f, gpt-5.6-sol/high; owns Supervisor loop, launcher supervision/events region and target tests.
- Material task 01a06f8b-f363-7243-9520-22d37223b076, gpt-5.6-terra/high; owns readiness/admission/application_jobs and target tests; no launcher edits.
- Runtime task 01a06f8b-fd14-7ec3-9d8b-648dec5a4a53, gpt-5.6-sol/high; owns agent_runtime, codex_app_server, runtime_cell, app_server_runtime_wiring, runtime_settings and target tests; no launcher edits.
- Internal code-mapper /root/batch_context_map owns read-only batch/context mapping.
- Delegates do not commit, merge, push, clean up, or touch live data. Root integrates their completed patches sequentially and resolves shared callers.

## Previous implementation wave (historical)

Readiness (6 files) and runtime configuration (10 files) have been applied to integration by root. Delegate narrow checks passed; root integration checks remain pending. Supervisor is finishing. Internal recipe experience and ATS first-prepare helper implementations are underway, with real caller wiring required before acceptance.

The user tightened scope: normal runs are 5-10 applications, 100 is rare, and budget should stay modest. The batch task 01a06f98-ef6b-7c21-aee0-b8432c2aa897 (gpt-5.6-sol/high, isolated applypilot-autonomy-batch-20260905) has been instructed to deliver only lightweight durable snapshot/next guidance, not large-batch orchestration. No real-model A/B or real submission is planned in this wave.

Root completed Supervisor integration, shared runtime configuration wiring, routine prepare caller, consumed-job acquisition guard and focused integrated verification. Current task is locally complete within the user's tightened budget scope; task.md records activation and qualification boundaries. No remote changes and no running delegate work remains.

Root combined check 2: same integration cwd and Python executable, PYTHONPATH=<cwd>/src, APPLYPILOT_DIR=C:/Users/raede/AppData/Local/Temp/applypilot-autonomy-root-check2. Command: python -m pytest -q tests/test_prepare_fast_path.py tests/test_prepare_fast_path_launcher.py tests/test_semantic_batch_production_wiring.py tests/test_semantic_batch_runtime.py tests/test_ats_fill_plan_repair_loop.py tests/test_apply_runtime_contract.py tests/test_apply_submission_contract.py tests/test_batch_progress.py tests/test_launcher_durable_runtime.py. Log=<APPLYPILOT_DIR>/combined.log. Root owns this combined mocked/no-network check. Success=all pass; stop on completion or any unexpected real service dependency. No user data or paid provider calls.

Combined check 2 completed, session 82569, exit 0: 367 passed in 25.34s. All modified/new Python files passed Ruff after import/unused-import cleanup and preserving the runtime configuration ValueError contract. Final narrow follow-up (same check2 environment): python -m pytest -q tests/test_batch_progress.py tests/test_recipe_experience.py; log=<APPLYPILOT_DIR>/final-narrow.log. This verifies the added partial-manifest capacity case and the malformed stored-controls TypeError adjustment, with the same success/stop/no-network rules.

## Comparison follow-up: integration and live qualification

The user's follow-up supersedes the prior local-only closeout. Implementation baseline is committed as a7cb610. Runtime fixture comparison completed; material comparison fix is integrated, with a root follow-up that classifies an empty extracted PDF as unavailable rather than fresh. ATS browser comparison is finishing a narrow descriptor-binding fix.

Root full local regression contract: cwd is the integration worktree above; executable is the existing workspace .venv/Scripts/python.exe; PYTHONPATH=<integration>/src, PYTHONUTF8=1, APPLYPILOT_DIR=C:/Users/raede/AppData/Local/Temp/applypilot-autonomy-qualification-20260905. Command: python -m pytest -q -m "not browser". Log=<APPLYPILOT_DIR>/core.log. Root is sole process owner. No real application data or paid model calls; stop on completion or any unexpected live dependency. ATS changes arriving afterward receive their affected unit and browser checks separately.

Root live canary contract, after qualifying affected tests: invoke the original operational run.ps1 with pwsh 7, explicitly inherited PYTHONPATH=<integration>/src and PYTHONUTF8=1. Use exact job URL, workers=1, limit=1, min-score=8, Codex Sol/high and the configured persistent ApplyPilot Edge profile. Root owns the browser, logs, ledger and all real Submit actions. Begin with a small cohort from admitted SmartRecruiters/Workday/custom careers cases; no repeated submission for A/B. Keep existing preflight, facts, SubmissionGate and receipt reconciliation. Stop each case on a verified receipt, normal bounded failure, manual hard gate or submission_uncertain; never force retry an uncertain effect. Private logs live outside Git under the workspace data directory. Fixture A/B supplies controlled comparison; distinct real jobs supply integration evidence only.

Full local regression session 60664 completed: 2253 passed, 2 failed, 34 deselected in 96.22s. Both failures were stale-PDF tests using invalid placeholder PDF bytes plus a TXT sidecar. They now create parseable PDFs with conflicting fresh TXT sidecars, exercising the actual upload predicate. Affected authorization/admission files passed 99 tests in 8.53s afterward.

ATS comparison completed and its adapter/test/fixture/report files are integrated. Root affected-check contract: same isolated qualification environment and Python, command `python -m pytest -q tests/test_prepare_fast_path.py tests/test_prepare_fast_path_launcher.py tests/test_semantic_batch_adapter.py tests/test_semantic_batch_runtime.py tests/test_semantic_batch_adapter_chromium.py`, log=<qualification>/ats.log. Browser tests launch their own disposable headless browser and intercept all fixture requests, with no live profile or CDP override. Then run Ruff and `python scripts/build_release.py` in this worktree; root owns completion/cleanup, release artifacts remain local ignored output. Success means each command exits zero and archive privacy audit passes.

Live case 1 started via pwsh 7 with semantic batch mode off, process session 21286, owned CDP 55928 and browser launch PID 53852. Existing application receipt count was zero; new attempt remained prepare/submit_started=0 at the first ledger observation. Private log is data/autonomy-canary-20260905/ncs-off.log. The live process has semantic batch disabled; the subsequent isolated adapter patch does not activate a new path in this case.

User steering during the live run: choose companies not previously applied to instead of repeatedly using familiar employers. Root stopped the exact owned NCS Codex process tree (PID 64584, verified parent 67056); the wrapper completed normal cleanup. NCS final ledger is prepare/submit_started=0/failed, exact-job receipt count=0, wrapper wall time 646.5s. First prepare turn took 482378ms and timed out; its allowed same-page recovery was stopped for the user's company-selection change after 145003ms. This is an interrupted canary, not a completed A/B or submission. The next cohort must be checked at company level across historical attempts/receipts/job states, with no old-employer resubmissions. The batch comparison task is doing that bounded read-only inventory; root retains all mutation/browser ownership.

Temasek first canary: session 90264, attempt-f4af4c61-df3a-4e09-b7b6-accb8372de0c, CDP 63469. Exact pre-submit attempt was finalized failed through the existing ledger API with root-cancellation evidence; heartbeat then ended the operator wait and wrapper cleaned the browser (exit 0). No durable human answer was fabricated. One retry session 70614 uses the repaired EU provider registry, CDP 51526, launch PID 34784, attempt-0ad341cc-633d-43a4-b5df-a84692a72cb4. Same exact job, one worker, Sol/medium prepare, default high submit, ordinary gates unchanged. Root retains sole ownership. Final non-browser regression session 33278 passed 2267 tests; provider alias applied afterward passed 27 affected tests. Bounded review requested one additional parser clause fix; only batch task owns that delegated correction.

All live wrapper sessions are finished with zero verified submissions. Read-only reproduction invalidated the initial claim that the second Temasek failure was caused by a missing email selector: both old and new helpers match the complete live form. The report explicitly corrects that inference. Generated Codex MCP env_vars omitted candidate PYTHONPATH, and isolated imports demonstrated main versus candidate source selection; the runtime comparison task supplied a narrow fix, integrated by root. Final affected runtime checks and rebuild are owned by root session 3591, same isolated qualification environment. Success requires target tests, Ruff and build/privacy audit passing; no paid/live process is part of this gate.

Delivery checkpoint: code commit 18dcb0bfe614fbc581c93686a4e11e1afcfa229e passed local qualification and was fast-forwarded into clean source/main, then pushed to personal fork/main. git ls-remote returned the identical code commit. All test/build/live sessions described above are completed, and no application is counted without a receipt. Retained worktrees are deliberate recovery copies. Root monitors the final main CI after this documentation-only follow-up.
