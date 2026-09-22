# Runtime and job transaction boundaries

## Scope

This change separates two implementation hotspots without changing the CLI,
submission rules, browser behavior, experimental-runtime admission, test tiers,
or GitHub Actions configuration. It is a bounded refactoring increment, not a
claim that the entire launcher has been decomposed.

The integration base is `e3ad9f4f5b9cc5d377d5de9340779e28dd629d0c`.

## Implementation owners

| Owner | Responsibility |
| --- | --- |
| `apply/agent_configuration.py` | Immutable model/effort configuration and precedence |
| `apply/agent_commands.py` | Provider executable resolution and MCP/command assembly |
| `apply/agent_process.py` | Subprocess lifecycle, quarantine, timeout and telemetry |
| `apply/job_candidates.py` | Candidate SQL and advisory company-priority ranking |
| `apply/job_acquisition.py` | Admission workflow and atomic attempt/job claiming |
| `apply/job_results.py` | Atomic job-result, release, preview and manual-status writes |
| `storage/transactions.py` | Owned transaction and nested-savepoint semantics |

`agent_runtime.py` retains stateless compatibility imports. All 29 original
function/class declarations are AST-identical in their new owners. This keeps
existing call sites working without duplicating runtime state or forwarding
monkeypatches dynamically. New consumers should import the relevant owner.

`application_jobs.py` retains its existing call signatures and resolves mutable
profile/environment/clock dependencies at invocation. Its final-submit duplicate
predicate is unchanged and still requires the submission claim transaction.
The facade shrinks from 857 to 126 lines; `agent_runtime.py` shrinks from 1,305 to
46 lines. These are facade sizes, not claims of equivalent total-code reduction.

## Queue acquisition

Selection, company-priority ranking, material/admission evaluation and manifest
matching no longer run under the queue-claim writer lock. Ranking still covers
all SQL-eligible candidates before selection; no raw-fit top-N truncation is
introduced.

Each candidate carries a separate persisted-row snapshot and ranked projection.
Inside a short write transaction, acquisition compares the current persisted
row against that snapshot, rechecks authorization expiry and current batch
consumption, then writes the attempt and job claim atomically. Optional Cell
claims participate in that same atomic unit. A conflict rolls back the attempt
and callback writes before considering the next candidate.

Changed candidates are skipped for that invocation rather than claimed from
stale evidence. This is intentionally conservative: unrelated row changes can
also cause a skip, and a concurrent update can make a call return no candidate.
The next ordinary invocation can select fresh data. Company priority is an
advisory read-time snapshot, not a promise of serializable ranking across
concurrent recruiting-feedback edits.

Stale-material retirement and portal pauses also compare the captured row
inside their own short transaction. An old assessment must not erase a newly
corrected attachment. Existing final preparation, audit, identity, duplicate,
submission-intent and receipt checks remain mandatory. SQLite transactions do
not make external files, websites or profile bytes transactional.

Acquisition requires a connection with no active caller transaction and rejects
one before running startup helpers. Cell callbacks must perform participating
database work only: no commit, browser launch or agent dispatch is permitted.

## Result writes

Result/status operations use `write_transaction`. An operation that owns the
transaction commits on success and rolls back on failure. An operation inside
a caller transaction uses a savepoint: success does not commit the caller's
work, and failure rolls back only this operation. Cancellation follows the same
rollback boundary. Nested callers are responsible for their final commit.

Job status, attempt finalization and any associated risk event now share that
unit. Existing stale-attempt predicates, status meanings, uncertain-submission
retry blocking and receipt authority are not relaxed.

The scope does not migrate every database helper, introduce a schema version,
change table layouts, or refactor all existing migration code.

## Tests and measurements

Port tests no longer freeze the exact count of dataclass fields. Capability
separation and production-disabled assertions remain. Executable selection
exercises the real version parser with fake operating-system I/O rather than
patching a private helper. No test markers, CI topology or feature gates change.

Sixteen added cases exercise real SQLite connections, concurrent claims,
changed material/JD/status snapshots, manifest expiry, Cell conflicts, caller
transaction ownership, nested rollback, cancellation and stale-attempt writes.
They do not call an employer, browser, mailbox or paid model.

Acquisition keeps existing performance keys and adds `transaction_hold_ms`,
`claim_transactions` and `stale_candidates_skipped`. The independent-writer test
proves admission does not retain the queue write lock. It is not an end-to-end
throughput, token-cost or live ATS benchmark.

## Local verification provenance and limits

The network-isolated runner used the repository's v0.5.2 source distribution
from Actions artifact `10628917091` (run `35578203242`). Every edited existing
file was checked against the integration base by Git blob identity; those files
are unchanged between the release and that base. Other release/main differences
are not silently overwritten by this commit.

Before edits, 110 relevant baseline tests passed. After edits, the broad
available-source non-browser/non-Windows run passed 2,763 tests, with 97 tests
deselected. The 16 new transaction cases are included in that passing total.
The local command was:

```sh
python -m pytest -q -m 'not browser and not windows' \
  --ignore=tests/test_curation_promotion_guards.py \
  --ignore=tests/test_ci_workflow_contract.py
```

Those two files were excluded locally because the release source distribution
omits `tools/curate_resume_library.py` and `.github/workflows/ci.yml`. The PR does
not exclude them from CI. Initial subprocess-import environment failures were
resolved by making the local source importable to child processes. An existing
CanaryPool test double also intermittently lacked `evict_worker_async` when a
shadow thread outlived its 10ms join; that test passed on isolated baseline and
modified runs and in the broad rerun. No experimental production path was
modified to mask it.

AST parsing and `git diff --check` passed. Local Ruff, release packaging,
Windows, Chromium and live application execution were not run. Exact-main
integration and platform qualification belong to the unchanged repository CI;
local passing counts alone do not establish those results.
