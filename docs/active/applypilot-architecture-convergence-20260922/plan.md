# ApplyPilot architecture convergence — 2026-09-22

## Goal

Complete architecture plan items 1–5 without weakening ApplyPilot's safety contract: decompose launcher and worker orchestration, converge final-submit authority, unify suitable SQLite transaction helpers, and introduce an explicit ordered schema-migration boundary.

## Scope

1. Keep `launcher.py` as the composition root while extracting responsibility-owned application stages.
2. Ensure every production-capable final-submit path reaches one SubmissionGate/reservation/receipt authority boundary.
3. Begin the `worker_orchestration.py` decomposition at the Runtime Cell
   shadow-session boundary. Lifecycle, entry-turn, recovery, and telemetry stay
   in the existing owner until later bounded passes; this tranche does not
   claim that the entire worker state machine has been split.
4. Adopt `storage/transactions.py` for compatible transaction sites while preserving bespoke boundaries where semantics differ.
5. Separate schema bootstrap from ordered, versioned migrations without operating on the user's production database.

## Sources of truth

- Repository code and tests at base `6e23d067d798794b74ce22079b3a10f8b25e313e`.
- `docs/runtime-job-boundaries.md` for the immediately preceding refactor contracts.
- Existing SubmissionGate, browser authority, receipt, and uncertain-submission behavior tests.
- User request in the current task authorizing implementation of plan items 1–5 with repeated CLI delegation.

## Stages

- [x] Stage 0: map boundaries, transaction ownership, submit paths, and exact acceptance tests.
- [x] Stage 1: extract the first launcher-owned stages while retaining compatibility and composition-root wiring.
- [x] Stage 2: converge and behavior-test the production final-submit authority path.
- [x] Stage 3: extract the Runtime Cell shadow-session owner behind existing typed ports/options.
- [x] Stage 4: migrate compatible transaction sites to the shared helper in bounded domain slices.
- [x] Stage 5: add schema versioning and ordered idempotent migrations; separate bootstrap from upgrade work.
- [ ] Stage 6: run scoped and broad verification, review the complete diff, commit, push, integrate, and safely synchronize the main checkout.

## Acceptance criteria

- `launcher.py` and `worker_orchestration.py` retain their public/monkeypatch-compatible entry contracts but no longer own the extracted implementation logic.
- No new production submit path exists; all equivalent effects require the same SubmissionGate, reservation, stale-attempt/page checks, and receipt handling.
- `submission_uncertain` still prohibits automatic replay and Runtime Cell production admission remains disabled.
- Shared transaction helpers preserve owned commit, nested savepoint, cancellation, and rollback semantics; externally coupled paths remain explicitly bespoke.
- A fresh database bootstraps at the current schema version; an older supported database migrates exactly once; rerunning initialization is idempotent; a newer unknown schema fails closed.
- Targeted tests, Ruff, compile checks, the non-browser/non-Windows suite, browser tier, compatibility lanes, Windows checks, and packaging/CLI verification are either passed or any unavailable lane is reported explicitly before integration.
- User and unrelated worktree changes are preserved.

## Non-goals

- No LangGraph, Temporal, Redis, queue service, microservice split, or new agent framework.
- No worker-count increase, browser-policy change, live ATS submission, mailbox action, production database migration, credential change, deployment, or release.
- No broad test-topology redesign; tests change only where needed to preserve behavior while removing implementation-shape coupling.
- No removal of compatibility facades until all affected call sites and monkeypatch contracts have migrated.

## Risks and constraints

- `run_job()` and `_worker_loop_with_port()` carry large closure-like mutable state; extraction must not duplicate authority-bearing state.
- Tests and external callers may monkeypatch names on `launcher` or `worker_orchestration`; owner moves need compatibility seams.
- SQLite transactions cannot make browser, filesystem, or network effects atomic.
- Existing migration logic includes triggers and historical backfills; versioning must not silently skip or repeat them.
- An initial Runtime Cell timing assertion was flaky in CI before passing on rerun; refactors must not mask timing failures by weakening assertions.
