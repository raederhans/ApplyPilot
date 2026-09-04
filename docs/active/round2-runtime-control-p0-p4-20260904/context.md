# Round 2 runtime-control context

## 2026-09-04 baseline

- Integration base: `05a1149f163695add1afd08e950bfe060d72fef6`.
- Branch: `codex/round2-p0-p4`.
- Worktree: `C:\Users\raede\.codex\worktrees\applypilot-round2-p0-p4`.
- Personal-fork CI run `33860650323` failed only in `Core Python 3.12`: two `test_apply_runtime_contract` cases attempted to open the durable App Server control database before its parent directory existed in a clean checkout. Browser and Python 3.11/3.13 compatibility jobs passed; Windows clean-install was skipped by dependency.
- Official App Server documentation confirms `turn/steer` requires the active `expectedTurnId`, `turn/interrupt` ends the turn as `interrupted`, and experimental `dynamicTools` require `capabilities.experimentalApi`, persist in rollout metadata, and restore on resume when not replaced.
- Runtime Cell production remains `NOT_ADMITTED`; effective production Cells remain one.
- No live ATS write or submission is part of phase implementation/testing.

## Live-process ownership

- Owner: root agent only.
- Long tests/builds run serially from this worktree.
- Stable logs will be written under `.artifacts/round2-runtime-control/` when long gates begin.
- Success: command exit code 0 with expected test/build/smoke assertions.
- Stop: first deterministic failure; inspect evidence before retrying.

## Next checkpoint

P0 completed locally. Feature-off state peeks no longer create a missing database,
authoritative CLI launch precedes all shadow work, shadow artifacts are isolated,
slow App Server subscriptions are bounded and individually evicted, and read-only
observations no longer upgrade the durable effect boundary. Main-path shadow
cancellation is signal-only; a detached adapter shuts down in the background.

P0 verification: `289 passed in 14.48s` for the focused App Server, launcher,
Runtime Cell, and runtime-settings union; target Ruff and `git diff --check` passed.

P1 completed locally. The authoritative event stream now feeds a deterministic,
model-free online Supervisor with a two-second silent-turn window, normalized
repeat detection, exact-turn App Server steering, and durable intent/action/outcome
control ordering. Assistant narration does not count as progress; confirmed page
effects do not become submission uncertainty; terminal/watchdog races are closed.
Unsupported page re-observation is reported as audit-only, and Level 3 truthfully
parks rather than claiming automatic replacement.

P1 verification: `286 passed in 17.95s` for the focused Supervisor, launcher,
App Server, runtime-contract, and browser authority/broker union; target Ruff and
`git diff --check` passed. Independent final review returned PASS with no material
finding.

Next: extend the existing production specialist allowlist into a staged
`shadow -> advisory -> required` read-only contract with heartbeat, partial,
cancellation, deadline, retry, and deterministic conflict reduction. No specialist
receives browser-write, submit, mailbox-send, ledger-write, or ApplicationActor
authority.

P2 completed locally. The specialist contract now supports
`off -> shadow -> advisory -> required` with `enforce` as a compatibility alias.
Provider classification, application facts, and work authorization are admitted
as bounded preflight reads; material readiness remains the durable deterministic
read; field semantic and page-failure specialists are allowlisted but explicitly
skipped outside their real prepare/observe phases. Advisory output reaches the
Agent's bounded specialist context, while required failure blocks before browser
launch. Legacy replay compatibility is restricted to completed read-only tasks
whose core spec is unchanged.

P2 intentionally does not claim the background runtime controls owned by P3:
cross-process heartbeat, retry scheduling, cancellation dispatch, lease reaping,
and conflict coalescing remain unopened. Execution budget metadata remains
truthful where a hard termination boundary does not yet exist.

P2 verification: `321 passed in 10.02s` for the focused specialist,
orchestration, legacy-journal, and runtime-contract union; target Ruff and
`git diff --check` passed. Independent final review returned PASS with no material
finding in the bounded P2 scope.

Next: migrate the existing `agent_tasks` journal additively and introduce the
single-owner background runtime with lease-epoch CAS, bounded workers, persistent
heartbeat/progress/cancellation/retry, result events, and non-read replay denial.
