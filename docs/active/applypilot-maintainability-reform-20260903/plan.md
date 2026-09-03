# ApplyPilot maintainability reform — 2026-09-03

## Goal

Reduce maintainability cost without weakening the application safety contract: CAPTCHA and assessment stops, SubmissionGate, exactly-once submit ownership, uncertain-receipt parking, and receipt reconciliation remain fail-closed.

## Scope and phases

1. **Phase I — low-risk quick wins (complete, local-only):** remove unreachable CAPTCHA-solver guidance; make runtime Prompt candidate facts profile/policy driven; remove or convert high-confidence duplicated/fragile tests; introduce explicit pytest tiers and an initial CI split.
2. **Phase II — typed command, worker, and attempt boundaries (complete, local-only):** keep the full platform-independent safety suite on Python 3.12 while limiting 3.11/3.13 to a compatibility subset; replace CLI/worker module service locators with immutable command/run options and responsibility-grouped typed ports; migrate only a safe, evidence-backed slice of attempt runtime state.
3. **Phase III — provider and page-observation modularization (complete, local-only):** split provider-specific routing and page-observation responsibilities behind capability-scoped descriptors and typed worker ports, then reduce the launcher compatibility surface without changing submission authority or receipt semantics.

## Non-goals

- No change to live ATS/browser/mailbox behavior, worker capacity, submission authorization, data schema, release publication, commit, push, or deployment.
- No full-suite, real-browser, real-ATS, submission, or mailbox run in Phases I or II.

## Phase I acceptance

- No CapSolver/CAPTCHA-solving instruction remains in `apply/prompt.py`; visible CAPTCHA remains a manual-review hard stop.
- Modified profile facts, not historical candidate literals, determine the rendered runtime Prompt.
- The merged/converted tests retain the protected behavior contracts.
- Pytest markers parse; every CI test tier has an owner; Chromium installs only in the browser lane; build/clean-install remains after upstream test gates.

## Phase II acceptance

- Python 3.12 core includes SubmissionGate, exactly-once ownership, uncertain-receipt, CAPTCHA/assessment hard-stop, and receipt-reconciliation behavior; Python 3.11/3.13 execute only the explicit compatibility subset; Chromium remains isolated to the browser lane.
- `cli.apply` projects Typer values into one immutable options object and calls a typed command runtime; `commands/apply.py` receives neither the CLI module nor 20 separate options.
- Worker core receives `WorkerRunOptions` and responsibility-grouped typed runtime ports. The launcher is the only compatibility composition root; the string-name port validator and direct `ModuleType` dependency are removed.
- Attempt identity/browser binding moves only when one source of truth can be preserved. If the mutable binding cannot move without dual writes, Phase II records the blocker and Phase III input instead of adding another authority-bearing representation.

## Phase III acceptance

- Provider detection, semantic upload, control write, credential relay, application episode, and LinkedIn handoff use capability-scoped `ProviderDescriptor` facts. Detection never grants another capability, and unknown providers fail closed.
- Shared page-surface selection, LinkedIn click/login/handoff observation, and post-submit observation have public module boundaries. `page_observation.py` remains the compatibility facade and owns the unmigrated pre-submit audit policy.
- `WorkerApplicationPorts` no longer carries page-observation services; `WorkerPageObservationPorts` is injected separately by launcher, which remains the composition root.
- Browser lease binding remains one mutable job mapping. No serialized submit authority or dual-written `AttemptRuntimeContext` is introduced while install, refresh, repair, heartbeat, and release still have multiple owners.
- Provider, page observation, worker runtime, SubmissionGate, and uncertain-receipt selections pass without real Chromium, live ATS, Submit, mailbox, remote CI, build, release, commit, or push activity.
