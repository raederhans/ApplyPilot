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
- The current main checkout is the integration workspace for this upgrade. Preserve all pre-existing or independently owned changes and keep commit/push/live execution out of this wave.

## Stages

- [x] Stage 1: Correct and enforce the application-surface taxonomy.
- [x] Stage 2: Unify manifest/runtime admission and expose effective worker capacity.
- [x] Stage 3: Add deterministic material preflight and exact observer evidence classes.
- [x] Stage 4: Activate the first read-only specialist with journal, reducer, replay, and lifecycle telemetry.
- [x] Stage 5: Add resource-aware scheduling, a process-global submission lane, a database active-writer guard, and reduce unnecessary MCP startup work.
- [ ] Stage 6: Run focused regression, matched concurrency cohorts, and supervised live-application tests. Focused/full regression and connected-route tests are complete; matched 1/2/4 evidence and action-time-confirmed HP continuation remain.
- [ ] Stage 7: Reconcile results, document remaining risks, and prepare a safe integration handoff.
- [x] Stage 8: Restore a hermetic green CI baseline on Python 3.11, 3.12, and 3.13 without requiring Codex CLI or optional CloakBrowser installation during unit tests. The three-file patch passes an isolated `905`-test baseline and portability probes; GitHub Actions run `33304564021` passed Python 3.11/3.12/3.13 plus Windows clean-install smoke on `d9d67129e703ee4ffee6d4236720e7af23ec7dac`.
- [x] Stage 9: Add an Application Actor transition kernel, deterministic autonomy policy, typed human interruptions, and bounded recovery plans by extending the existing control-plane contracts rather than creating a second event model; connect bounded non-submit decisions to the real worker/control-plane paths while retaining legacy write authority. Final combined local gate: `947 passed`; full Ruff passed.
- [ ] Stage 10: Strangle the monolithic worker one vertical slice at a time. Start with prepare/recover, compare legacy and Actor decisions on replay fixtures, and retain the legacy path as the only writer until decision parity is proven.
- [ ] Stage 11: Introduce provider-neutral Agent session continuation, browser-profile leases, and high-level idempotent form operations with page-version preconditions. Do not adopt a new Agent framework until one ATS adapter proves a measured need.
- [ ] Stage 12: Park per-job exceptions without blocking the batch, establish ATS-specific fast paths, and run matched 1/2/4-worker cohorts before changing the supported production worker limit.

## Current implementation audit

| Capability | Current evidence | Verdict | Next acceptance gate |
| --- | --- | --- | --- |
| Single submit writer, reservation, attempt lease, manifest and receipt binding | Real worker and storage callers guard Submit and only admitted receipts may produce `applied`. | LANDED | Preserve unchanged in every later slice. |
| Phase-specific capability registry and structured Agent report | Production `run_job` composes phase/route/state tools and reconciles structured output against the legacy marker. | LANDED, dual-path | Make the versioned structured outcome canonical only after compatibility replay is green. |
| Resource-aware execution plan and in-process DAG | `launcher` consumes the plan; read-only preflight and material specialist run through the coordinator. | LANDED, single-process | Do not claim cross-process scheduling until durable queue/lease recovery exists. |
| Material-readiness and ATS fill-plan specialists | Real production preflight invokes them; configured material mode is `enforce`, and worker control flow consumes the result before browser launch. | LANDED | Extend through the same registry/journal path, not a second specialist system. |
| Generic model-emitted `AgentProposal` execution | Execution exists only when `_agent_proposal_runner` is injected; there is no default production runner or durable generic result. | SCAFFOLD | Add allowlisted kinds, typed payload/effect checks, durable result journal and a real caller. |
| Application events/checkpoints/human requests | Production now persists schema-v1 Actor decisions and typed hard-boundary HumanRequests through the existing control plane, but `latest_checkpoint`, human response loading and resume context are not consumed by production. | PARTIAL / WRITE-ONLY RESUME | Prove request -> response ref -> fresh checkpoint -> same task resume through a real entry. |
| Provider-neutral `AgentRuntime` | Protocol and scripted replay implementation exist; production still launches a fresh ephemeral subprocess per turn. | SCAFFOLD | Ship a production adapter with run/resume/cancel, parent checkpoint binding and unchanged Submit authority. |
| Application Actor and deterministic recovery planner | The pure kernel is called by real `run_job` and worker entries; its envelope is persisted, typed human decisions alter control records, and `retry_new_session` gates the existing one-shot pre-submit browser fallback. | LANDED VERTICAL SLICE | Add checkpoint replay and broader prepare/recover parity without granting page-write or Submit authority. |
| Browser broker/profile pool | CDP locks, per-worker profiles and cleanup exist; no durable broker/lease abstraction or multi-launcher sharing exists. | PARTIAL PRIMITIVES | One owner per page/profile, restart-safe lease, connected/isolated backend identity and page-version preconditions. |
| Exception queue and measured throughput promotion | Jobs can fail/park in existing states, but there is no typed resumable exception queue or matched 1/2/4 cohort. | ABSENT | Batch continues around parked jobs; promote only on receipt-confirmed suitable applications/hour and quality guardrails. |

## Delivery slices and proof

