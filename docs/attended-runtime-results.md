# Attended runtime: real ATS preparation samples

On 2026-09-22, three sequential timing rounds used real public Lever guest forms
in the Codex in-app browser, with real Codex CLI workers. Each job filled the same
four authoritative contact fields (name, email, phone and LinkedIn). Round 1 ran
two jobs serially; round 2 repeated those two jobs concurrently; round 3 used four
distinct jobs with the same form structure. Every round used fresh bridge bindings
and empty form fields. The attending operator explicitly reviewed each request.

| Runtime concurrency | Jobs | Independently retained values | Batch wall time | Peak CLI-tree working set | Bridge operations |
| --- | --- | --- | --- | --- | --- |
| 1 | 2 | 8/8 | 137.968 s | 313.8 MiB | 5 |
| 2 | 2 | 8/8 | 65.751 s | 463.9 MiB | 4 |
| 4 | 4 | 16/16 | 106.637 s | 768.2 MiB | 10 |

All eight worker turns exited successfully, and all eight four-field batches
reported verified. Independent page inspection after the turns confirmed the
32 expected values. No batch parked or encountered an unknown write outcome.
Other observed fields remained empty/unselected. No uploads, declarations,
consents or Submit operations were performed. These results are not application
completion or employer receipt evidence.

The maximum four-field action duration was 862 ms. The longest observed request
wait for the attending operator was 25.855 s. Model decisions included different
numbers of extra observations (5/4/10 total bridge operations), and operator
response delays varied. Thus the table is a small workload observation, not a
controlled estimate of speedup or a throughput SLA. Each four-field write used
one model/host request instead of four single-field write requests; this reduces
write round trips, while retaining per-field checks.

Process-tree working set and CPU were sampled from the test launcher every
100 ms; short-lived processes can be missed and shared pages can be counted more
than once. CPU samples totalled 30.30/15.02/30.03 seconds across the three rounds.
This excludes the shared Codex application and IAB browser. System available RAM
stayed above 31.5 GiB. The sample did not approach a measured hardware ceiling.

The per-hostname cap was explicitly raised to 2/4 for these same-site stress
rounds. Production defaults remain two model workers, one per hostname, and a
1024 MiB available-memory admission reserve. Browser requests execute at most two
at a time even with four model workers. Do not infer account/cookie isolation from
separate tabs. Unknown/stale outcomes still require reconciliation before reuse.

Round 3 also exercised the packaged module worker entry after its source-path
dependency was removed. Cancellation hardening is covered separately by process
tests, including POSIX CLI/grandchild cleanup and interruption during spawn.
It is not inferred from successful browser turns.

Not established: cross-provider ATS reliability, uploads and subsequent resume
parsing, custom combobox persistence, multi-page applications, CAPTCHA handoff,
long-running memory growth, end-to-end submissions, or a safe maximum above four.
The current release keeps these evidence limits explicit and provides no automatic
concurrency escalation. Raw observations and contact data remain private outside
the source/release archives.
