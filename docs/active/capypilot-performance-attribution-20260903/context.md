# Context

## 2026-09-03

- Integration owner: root agent. User-visible audit tasks and internal subagents are read-only; only this worktree may write.
- Base: `fork/main` / local `main` at `70dd7e5d8c67ec0869c8500fb9d8d54fc7c0b9af`, remote CI run `33713631836` succeeded.
- Worktree: `C:\Users\raede\Desktop\简历\applypilot-local\wt-performance-attribution`; branch `feat/performance-attribution-v2`.
- The prior no-submit cohort measured throughput, latency, RSS, SQLite wait, and submit-lane wait/hold but did not attribute job latency across browser/session, authority, inspection, evidence, persistence, and semantic execution.
- Current task is diagnostic-only. It must not alter production worker authority or promote 2/4 workers.
- Parallel audits converged on the same sequence: complete M1 attribution first; do not enable persistent MCP or Workday host writes until browser authority mutation has one owner. `BrowserAuthorityHandle` is recorded as the next architectural prerequisite, not implemented in this change.
- M1 implementation records exclusive task spans, worker lifecycle spans, coverage/residual time, missing required spans, and unavailable Agent/MCP spans. Missing or sub-90% attribution is reported as incomplete for optimization decisions, but this diagnostic slice does not change the existing worker admission algorithm or production authority.
- Integration closed at implementation commit `89dc5cd89159e05cf989855b2a3de5a5624f103d`; exact-head CI `33730452257` passed. The feature branch and worktree were then removed, leaving `main` as the only registered worktree.
