# Round 2 runtime-control task status

- [x] P0 critical path and clean-install CI
- [x] P1 online supervisor loop
- [x] P2 production proposal specialists
- [x] P3 background task runtime
- [x] P4 unified ToolBroker and deferred loading
- [x] Cross-phase focused union
- [x] Full lint and test suite
- [x] Distribution and clean-install smoke
- [x] Real no-submit delivery gate (safe pre-browser block; no eligible alternate)

Current stage: complete locally; real browser-level ATS smoke remains blocked by
current material/CAPTCHA/coverage evidence rather than by an untested runtime.

P0 gate: `289 passed in 14.48s` across the App Server, launcher runtime,
Runtime Cell, and runtime-settings focused union; target Ruff and `git diff --check`
also passed.

P1 gate: `286 passed in 17.95s` across the online Supervisor, launcher runtime,
App Server, runtime contract, and browser authority/broker focused union; target
Ruff and `git diff --check` also passed. Independent final review: PASS with no
material finding.

P2 gate: `321 passed in 10.02s` across production specialist modes, ATS fill
planning, orchestration contracts, performance coordination, legacy journal
replay, and runtime contracts; target Ruff and `git diff --check` also passed.
Independent final review: PASS with no material finding in the bounded P2 scope.

P3 gate: `333 passed in 12.18s` across the task journal, background runtime,
production specialists, orchestration, and runtime contracts; target Ruff and
`git diff --check` also passed. Independent final review: PASS with no material
finding.

P4 gate: canonical ToolSpec/ToolBroker admission now owns CLI, internal MCP,
mailbox/credential transport, and App Server Dynamic Tools surfaces. Default
remains shadow; active is fail-closed and limited to explicitly safe,
non-sensitive, idempotent read/report/proposal tools. Dynamic Tools are
experimental, default-off, start-only, durable across resume, and currently
expose only the schema-complete `detect_ats` read when its compiled ATS state
admits it. P4 focused verification: `290 passed in 13.64s`; independent final
review: PASS with no material finding.

Cross-phase focused union: `584 passed in 30.20s`. After the live browser cleanup
race fix, full Ruff and `git diff --check` passed and the full repository suite
reached `2131 passed in 156.13s`.

Final delivery gates: release wheel, sdist, bundle, and hashes rebuilt; clean
install passed `resume-route --help -> init -> doctor -> dashboard`. Real Codex
App Server `0.149.0` accepted the experimental API handshake, received the sole
read-only `detect_ats` dynamic descriptor, issued `item/tool/call`, accepted the
schema-valid result, completed the turn, and shut down. Edge/CDP plus the actual
Playwright MCP package exposed 24 tools and observed the Web Form target; cleanup
confirmed the endpoint, bootstrap process, and profile sidecar were all closed.

The exact Workato Greenhouse dry-run was then executed with one worker and final
Submit disabled. The required material gate found a stale GPA (`3.6` in the old
resume versus `3.46` in the current profile), retired that artifact, and stopped
before browser launch. Reconciliation remained at two historical pre-submit
attempts, zero `submit_started`, zero receipts, zero risk events, and zero new
runtime turns/tasks. A bounded review of five alternate live ATS jobs found no
candidate that also cleared CAPTCHA, material, and coverage gates, so no unsafe
second attempt was made.
