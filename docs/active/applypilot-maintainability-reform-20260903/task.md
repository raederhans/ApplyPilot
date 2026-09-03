# Task status

## Phase I — complete (local-only)

- [x] Read repository rules, inspect HEAD/worktree and existing active records.
- [x] Inspect prompt, targeted tests, pytest configuration, and CI workflow.
- [x] Remove unreachable CapSolver guidance while retaining fail-closed CAPTCHA guidance.
- [x] Make targeted candidate runtime Prompt facts profile/policy driven and add modified-profile regression coverage.
- [x] Merge the redundant CapyPilot branding test into distribution/view coverage; convert selected source scans to behavior checks.
- [x] Add browser/compatibility/windows pytest tiers and split CI responsibilities.
- [x] Run Phase I selection: prompt/profile, changed behavior contracts, workflow parse/contract, collect-only, and scoped Ruff.

### Phase I evidence

- Focused selection: `53 passed in 4.95s` across profile availability, Prompt/CAPTCHA, direct-email fact rendering, mapping/provenance, merged branding/view coverage, episode boundary, synthetic-browser report contract, and CI workflow contract tests.
- Tier collection: core `1486`, browser `7`, compatibility/windows `227`; `1720` total collected exactly once across the three expressions.
- `ruff check` over every changed Python file passed; `ci.yml` parsed as YAML; `git diff --check` passed for tracked changes.

## Phase II — complete (local-only)

- [x] Remove the broad compatibility ownership from `test_local_compat.py`; keep full platform-independent core on Python 3.12 and define a narrow 3.11/3.13 compatibility subset.
- [x] Keep Chromium browser tests and Windows-only profile-lock tests in their dedicated lanes; make release smoke depend on core, compatibility, and browser gates.
- [x] Replace CLI apply's module service locator and 20-option command boundary with `ApplyCommandRuntime` and immutable `ApplyCommandOptions`.
- [x] Replace worker core's `ModuleType`/56-name validator with six responsibility-grouped runtime port objects and immutable `WorkerRunOptions`; keep launcher as the compatibility composition root.
- [x] Evaluate attempt identity/browser binding migration and stop at the recorded design boundary because current mutation ownership would create dual state.
- [x] Run command/worker/actor/SubmissionGate selections, marker collection, scoped Ruff, and diff checks without executing live or full-suite routes.

### Phase II local evidence

- Focused command/runtime/safety selection: `320 passed in 11.30s` across apply command contracts, worker runtime contracts, ApplicationActor, SubmissionGate, runtime settings, CI contract, optional dependency compatibility, and distribution compatibility.
- Additional fail-closed worker selection: `9 passed in 1.71s`; adapter/marker smoke selection: `29 passed in 1.92s`.
- Collection ownership: total `1720`; Python 3.12 core `1683`; 3.11/3.13 compatibility subset `19`; Chromium `7`; Windows-only `30`. Compatibility is intentionally included in the Python 3.12 core run.
- No full execution, real Chromium, live ATS, Submit, mailbox action, remote CI, commit, push, release, or deployment was performed.

## Phase III — complete (local-only)

- [x] Inventory provider/platform host and capability duplication without creating a supported-provider union.
- [x] Add capability-scoped typed provider descriptors and migrate ATS detection, control write, semantic resume/upload, application episode, credential relay, and LinkedIn Python host policy.
- [x] Extract trusted page surfaces, LinkedIn causal handoff/login, and post-submit observation into public modules while retaining compatibility forwards.
- [x] Replace launcher use of migrated private `page_observation` functions with public module calls.
- [x] Split six observation functions from `WorkerApplicationPorts` into `WorkerPageObservationPorts`; keep launcher as the composition root.
- [x] Audit every production `_browser_lease_binding` write/remove point and defer centralization because mutation is not yet single-owner; do not add dual state or serialize submit authority.
- [x] Run provider, observation, worker, SubmissionGate, uncertain-receipt, Ruff, compile, and diff checks without live or full-suite routes.

### Phase III local evidence

- Before/after: `page_observation.py` 3,255 -> 1,866 lines; provider capability fact sources 7 -> 1 registry; launcher private aliases 20 -> 11 and direct private calls 2 -> 0; `WorkerApplicationPorts` 20 -> 14 fields plus a six-field observation port.
- Main selection: `487 passed in 28.73s`; supplementary semantic-browser/admission/stateful-control/repair selection: `104 passed in 18.57s`.
- Unmigrated: pre-submit answer/policy/audit remains in the facade; browser lease install/refresh/repair/heartbeat/release remains a single job-mapping fact with multiple mutation sites; browser-injected LinkedIn JavaScript retains its local hostname predicate because it executes outside Python registry scope.
- No full 1,720-test execution, real Chromium, live ATS, Submit, mailbox action, remote CI, build, release, commit, push, or deployment was performed.

## Integration follow-up — locally qualified

- [x] Push the combined reform commit `2a09332480bf8832b665b22f980a25d5a851fbd9` to `fork/main` after Ruff, compile, `1691` core tests, and `30` Windows tests passed locally.
- [x] Diagnose CI run `33713251268`: Python 3.11/3.13 compatibility and the original Chromium tier passed, but core exposed 23 real-Chromium cases that were not owned by the browser marker; Windows correctly remained blocked by the failed upstream gate.
- [x] Mark only the real Chromium test functions in the five mixed-responsibility files, leaving their pure contract tests in core.
- [x] Validate the corrected partition: `1728` total, `1668` core, `19` compatibility, `30` browser, and `30` Windows selections; the complete browser tier passed locally (`30 passed`).
- The follow-up commit requires a fresh remote CI run before the integration owner may report the remote gate as passing.
