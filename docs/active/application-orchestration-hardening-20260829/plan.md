# Plan

## Goal

Make ApplyPilot's submission-surface policy, candidate admission, evidence classification, specialist orchestration, feedback persistence, and cross-application concurrency executable and measurable while preserving receipt proof and the single-page-writer invariant.

## Scope

- Restore LinkedIn's Apply entry as a first-class route that may resolve at runtime to native Easy Apply or the exact employer/ATS application page.
- Unify authorization-manifest and runtime-acquisition eligibility, including attempt exhaustion and retry/risk blocks.
- Add deterministic pre-browser material checks and precise post-submit evidence categories.
- Activate one bounded read-only specialist through a production runner, deterministic reducer, durable journal, replay/idempotency, and proposal lifecycle telemetry.
- Report effective concurrency and schedule workers against explicit resource capacity.
- Reduce avoidable MCP/process overhead where existing runtime boundaries allow it safely.
- Run focused tests, controlled two-worker smoke tests, and supervised real applications when all gates pass.

## Sources of truth

- Runtime/domain contracts in `src/applypilot/apply/`, `src/applypilot/storage/`, and `src/applypilot/database.py`.
- Submission policy in the external ApplyPilot data-root `profile.json`; repository defaults and schemas remain the versioned contract.
- Existing tests under `tests/`, live application evidence, application gates, and durable receipt reconciliation.
- The dirty parent worktree is user-owned; this isolated worktree is the implementation owner.

## Stages

- [x] Stage 1: Correct and enforce the application-surface taxonomy.
- [x] Stage 2: Unify manifest/runtime admission and expose effective worker capacity.
- [x] Stage 3: Add deterministic material preflight and exact observer evidence classes.
- [x] Stage 4: Activate the first read-only specialist with journal, reducer, replay, and lifecycle telemetry.
- [x] Stage 5: Add resource-aware scheduling, a process-global submission lane, a database active-writer guard, and reduce unnecessary MCP startup work.
- [ ] Stage 6: Run focused regression, matched concurrency cohorts, and supervised live-application tests. Focused/full regression and connected-route tests are complete; matched 1/2/4 evidence and action-time-confirmed HP continuation remain.
- [ ] Stage 7: Reconcile results, document remaining risks, and prepare a safe integration handoff.

## Acceptance criteria

- LinkedIn Apply is explicitly allowed to resolve to either native Easy Apply or a verified current-job employer/ATS handoff without weakening restricted-portal boundaries.
- Manifest construction and runtime acquisition produce the same executable/blocked decision and reason for a frozen candidate snapshot.
- Jobs missing required materials or exhausted attempts are rejected before launching an Agent/browser and receive an explicit terminal or actionable state.
- Pre-submit forms, historical duplicate guards, validation blockers, and decisive receipts cannot be confused by screenshot filenames or Agent assertions.
- At least one production specialist proposal is emitted, executed, durably journaled, consumed by a deterministic reducer, and reflected in telemetry without receiving submit/browser/ledger write authority.
- Requested and effective worker counts are observable; two-worker tests show no page/profile/job cross-talk.
- A live submission is counted only after exact identity plus decisive durable receipt evidence; uncertain or historical duplicate outcomes remain non-success.
- Focused tests and the smallest sufficient shared-contract suite pass from this worktree.

## Non-goals

- No LangGraph or Temporal migration.
- No multiple writers on one application page.
- No CAPTCHA, assessment, identity, financial, MFA, or unsupported-answer bypass.
- No broad portal-policy relaxation for JobStreet, InternSG, or unverified social/forum leads.
- No force-push, production deployment, or overwrite of the dirty parent worktree.

## Risks and constraints

- Real tests share the parent data root, SQLite database, evidence directory, browser caches, and external provider state.
- Agent/runtime latency is stochastic; throughput claims require paired evidence rather than one successful retry.
- Existing code is substantial recent WIP; changes must reuse current abstractions and avoid parallel edits to the same files.
- Live test execution has one owner: `/root`. Subagents may inspect completed logs and static code only.
