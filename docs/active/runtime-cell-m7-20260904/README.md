# M7 Runtime Cells local evidence

- Integration base: `a7d7ddf368886d333f3ae41bafe25c2c5efa8d22`.
- Benchmark: `runtime_cell_no_submit_report.json` (`NOT_ADMITTED`).
- Paired bootstrap 95% lower bound: `1.5351590966084585x` (required: `1.6x`).
- Mean paired speedup: `1.73003788616382x`.
- Same-domain peak: `1`; different-domain two-Cell peak: `2`.
- Submit, effect, duplicate-submit, reservation, receipt, and cross-Cell-write counters: `0`.
- The report is local diagnostic evidence and grants no production authority.
- App Server-backed production Cells remain separately blocked/default-off on this baseline; the
  current production-safety review is not an admitted gate receipt.
- No live ATS, real application, or real Submit was run.
