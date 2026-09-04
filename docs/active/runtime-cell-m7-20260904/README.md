# M7 Runtime Cells local evidence

- Integration base: `a7d7ddf368886d333f3ae41bafe25c2c5efa8d22`.
- Current diagnostic: `runtime_cell_scheduler_microbenchmark_report_v2.json`.
- This is a SQLite-and-sleep scheduler microbenchmark only. It exercises same-domain exclusion
  and different-domain scheduler concurrency, but does not exercise the App Server or browser
  context host lifecycle and cannot produce an M7 gate receipt.
- Its Submit, effect, duplicate-submit, reservation, receipt, and host-lifecycle observations are
  explicitly `unavailable`, not inferred zeroes.
- Historical `runtime_cell_no_submit_report.json` is superseded, non-admission diagnostic output.
  Its former `NOT_ADMITTED` label and hard-coded safety counters must not be consumed as current
  evidence or as a gate receipt.
- Historical `runtime_cell_scheduler_microbenchmark_report.json` is also superseded because its
  source identity predates the compatible v2 process-identity registry migration.
- The production Runtime Cell admission evaluator rejects the scheduler-microbenchmark schema.
- Runtime Cell production status remains `NOT_ADMITTED`; effective production Cells remain `1`.
- App Server-backed production Cells remain separately blocked/default-off on this baseline; the
  current production-safety review is not an admitted gate receipt.
- No live ATS, real application, or real Submit was run.
