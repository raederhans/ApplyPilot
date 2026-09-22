# Task

## Current status

Stages 0–5 are implemented and verified. Stage 3 is intentionally the first
bounded worker split: Runtime Cell shadow-session ownership moved out of the
large loop, while lifecycle, entry-turn, recovery, and telemetry remain future
slices. Stage 6 integration is in progress.

## Checklist

- [x] Confirm merged base and protect dirty main checkout.
- [x] Create isolated worktree and branch.
- [x] Start launcher/worker/submit authority CLI analysis.
- [x] Start transaction/schema CLI inventory.
- [x] Integrate Stage 0 evidence and freeze the first implementation slice.
- [x] Implement and verify Stage 1.
- [x] Implement and verify Stage 2.
- [x] Implement and verify Stage 3.
- [x] Implement and verify Stage 4.
- [x] Implement and verify Stage 5.
- [ ] Complete Stage 6 integration and synchronization.

## Validation evidence

| Command or check | Result |
| --- | --- |
| `git rev-parse HEAD` in implementation worktree | `6e23d067d798794b74ce22079b3a10f8b25e313e` |
| `git status --short --branch` in source checkout | Dirty user WIP identified and left untouched |
| External CLI launcher analysis | Router task `c7cdb5d4648249b98ccf385d14ab7bd7`, running read-only |
| External CLI SQL/schema analysis | Router task `140b8b6e34f543f3a9ca0c429f9b365a`, running read-only |
| Baseline architecture tests | `108 passed in 18.02s` |
| Transaction helper tests | `96 passed in 4.04s` |
| Database/storage focused regression | `270 passed in 21.60s`; final migration suite `7 passed` |
| Launcher/preflight focused regression | `35 passed, 186 deselected in 1.89s` |
| Submission authority focused regression | `28 passed, 193 deselected in 4.28s` |
| Worker shadow extraction regression | `62 passed in 6.89s`; Ruff passed |
| Ruff | `ruff check src` passed |
| Non-browser/non-Windows | `2790 passed, 97 deselected in 95.23s` |
| Compatibility | `48 passed, 2839 deselected in 1.56s` |
| Windows | `32 passed, 2855 deselected in 2.32s` |
| Browser | `65 passed, 2822 deselected in 107.14s` |
| Packaging and installed-wheel smoke | Build passed; install, help, init, doctor, dashboard passed |
| Independent final reviews | Database concurrency/WAL fix `PASS`; complete bounded diff `PASS` |

## Open risks and remaining work

- The database version-1 migration now performs the historical additive bootstrap atomically; future versions run in the same ordered transaction chain.
- Full worker lifecycle/entry/recovery/telemetry extraction remains a later architecture direction and is not claimed by this tranche.
- Existing unrelated worktrees are historical and out of scope; only the new convergence worktree will be integrated by this task.
