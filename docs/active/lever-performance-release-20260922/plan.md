# Plan

## Goal

Reduce unnecessary Lever browser-agent round trips, prove the result with repeated real no-submit runs, then publish the accumulated compatible release as Job Apply Pilot v0.6.0.

## Scope

- Privacy-safe browser tool duration/count attribution.
- Lever prompt guidance for grouped native-control operations and bounded verification.
- Focused, full, release-package, and repeated real-site no-submit verification.
- Performance PR integration followed by a separate v0.6.0 release-preparation PR and immutable tag.

## Sources of truth

- `logs/lever-sggc-r1.log` and `logs/lever-sggc-r2.log` under the isolated 2026-09-22 test run.
- Current `fork/main` and GitHub CI/Release receipts.
- `pyproject.toml`, `src/applypilot/__init__.py`, `CHANGELOG.md`, and `.github/workflows/publish.yml`.

## Stages

- [x] Stage 1: Attribute the observed Lever variance and select the lowest-risk optimization.
- [x] Stage 2: Implement bounded timing plus consistent Lever interaction guidance.
- [x] Stage 3: Run focused, full, and repeated real-site no-submit verification.
- [ ] Stage 4: Review, commit, push, open and merge the performance PR.
- [ ] Stage 5: Prepare, verify, merge, tag, and verify Job Apply Pilot v0.6.0.

## Acceptance criteria

- Lever no-submit runs retain resume upload, field completion, final-submit prohibition, and browser cleanup behavior.
- Repeated runs expose browser tool counts and paired duration samples without recording field values or credentials. Duration sums are advisory execution sums, not wall-clock totals when calls overlap.
- The optimized cohort materially reduces redundant browser calls or latency spread versus the 26/41-call, 206.6/337.6-second baseline; otherwise the optimization is not released as successful.
- Local verification and required GitHub CI pass before each merge.
- The v0.6.0 tag points at the exact release-preparation merge commit and publishes wheel, sdist, bundle, and checksums.

## Non-goals

- No production concurrency increase, provider recipe promotion, final job submission, PyPI enablement, or upstream `origin` push.
- No cleanup or modification of unrelated WIP in the primary checkout or historical worktrees.

## Risks and constraints

- Browser/model latency is variable; compare repeated same-site runs and report sample limits.
- Reducing observations must not weaken dynamic-control persistence, upload, CAPTCHA, credential, submit, receipt, or cleanup gates.
- GitHub branch protection is absent, so PR state, CI, merge SHA, and tag target require explicit checks.
