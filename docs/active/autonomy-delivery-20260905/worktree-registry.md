# Worktree Registry

| Worktree | Task owner | Base | State | Ownership |
| --- | --- | --- | --- | --- |
| applypilot-autonomy-delivery-20260905 | root | ad81f1c | code 18dcb0b fast-forwarded into main and verified on fork/main | integration, live data/browser, tests, Git |
| applypilot-autonomy-runtime-compare-20260905 | 01a06faf-09fe-7d73-ac4c-162fa644c705 | a7cb610 | completed; report and Python MCP source-path correction integrated | runtime/event fixtures |
| applypilot-autonomy-ats-compare-20260905 | 01a06faf-16f4-7b32-a9c8-05b2b77d370e | a7cb610 | completed; Country, EU-provider and exact-email-label patches integrated | isolated ATS fixtures and helper patches |
| applypilot-autonomy-batch-compare-20260905 | 01a06faf-26fc-77f0-8508-b633e8293a34 | a7cb610 | PDF, parser and mixed-clause corrections integrated | batch/material fixtures and requirement parsing |
| applypilot-autonomy-supervisor-20260905 | 01a06f8b-e75d-7871-bd0f-2dc0c97c1a4f | ad81f1c | previous implementation integrated in a7cb610 | Supervisor/launcher |
| applypilot-autonomy-readiness-20260905 | 01a06f8b-f363-7243-9520-22d37223b076 | ad81f1c | previous implementation integrated in a7cb610 | admission/material |
| applypilot-autonomy-runtime-20260905 | 01a06f8b-fd14-7ec3-9d8b-648dec5a4a53 | ad81f1c | previous implementation integrated in a7cb610 | runtime contracts |
| applypilot-autonomy-batch-20260905 | 01a06f98-ef6b-7c21-aee0-b8432c2aa897 | ad81f1c | previous implementation integrated in a7cb610 | durable batch progress |

Root has integrated patches sequentially. Delegates made no commits or pushes. Main and personal fork/main were freshly identical at 2d1323e on 2026-09-05. Existing unmerged candidate ad81f1c remains an ancestor of delivery, preserving its eight architecture commits. All listed branches/worktrees remain available for recovery, including delegated uncommitted patch copies. Unrelated historical worktrees are preserved and not part of cleanup. All three comparison tasks are complete. Root completed qualification, fast-forwarded clean main from 2d1323e to code commit 18dcb0bfe614fbc581c93686a4e11e1afcfa229e, pushed fork/main and verified the same remote ref. This follow-up records the observed delivery; main CI is the authoritative cross-platform verification. Source and integration checkouts were clean after the code delivery. Retained worktrees preserve patch provenance and unrelated WIP.


Login/routing follow-up: applypilot-login-routing-20260905, branch codex/login-routing-20260905, base 01e1719; root owns live tests, CLI, integration and Git. Internal score_led_routing owns routing and auth helper/prompt changes; auth_regression_history is read-only; auth_routing_review is a bounded read-only review. No additional user-visible tasks or worktrees were created for these internal delegates. Candidate qualification passed: affected suite 488, final routing 47, adjacent checks 2, final build/privacy audit and bounded review PASS. Live auth results are in task.md. Main and fork/main were freshly clean/equal at 01e1719 before integration. Old worktrees remain untouched; this worktree is retained for recovery.
