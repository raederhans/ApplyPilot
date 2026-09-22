# CLI + in-app browser concurrent preparation

`browser-batch` prepares several separately bound IAB tabs while the current Codex
task remains the attending browser operator. Default model-worker concurrency is
2; available settings are 1–4. Default per-hostname concurrency is 1 and available
RAM reserve is 1024 MiB. These are admission limits, not measured ATS capacity.
App Server, the external-browser RuntimeCell experiment, and Recipe flags do not
need to be enabled. The existing unattended `apply` path remains separate.

## Run a batch

1. Admit jobs, freeze materials and begin their existing `attended-application`
   attempts as described in `visual-worker-bridge.md`. Keep distinct job IDs,
   attempts, tabs, goal files and bridge directories. Workers receive authoritative
   facts and artifact references, never credentials. IAB tabs share the browser's
   login context: separate tabs do not imply separate cookies/accounts.
2. Attach one `createInAppBrowserHost` in phase `prepare` per tab using the supported
   browser session. Finish attachment/readability checks before launching workers;
   a failed or timed-out setup call is not an active host confirmation. Use fresh
   directories after a lost session; reconcile old workers before reconnecting.
3. Create a manifest, resolving job paths relative to that file:

```json
{
  "max_workers": 2,
  "per_origin_limit": 1,
  "timeout_seconds": 600,
  "jobs": [
    {"job_id": "job-a", "bridge_dir": "job-a/bridge", "task_file": "job-a/goal.txt"},
    {"job_id": "job-b", "bridge_dir": "job-b/bridge", "task_file": "job-b/goal.txt"}
  ]
}
```

4. From the source directory run the workspace wrapper:

```powershell
../run.ps1 browser-batch --manifest <manifest.json> --output-dir <fresh-output-directory>
```

5. Keep servicing each host in Codex. `createBrowserHostGroup` in
   `scripts/browser-host-group.mjs` combines their `peek()` results. Inspect
   requests against the latest page observation, then pass only reviewed
   `{job_id, request_id}` pairs to `group.execute(...)`. At most one request per
   job and two browser operations execute concurrently. There is no background
   pump, automatic request approval, alternate browser controller or fixed click
   sequence. Refresh all attached host heartbeats while servicing the batch.
6. Independently inspect final page state and update each existing attended
   attempt. `worker_completed_needs_review` means the model turn ended; it may have
   reported a blocker. Use the established checkpoint, gate, reservation and receipt
   path for one authorized submission at a time. This command cannot submit a batch.

The standalone `scripts/run_browser_batch.py` accepts the same manifest and output
options. `BrowserBatch.run()` also acquires exclusive bridge leases when called as
a library. Same bridge/tab/session duplicates are rejected before spawning.
Single `browser-work` workers honor these leases and maintain a separate exclusive
worker owner. Existing owner files are never automatically stolen after a crash.

## Field batching

IAB `prepare` hosts support `fill_batch`: one to four steps containing `operation`,
`field_key` and `value`. A worker normally combines two to four independent known
text/native-select fields from its current observation. The entire plan validates
before the first write. Each step then checks the current control, writes, commits
blur and reads back. Unexpected form/option/value changes or non-persistence park
the remaining steps. The reply reports partial completed steps; never replay the
whole batch. Unknown write outcomes stop the host for reconciliation.

Dates, custom comboboxes, checkboxes, file uploads, navigation, declarations and
Submit stay outside the batch. The existing per-control/host handling remains
available. This IAB-native path reduces model/host round trips; it does not turn on
the older external CDP semantic-batch flag. Immediate readback is not proof against
later resume parsing or delayed validation.

## Observe progress and application state

`output/status.json` records last-observed counts, per-job status/exit code, queue
and execution duration, actual worker highwater, owner PID/update time, limits and
sampled system available RAM. Zero exit codes do not imply form readiness. Failed,
cancelled or timed-out jobs make the batch command return nonzero. Other jobs can
continue after an individual failure. RAM below the reserve postpones admission;
unknown RAM with an enabled guard fails closed. Queue waiting for RAM is bounded.

Per-job logs can contain browser observations and model responses; treat them as
private application artifacts. The status file omits goal/answer contents. A killed
parent may leave last-known `running` entries and lease files. Inspect process and
browser evidence before recovery, preserve the report, and start a fresh batch;
never infer that stale status permits another writer or another submission.

```powershell
../run.ps1 attended-plan --db <jobs.db> --attempt-id <existing-attempt-id>
```

This read-only plan projection uses current ledger records with a read-only SQLite
connection. It exposes exact attempt/job/host/tab bindings, durable stage, historical
checkpoint, material digests, blocker codes, evidence references and admitted receipt
binding. It has no executor or submission authority, creates no extra database and
does not require the experimental ApplicationPlan shadow flag.

## Capacity interpretation

Prefer completed, independently reviewed jobs and blocker rates over raw speed.
Increase concurrency only when host queue delay stays tolerable, fields persist,
no cross-tab interference appears, and resource reserve remains healthy. Same-site
login state, host servicing, model quota/rate limits, uploads, complex ATS controls
and long-lived tabs can become limits before CPU or RAM. Available RAM is a system
observation, not per-runtime memory usage; shared Codex/IAB memory needs a separate
whole-app measurement. See [real ATS results and evidence limits](attended-runtime-results.md).
The default remains two workers; a successful four-worker sample is not a reason
to automatically raise concurrency or bypass the default one-worker hostname cap.
