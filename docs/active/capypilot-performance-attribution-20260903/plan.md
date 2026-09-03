# CapyPilot performance attribution v2 — 2026-09-03

## Goal

Make the existing isolated no-submit cohort explain where its measured time is spent before any persistent-runtime, semantic-fast-path, or worker-count change is admitted.

## Scope

1. Add explicit, monotonic phase timings to the deterministic P4 worker and Chromium lifecycle.
2. Report per-task attribution coverage plus unavailable spans instead of silently zero-filling unobserved Agent/MCP metrics.
3. Aggregate phase percentiles and coverage without changing the production admission decision or worker governor.
4. Validate the report contract and one local Chromium no-submit path only.

## Non-goals

- No live ATS, application submission, mailbox access, worker-count promotion, persistent Agent/MCP runtime, or Workday production fast path.
- No serialization of browser or submit authority.
- No change to `SubmissionGate`, independent observation, or uncertain-receipt semantics.

## Acceptance

- Every emitted phase duration is finite and non-negative.
- Coverage is derived from observed exclusive spans and bounded to `[0, 1]`.
- Unobserved production Agent/MCP spans are explicitly listed as unavailable.
- Existing no-submit safety and admission contracts remain unchanged.
