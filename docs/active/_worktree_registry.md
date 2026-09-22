# Worktree Registry

| Worktree / path | Task | Base branch / commit | Current branch / HEAD | Goal | State | Hotspots | Tests | Overlap | Order | Next action |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `C:/Users/raede/.codex/worktrees/applypilot-architecture-convergence-20260922` | Architecture convergence 1–5 | `main` / `6e23d06` | `codex/architecture-convergence-20260922` / pending commits | Launcher, submit authority, bounded worker split, transactions, schema migrations | ready-for-integration | `launcher.py`, `worker_orchestration.py`, storage/database | All repository CI-equivalent tiers, build, and installed-wheel smoke passed | Dirty main checkout is protected; no path ownership shared with another active writer | 1 | Commit, open PR, merge, and fast-forward main |

States: `in-progress`, `blocked`, `ready-for-review`, `ready-for-integration`, `integrated`, `abandoned`.

## Delivery package

- Summary: extracted preflight and submission authority, isolated Runtime Cell shadow ownership, unified compatible SQLite transactions, and introduced atomic ordered top-level migrations.
- Files: application launcher/worker owners, storage transaction and migration modules, focused regression tests, and this task record.
- Diff from base: net code reduction in launcher/worker with new responsibility-owned modules; additive database migration tests.
- Commit and branch state: reviewed working tree ready for Lore commits.
- Divergence from main: based on merged PR #9 (`6e23d06`); no unrelated main WIP included.
- Overlap and conflict risk: main contains unrelated WIP outside the current planned product hotspots.
- Validation evidence: Ruff; 2790 non-browser; 48 compatibility; 32 Windows; 65 browser; build and installed-wheel smoke; two independent PASS reviews after database fixes.
- Unverified risks: full worker lifecycle/entry/recovery/telemetry decomposition is explicitly deferred; no production database was migrated.
- Recommended integration method: protected-main PR followed by fast-forward synchronization of the dirty main checkout.
