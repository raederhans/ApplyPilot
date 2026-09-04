# Round 2 runtime-control plan (P0-P4)

## Goal

Turn the existing shadow/default-off runtime components into a measured online control loop while preserving one browser writer, SubmissionGate authority, receipt reconciliation, and fail-closed uncertainty handling.

## Sequence

1. P0: restore clean-install CI; remove synchronous shadow work from the authoritative Agent launch path; separate observation from effect persistence; bound and route App Server subscriptions.
2. P1: add the deterministic `ApplicationSupervisorLoop` with progress/stall signals and levelled observe, repair, steer, interrupt, and park decisions. No extra model call on the normal fast path.
3. P2: connect read-only proposal specialists with heartbeat, deadline, partial result, cancellation, retry, conflict reduction, and required/advisory semantics. Specialists never own browser writes or submission authority.
4. P3: complete the durable background-task journal and worker runtime with leases, reaping, retry scheduling, cancellation dispatch, result events, and dead-letter evidence.
5. P4: compile CLI, App Server, MCP, and deterministic host tools from one typed registry; expose a minimal phase/provider/authority/sensitivity-scoped surface; support App Server dynamic tools at start and resume restoration.

## Stage gates

- Add a focused failing test before each behavior fix or feature slice.
- Run the narrow module tests after each slice and the phase-focused union before advancing.
- After P4, run full lint, the full test suite, distribution build, clean-install smoke, and a no-submit real runtime delivery smoke.
- Keep production Runtime Cells at one unless a separate admitted gate receipt exists.

## Acceptance boundaries

- Shadow observation cannot delay authoritative Agent process launch.
- Read-only observations cannot mutate effect/submit state.
- Repeated no-progress tool calls receive deterministic intervention; uncertain effects are never replayed automatically.
- Proposal specialists are bounded, cancellable, durable, and read-only.
- Background tasks have observable leases, heartbeats, retries, cancellation, results, and dead-letter reasons.
- Tool exposure is fail-closed and derived from a common declaration registry.
- `dynamicTools` is treated as experimental and capability-gated, matching official App Server behavior.

## Non-goals

- P5 provider fast paths and two-Cell production canary.
- Increasing worker count without a fresh production admission receipt.
- Weakening CAPTCHA, assessment, identity/financial document, unsupported declaration, SubmissionGate, or uncertain-receipt stops.
- Pushing, deploying, or making a real ATS submission without a later explicit closeout decision.

