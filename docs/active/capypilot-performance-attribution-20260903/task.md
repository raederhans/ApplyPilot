# Task status

## M1 — in progress

- [x] Confirm exact green base `70dd7e5` and remote CI `33713631836`.
- [x] Create isolated integration worktree and branch.
- [x] Implement phase attribution and explicit unavailable-Agent/MCP contract in the isolated P4 benchmark.
- [x] Add focused pure-contract and Chromium no-submit tests.
- [ ] Review, commit, merge to `fork/main`, verify remote CI, and clean the worktree.

### Local evidence

- Scoped Ruff: passed for both changed runtime modules and both attribution test files.
- Focused non-browser safety selection: `54 passed, 1 deselected`.
- Chromium no-submit selection: `1 passed, 6 deselected`.
- Independent diff review: `PASS WITH NOTES`; the initial admission-gate coupling was removed, and the reviewer found no remaining blocker.
- No live ATS, Submit, SubmissionGate reservation, receipt, mailbox, or production worker-count change was exercised.
