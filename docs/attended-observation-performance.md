# Attended preparation: observation reuse and diagnostics

## Scope and rollout

`createInAppBrowserHost` now defaults to `reuseFormObservations: true` for the
**prepare** phase only. Discovery and submit hosts retain full observations.
The legacy `browserAdapter(tab)` default remains unchanged. To compare against
the previous behavior or disable reuse in a preparation session:

```js
const host = await createInAppBrowserHost({
  directory, tab, phase: 'prepare', reuseFormObservations: false,
});
```

There is no new daemon, polling pump, browser controller or dependency. CAPTCHA
handling, external-browser routing, submission authority, the durable intent
latch and receipts are unchanged. Production worker/Runtime Cell admission is
not enabled or relaxed. These changes qualify a narrower single-host path,
not concurrent live applications.

## What is reused

A semantic `fill_control`, `select_control` or `set_checked` already reads the
form before writing and again after the action/blur. Previously the host discarded
that second result and immediately read the form a third time.

Preparation now passes a private one-use identity ticket directly from the
adapter's action to that action's observation. It can reuse the readback only
when the action reports confirmed persistence without a target validation error,
the page URL matches, field structure/coverage/options have not changed, no
new validation error appeared and no value is explicitly unavailable. Correcting
an initially invalid empty required field to a valid value does not itself count
as a structural change.

The ticket expires after 250 ms and never crosses an operation, adapter, tab or
explicit observation. This is an immediate-feedback age budget, **not** a claim
that the page stayed stable for 250 ms. Every subsequent semantic write still
independently re-reads and validates its target.

The smaller reply still includes **fresh visible DOM** and the **entire structured
form**, including dependent-field and upload-baseline deltas. It omits only the
additional overlapping `domSnapshot` and repeated `observeForm` call. It does not
truncate form fields or hide current visible alerts. A full observation remains
available through the ordinary `observe` operation.

Explicit `observe`, host inspection, pause/resume, screenshots, uploads,
non-semantic actions, expired/failed/unknown readbacks, navigation and structural
changes retain the full path. An explicit refresh drops old control results. If
an immediate reply needs a full refresh and the target value changed, persistence
becomes unknown instead of attaching the earlier success to the new value.
Mixed-page observations fail rather than pairing one page's identity with another
page's form. Existing interrupted-input handling remains `outcome_unknown`.

## Coverage and diagnostics

Preparation replies include `observation_feedback` with the selected path and
a descriptive coverage summary. Unknown counts are `null`, not zero. Frame
contents remain `not_inspected`; all-step completeness remains `unverified`.
These fields cannot be used as passing audit flags. No new iframe/closed-shadow
inspection capability is claimed.

```js
const diagnostics = host.metrics();
```

This method is read-only and performs no browser operation. It reports lifetime
outcome counters and nearest-rank p50/p95/max over the last 128 serviced requests,
with measured/unavailable counts for each metric. It retains no request arguments,
applicant values, URLs, paths, full page text, screenshots or credentials.

| Metric | Meaning and limit |
| --- | --- |
| `queue_wait_ms` | Request publication to host service; missing/future timestamps remain unknown |
| `action_ms` | Adapter action, including its pre-write check and post-write readback; unavailable on pure reads |
| `observation_ms` | Host reply observation; action-internal reads are counted under action time |
| `host_service_ms` | Entire serviced request, including queue-file/response overhead, excluding earlier queue wait |
| `text_bytes`, `image_bytes` | Observation payload bytes, not model tokens or full protocol-envelope size |
| `form_readbacks_reused` | Lifetime count of successful immediate readback reuse |

Model thinking/startup, human waiting and end-to-end application throughput are
not inferred from these timings. Completed operations are not submitted jobs.
Metrics remain in memory and disappear with the host; aggregate snapshots can be
retained by the attending operator when needed.

## Verification

The new Node contracts cover reuse, expiry, forged/cross-adapter tickets,
structural changes, unknown values, dependent fields, upload deltas, full submit
observations, pause/resume, stale observation rejection, interrupted inputs and
value-free bounded metrics. Existing host/form Node tests run alongside them.

```bash
python -m pytest -q tests/test_attended_observation_feedback.py -m "not windows"
```

The browser tier reuses the JavaScript driver shipped with Python Playwright and
an installed Chromium. It loads only offline synthetic HTML with no Submit button
and aborts network requests. It is an API-compatible test adapter, not the live
Codex IAB runtime or a model benchmark. The Windows tier reruns Node contracts;
it does not claim Windows browser end-to-end qualification.

A local Chromium comparison on 2026-09-21, using the same five ordinary writes:

| Reads including initial observation | Reuse disabled | Reuse enabled |
| --- | ---: | ---: |
| Form reads | 16 | 11 |
| Additional full snapshots | 6 | 1 |
| Current visible-DOM reads | 6 | 6 |

Final field values matched. Delayed parser changes, conditional required fields
and navigation were separately exercised. This measures call counts in this
fixture, not a live ATS speedup, token reduction or application success rate.
Slow CI runners may legitimately exceed the age budget; integration tests verify
that saved reads match actual reuse rather than forcing reuse by disabling guards.
