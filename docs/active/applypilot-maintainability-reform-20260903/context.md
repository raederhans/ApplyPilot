# Context

## 2026-09-03 — Phase I start

- Base: `main` at `a12a21d6f40cdfec6a24e9f00b75f3d4a433d32e`; starting worktree was clean.
- Repository `AGENTS.md` requires the local wrapper/workspace for operational ApplyPilot commands and preserves CAPTCHA, assessment, identity/financial, unsupported-answer, and uncertain-receipt hard pauses. Phase I runs no operational route.
- Direct inspection found `_build_captcha_section()` returned the fail-closed helper before an unreachable CapSolver implementation. The old code was never emitted but kept an obsolete operational instruction and an unused `os` import.
- Candidate literals were found in runtime Prompt rendering, including location, direct-email availability, and pre-submit review text. Product-level Singapore radar defaults are outside this task and are intentionally unchanged.
- Existing CI executed `python -m pytest -q` on every Linux matrix entry and Windows, and installed Chromium on every one of those jobs.

## Decisions

- Render runtime candidate location from `personal.city/province_state/country`; direct-email availability/work-auth wording reads the current `availability` and `work_authorization` values.
- Keep location-specific profile keys compatible while making their human-facing Prompt wording geography-neutral.
- Preserve behavior assertions for browser safety and episode boundaries; remove only source-text scans whose runtime/contract behavior is already asserted.
- Treat the Windows clean-install job as the publish/release gate, and make it depend on core matrix and Chromium tier completion.

## Handoff boundary

Phase II must correct the local tier ownership before relying on remote timing: broad compatibility marking must not remove platform-independent safety contracts from the main Python version. It must not claim remote CI, release publication, live provider behavior, or production safety changes from local checks.

## 2026-09-03 — Phase I local completion

- Prompt/profile and protected-boundary selection passed: `53 passed in 4.95s`.
- Pytest collection is a complete non-overlapping partition under the new markers: core `1486`, browser `7`, compatibility/windows `227`, for `1720` collected tests.
- Scoped Ruff passed for all changed Python files. `ci.yml` parsed successfully and `tests/test_ci_workflow_contract.py` was included in the 53-test selection.
- No full execution (1720 tests), Chromium launch, live browser, ATS, submission, mail action, remote CI, commit, push, or release was performed.

## 2026-09-03 — Phase II boundary decisions

- The whole-file `compatibility` mark on `tests/test_local_compat.py` moved 179 platform-independent tests, including worker submission gates and uncertain-receipt behavior, out of Linux core and into Windows 3.12. The corrected topology runs the full non-browser/non-Windows suite on Python 3.12, a 19-test compatibility subset on Python 3.11/3.13, seven Chromium tests in the browser lane, and 30 Windows-only tests on Windows.
- `commands/apply.py` used the entire `applypilot.cli` module as a service locator and accepted 20 independent command options. Phase II replaces that boundary with `ApplyCommandRuntime` and immutable `ApplyCommandOptions`; the CLI adapter resolves current monkeypatch-compatible globals at call time and preserves Typer exit codes.
- `worker_orchestration.py` declared 56 required names and directly read 68 runtime attributes from a `ModuleType`. Phase II replaces the module/string contract with `WorkerRuntimePorts`, six responsibility groups, and immutable `WorkerRunOptions`; `launcher.py` owns the compatibility composition.
- Attempt/browser binding is still mutated in launcher-owned job mappings and consumed across worker, provenance, episode, operator, and broker paths. Creating a second mutable `AttemptRuntimeContext` now would require dual writes or move authorization-bearing lease data without a single owner. Phase II therefore records the boundary rather than implementing it. Phase III must first centralize binding mutation behind one broker-owned API, then an ephemeral typed context may be derived without becoming a second source of truth.

## Phase III inputs

