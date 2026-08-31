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

## M3 execution wave (2026-08-30)

- **M3-A durable identity and checkpoint idempotency — complete locally.** A stable attempt-scoped `actor_id`, distinct per-call `turn_id`, backward-readable DecisionEnvelope v2/upcaster, actor-scoped monotonic checkpoint CAS, deterministic completion/HUMAN_ONLY replay, and a real `run_job` -> launcher -> database consumer are now implemented. An independent review also forced SAVEPOINT rollback for caller-owned transactions. Fresh-turn resume remains a separate slice; browser, ledger, page-write and Submit authority are unchanged.
- **M3-B Windows truth gate — complete locally, remote pending.** Windows Python 3.12 now runs the core pytest suite and the wheel clean-install smoke executes the installed `applypilot resume-route --help`; the Linux 3.11/3.12/3.13 matrix remains unchanged. Local Windows evidence is `947 passed`, but `windows-latest` remains unproven until a later authorized push.
- **Integration owner — `/root`.** Review both task outputs in the shared checkout, reject scaffold-only APIs, run the smallest shared-contract gate followed by the full isolated suite when warranted, and decide whether M3 is ready for the separate fresh-turn resume slice. No live browser/ATS run, worker-cap increase, commit or push is authorized in this wave.

M3-A is accepted only if replay of the same completed turn is a no-op even when a regenerated timestamp differs, stale actor versions fail closed, schema-v1 state remains readable without gaining authority, and a production entry plus integration test consumes the new identity/checkpoint contract. Passing isolated dataclass or storage tests alone is insufficient.

## Six-application multi-channel live wave (2026-08-30)

- **Goal:** produce six new, independently confirmed applications from the existing local library, distributed across multiple source/application surfaces. A browser click, queued email, saved draft or Agent assertion does not count; each slot closes only with exact-job durable receipt evidence or a verified direct-email Sent copy with the expected attachment.
- **Selection:** exclude every `applied`, `submission_uncertain`, expired, duplicate/repost, exhausted-attempt, materially unsupported or stale-identity record. Rank by current fit and material readiness, then preserve source diversity. The initial queue may use Greenhouse, LinkedIn-to-official ATS, official company careers, Lever and another admitted official source; pre-submit blockers are replaced from the same audited queue rather than bypassed.
- **Execution:** `/root` is the sole live-process, browser, SQLite and Submit owner. Run one exact URL at a time with one worker and a fresh short-lived standing manifest. Re-observe page/job/material identity immediately before Submit, reconcile the receipt immediately after each completion, and never repeat Submit when the receipt state is uncertain.
- **Authorized routine actions:** ordinary ATS sign-in/account creation, configured credential relay, existing Google-session reuse, exact resume/cover upload, truthful routine fields, mailbox verification within the configured account, and final Submit after all gates pass.
- **Hard pauses:** CAPTCHA/manual relay, assessment, MFA/security recovery, identity/financial/biometric material, abnormal OAuth permissions, unsupported material/legal answers, wrong/stale job or page, and `submission_uncertain`. These consume no success slot and do not authorize a repeated Submit.
- **Stop condition:** six receipt-confirmed applications, or the admitted library is exhausted by hard pauses/unsupported material. Production worker capacity remains one for this test even though the configured cap is two.

## Current implementation audit

| Capability | Current evidence | Verdict | Next acceptance gate |
| --- | --- | --- | --- |
| Single submit writer, reservation, attempt lease, manifest and receipt binding | Real worker and storage callers guard Submit and only admitted receipts may produce `applied`. | LANDED | Preserve unchanged in every later slice. |
| Phase-specific capability registry and structured Agent report | Production `run_job` composes phase/route/state tools and reconciles structured output against the legacy marker. | LANDED, dual-path | Make the versioned structured outcome canonical only after compatibility replay is green. |
| Resource-aware execution plan and in-process DAG | `launcher` consumes the plan; read-only preflight and material specialist run through the coordinator. | LANDED, single-process | Do not claim cross-process scheduling until durable queue/lease recovery exists. |
| Material-readiness and ATS fill-plan specialists | Real production preflight invokes them; configured material mode is `enforce`, and worker control flow consumes the result before browser launch. | LANDED | Extend through the same registry/journal path, not a second specialist system. |
| Generic model-emitted `AgentProposal` execution | Execution exists only when `_agent_proposal_runner` is injected; there is no default production runner or durable generic result. | SCAFFOLD | Add allowlisted kinds, typed payload/effect checks, durable result journal and a real caller. |
| Application events/checkpoints/human requests | Production writes a schema-v1 compatibility mirror plus native schema-v2 durable identity through the existing control plane. Completion checkpoints now use actor-scoped monotonic CAS, deterministic replay and atomic event/checkpoint/HumanRequest persistence; `run_job` directly exercises this path. Production still does not consume checkpoint state or HumanResponse for a fresh turn. | LANDED DURABLE WRITE SLICE / RESUME ABSENT | Prove request -> response ref -> parent-bound fresh turn -> higher checkpoint -> same task resume through a real entry. |
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
