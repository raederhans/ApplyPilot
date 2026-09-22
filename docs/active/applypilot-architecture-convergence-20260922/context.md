# Context

## Current truth

- Main/fork merged PR #9 at `6e23d067d798794b74ce22079b3a10f8b25e313e`.
- Main checkout contains unrelated tracked and untracked user changes, so implementation is isolated in `C:/Users/raede/.codex/worktrees/applypilot-architecture-convergence-20260922` on branch `codex/architecture-convergence-20260922`.
- The requested scope is architecture plan items 1–5. No live applications, production database migration, deployment, or release are in scope.
- `launcher.py` is 8,633 lines and `run_job()` spans about 2,541 lines. `worker_orchestration.py` is 3,706 lines and `_worker_loop_with_port()` spans about 622 lines at the base.

## Decisions and deviations

| Time | Evidence or decision | Impact |
| --- | --- | --- |
| 2026-09-22 | Main checkout is dirty with unrelated WIP. | Use an isolated worktree; never reset or stash the user's files. |
| 2026-09-22 | External CLI execution options expose Gemini 3.1 Pro high and GLM-4.5-Air; GLM-5.3 is blocked. | Use Gemini for architectural mapping and Air for mechanical SQL inventory. |
| 2026-09-22 | The previous refactor introduced `storage/transactions.py` but explicitly did not migrate all helpers or add schema versioning. | Stages 4–5 build on that helper incrementally. |
| 2026-09-22 | Both browser and direct-email production submits pass through the same reservation/SubmissionGate and single-writer lane. | Stage 2 can centralize authority without changing the worker's final-effect order. |
| 2026-09-22 | The first Zhipu writer was blocked by Windows allowlist path normalization twice and produced no edits. | Parent implemented Stage 4 and retained the durable CLI failure evidence. |
| 2026-09-22 | The first Antigravity Stage 1 writer failed before model execution with a transient 503 and produced no edits. | Resume the same task on Gemini 3.8 Flash high; do not count the failed attempt as implementation. |

## Live process ownership

| Process | Owner | Log path | State |
| --- | --- | --- | --- |
| Stage 1 preflight extraction `bd423314668b44ab9243b78e96253af0` | cli-router / Antigravity | Router durable task record | Cancelled after no edits; parent completed the slice |
| Stage 3 shadow-session extraction `83a6f53f897b46f3937f7eef6232468f` | cli-router / Antigravity | Router durable task record | Completed and integrated |
| CI-equivalent verification | root | Console receipts | Completed; all required lanes passed |

## Handoff

No external writer or test process is active. Root owns final Git integration.
The exact CI scopes passed from the isolated worktree. An earlier diagnostic
used `ruff check src tests` and surfaced three pre-existing test-style findings
outside this task; the repository CI contract is `ruff check src`, which passed.

## Next step

Commit the reviewed slices, create and merge the PR, then fast-forward the dirty main checkout without touching unrelated WIP.