1. **P0 hermetic baseline (complete)** — isolate CLI discovery and optional CloakBrowser from unit-test ambient state; assert the resume route command contract without dependency-version-specific help rendering. Exact regressions, the isolated full suite, portability probes and remote Python 3.11/3.12/3.13 CI are green.
2. **P1 Actor vertical slice (complete locally)** — add versioned state, deterministic autonomy/recovery decisions and typed interruption, then consume bounded non-submit decisions in the real worker/control plane. Direct entry tests distinguish recoverable technical failure from `submission_uncertain`, CAPTCHA, identity, assessment and unsupported-answer hard stops.
3. **P2 canonical runtime/outcome** — adapt the current subprocess runner behind `AgentRuntime`, bind turn/run/attempt/route/evidence, and support checkpointed run/resume/cancel. Proof: killed-process replay resumes without repeated admitted action; structured/legacy disagreement remains fail-closed.
4. **P3 governed specialists and HITL** — register allowlisted proposal kinds, validate payload/effect/authority, journal results, and connect human response references to fresh resume context. Proof: a real proposal and a real human response both change the intended production state while neither gains browser/Submit/ledger authority.
5. **P4 browser resource broker** — introduce durable browser/profile leases and high-level idempotent `observe_form` / `apply_form_patch` / `upload_artifact` / `resolve_validation_errors` operations with page-version checks. Proof: crash/restart, stale-page, profile-conflict and two-worker isolation tests.
6. **P5 adapters, parking and scale** — add ATS-specific semantic adapters, typed exception parking and an operator queue; run matched 1/2/4 cohorts. Proof: no regression in exact-job identity, answer/material truth, single-writer safety, receipt rate or uncertainty rate before raising the worker limit.

## Acceptance criteria

- LinkedIn Apply is explicitly allowed to resolve to either native Easy Apply or a verified current-job employer/ATS handoff without weakening restricted-portal boundaries.
- Manifest construction and runtime acquisition produce the same executable/blocked decision and reason for a frozen candidate snapshot.
- Jobs missing required materials or exhausted attempts are rejected before launching an Agent/browser and receive an explicit terminal or actionable state.
- Pre-submit forms, historical duplicate guards, validation blockers, and decisive receipts cannot be confused by screenshot filenames or Agent assertions.
- At least one production specialist proposal is emitted, executed, durably journaled, consumed by a deterministic reducer, and reflected in telemetry without receiving submit/browser/ledger write authority.
- Requested and effective worker counts are observable; two-worker tests show no page/profile/job cross-talk.
- A live submission is counted only after exact identity plus decisive durable receipt evidence; uncertain or historical duplicate outcomes remain non-success.
- Focused tests and the smallest sufficient shared-contract suite pass from this worktree.
- Clean Linux CI passes the same unit suite on Python 3.11, 3.12, and 3.13; optional runtime integrations are tested through explicit seams or availability skips rather than ambient developer-machine installs.
- Every Actor transition is a pure, replayable decision over versioned state and an existing `ApplicationEvent`; illegal submit/retry transitions fail closed.
- A new contract, planner, adapter, or Agent role is not accepted merely because it imports or passes isolated unit tests. At least one real `run_job`/worker entry must invoke it, a production reducer/policy/state branch must consume its output, and an integration test must prove that its decision changes or blocks the intended control flow.
- Human interruptions are typed and actionable. Recoverable technical failures exhaust a bounded deterministic plan before parking; hard boundaries never become automatic retries.
- The current launcher/worker remains behavior-authoritative until replay comparison has zero unexplained decision drift and no receipt, identity, manifest, or single-writer regression.
- Session/profile reuse never allows two writers to share one page or user-data directory, never changes runtime after submit starts, and survives process restart without repeating an admitted action.
- Throughput promotion uses receipt-confirmed suitable submissions per hour plus quality guardrails; worker count is not raised from telemetry-free intuition.

## Non-goals

- No LangGraph or Temporal migration.
- No multiple writers on one application page.
- No CAPTCHA, assessment, identity, financial, MFA, or unsupported-answer bypass.
- No broad portal-policy relaxation for JobStreet, InternSG, or unverified social/forum leads.
- No force-push, production deployment, or overwrite of the dirty parent worktree.
- No big-bang rewrite of `launcher.py` or `worker_orchestration.py`.
- No duplicate policy, event, checkpoint, human-request, task-journal, or receipt source of truth.
- No immediate full migration to OpenAI Agents SDK, LangGraph, Temporal, or a peer-to-peer Agent swarm.

## Risks and constraints

- Real tests share the parent data root, SQLite database, evidence directory, browser caches, and external provider state.
- Agent/runtime latency is stochastic; throughput claims require paired evidence rather than one successful retry.
- Existing code is substantial recent WIP; changes must reuse current abstractions and avoid parallel edits to the same files.
- Live test execution has one owner: `/root`. Subagents may inspect completed logs and static code only.
- The 2026-08-30 shared-chat review is an architectural hypothesis source, not repository truth; current code, tests, durable ledgers, and exact remote CI logs take precedence.
- P0 and the Actor vertical slice run in separate user-visible Codex tasks with disjoint ownership. The Actor task may edit one bounded non-submit launcher/worker seam, but neither task may commit, push, publish, run live submissions, change ledger/Submit authority, or edit this task record.
