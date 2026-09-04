# Round 2 runtime-control task status

- [x] P0 critical path and clean-install CI
- [x] P1 online supervisor loop
- [x] P2 production proposal specialists
- [x] P3 background task runtime
- [x] P4 unified ToolBroker and deferred loading
- [x] Cross-phase focused union
- [x] Full lint and test suite
- [ ] Distribution and clean-install smoke
- [ ] Real no-submit delivery smoke

Current stage: distribution, clean-install, and real no-submit delivery gates.

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

Cross-phase focused union: `584 passed in 30.20s`. Full Ruff and `git diff
--check` passed. Full repository suite: `2130 passed in 151.78s`.