- Split provider-specific LinkedIn/ATS routing and page-observation/audit operations out of the compatibility adapter behind the Phase II `WorkerApplicationPorts` and `WorkerBrowserPorts` groups.
- Centralize `_browser_lease_binding` install/heartbeat/release mutation in one broker-owned boundary before introducing `AttemptRuntimeContext`; do not serialize submit authority or weaken stale-page/attempt checks.
- Use authorized remote CI evidence only to tune duration/flakiness after the corrected local ownership contract is preserved.

## 2026-09-03 — Phase III provider and observation boundaries

### Measured starting point

- `page_observation.py` was 3,255 lines and combined answer/audit policy, trusted surface selection, LinkedIn causal Apply/login/handoff, pre-submit audit, post-submit independent observation, and receipt-evidence comparison.
- Seven independent provider capability facts existed as hard-coded tables or equivalent membership/match branches: ordinary ATS detection, control write, semantic resume host detection, semantic upload admission, semantic browser admission, application episode admission, and credential-relay known ATS hosts. LinkedIn host checks were additionally repeated inside its observation implementation.
- Launcher had 20 private `page_observation` alias assignments plus two direct private calls. `WorkerApplicationPorts` had 20 fields, including six unrelated page-observation functions.

### Implemented boundary

- `provider_registry.py` is the one typed provider fact registry. Each `ProviderDescriptor` declares separate host rules for `detection`, `semantic_upload`, `control_write`, `credential_relay`, and `linkedin_external_handoff`, plus the `application_episode` flag. Callers request one capability; no supported-provider union exists.
- `page_surfaces.py` owns trusted same-origin surface selection and immutable target-lineage filtering. `linkedin_page_observation.py` owns LinkedIn job identity, one-click causal attestation, post-login guard, and bounded external handoff observation. `post_submit_observation.py` owns independent browser observation, historical-duplicate classification, and model/observer evidence consistency.
- `page_observation.py` is now 1,866 lines. It keeps compatibility aliases and the still-coupled answer validation, CAPTCHA/verification checks, ATS fill-plan snapshot, and pre-submit audit. Extracted modules are 269, 890, and 356 lines respectively; the split is responsibility-based rather than a line-count partition.
- Launcher private `page_observation` aliases fell from 20 to 11; direct private calls fell from two to zero. `WorkerApplicationPorts` fell from 20 to 14 fields, while six observation functions moved to `WorkerPageObservationPorts`.

### Browser lease binding decision

- Centralization is deferred because production writes remain in three modules and cover distinct fail-closed transitions. Current write/remove points are launcher `_release_application_browser_authority` (`launcher.py:528`), `_heartbeat_operator_handoff` (`:575`), `_browser_lease_for_agent_turn` (`:920`), `_refresh_semantic_browser_bundle` (`:951`), `_complete_observed_semantic_resume_effect` (`:1089`), `_try_semantic_pre_submit_repair` (`:1265`, `:1374`, `:1427`), and `run_job` heartbeat/release (`:5873`, `:6001`); worker repair adoption is `_consume_provenance_repair_artifacts` (`worker_orchestration.py:292`); the isolated synthetic P4 path is `run_p4_no_submit_worker` (`p4_no_submit_worker.py:337`).
- A later migration requires one broker-owned typed mutation API covering install, refresh/epoch CAS, repair adoption, heartbeat, and release; every caller must have direct stale-attempt/page tests before the mapping writes are removed in one change. Until then, `_browser_lease_binding` remains the only mutable fact, no `AttemptRuntimeContext` is dual-written, and no submit authorization capability is serialized.

### Phase III local evidence

- Main provider/observation/runtime/safety selection: `487 passed in 28.73s`.
- Supplementary semantic-browser/admission/stateful-control/repair selection: `104 passed in 18.57s`.
- Changed-file Ruff and compile checks passed. No real Chromium, live ATS, Submit, email, remote CI, full 1,720-test run, build, release, commit, push, or deployment was performed.
