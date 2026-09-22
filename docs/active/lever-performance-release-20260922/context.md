# Context

## Current truth

- Isolated worktree: `C:\Users\raede\.codex\worktrees\applypilot-lever-perf-release`.
- Branch: `codex/lever-perf-release-20260922`, based on `fork/main@2430f34`.
- Baseline Lever durations: 206.6s and 337.6s; acquisition remained about 132-134ms.
- Browser calls increased from 26 to 41; the post-upload form phase explains about 110s of the 131s delta.
- Primary checkout has unrelated WIP and must remain untouched.

## Decisions and deviations

| Time | Evidence or decision | Impact |
| --- | --- | --- |
| 2026-09-22 | Repeated explicit waits were not present; tool round trips increased 57.7%. | Optimize interaction guidance and add bounded timing rather than changing DB, workers, or timeouts. |
| 2026-09-22 | Lever-specific grouped verification conflicts with the generic per-combobox re-snapshot rule. | Make the generic rule defer to a stricter provider-specific safe batch rule. |
| 2026-09-22 | Since v0.5.2, main includes a public rebrand and substantial compatible runtime/data changes. | Target v0.6.0, prepared in a separate release commit/PR after performance integration. |
| 2026-09-22 | Three initial comparison runs resolved imports through the primary checkout's editable install despite `PYTHONPATH`. | Treat them as additional baseline only; use the dedicated worktree `.venv` and a new isolated root for optimized evidence. |
| 2026-09-22 | The operator reaffirmed that Codex browser work must use the in-app browser as the default surface. The in-app browser exposes DOM operations, native selects, and file chooser uploads. | Cancel the external Edge cohort and use IAB for real no-submit validation. Edge/Chrome is fallback-only when a concrete IAB capability or session requirement is proven. Do not misrepresent the still-separate unattended `apply` Edge/CDP pipeline as migrated. |
| 2026-09-22 | The cancelled Edge warm-up exited before preview completion. Exact cleanup found no live process and released its owned lease/attempt; `submit_started=0`, no gate and no receipt. | Exclude the cancelled run from performance comparison and retain it only as cleanup evidence. |
| 2026-09-22 | Direct IAB Lever control completed three synthetic no-submit fill/select/readback cycles in 225ms, 193ms and 189ms. | Treat this as browser-control consistency evidence, not as a comparison with the external-agent baseline. |
| 2026-09-22 | The attended IAB worker completed 12/12 bridge operations with no failed or unknown outcome; 7/8 requested values persisted. The intentionally synthetic autocomplete location did not persist, while five ordinary fields except location and all three native selects were verified. | The IAB path correctly detected a custom-autocomplete non-persistence instead of claiming success. No upload, consent, visa answer or submit occurred. |
| 2026-09-22 | Greenhouse and SmartRecruiters loaded and exposed their real forms in IAB. Basic text fields persisted, but controlled email/phone fields were cleared; Greenhouse emitted React hydration recovery errors. | Record this as current site/runtime compatibility evidence. It is not a regression caused by the Lever-only prompt change and must not be hidden by an automatic external-browser switch. |

## Live process ownership

| Process | Owner | Log path | State |
| --- | --- | --- | --- |
| Real IAB Lever/ATS browser runs | `/root` | Current Codex in-app browser plus task validation notes | Active; serial runs only; one page writer |
| Cancelled external Edge warm-up | `/root` | `C:\Users\raede\Desktop\简历\applypilot-local\test-runs\live-lever-optimized-patched-20260922\logs` | Closed; excluded from comparison; no submit/gate/receipt |
| Attended Lever bridge | `/root` | `C:\Users\raede\Desktop\简历\applypilot-local\test-runs\iab-lever-bridge-20260922-b` | Closed cleanly; no submit; page reloaded and tab closed |
| Full local pytest/release build | `/root` | Worktree console output and generated ignored `dist/` artifacts | Complete; 2899 passed, release build succeeded |

## Handoff

- Subagents and external CLI are read-only and may inspect completed logs/code only.
- Only `/root` may run browsers, mutate test databases, commit, push, merge, tag, or publish.

## Next step

Run the release-package smoke gate, then commit and integrate the performance PR. Treat IAB as the normal operator browser surface and preserve the Greenhouse/SmartRecruiters compatibility observations as bounded open evidence.
