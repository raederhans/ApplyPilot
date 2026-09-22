# Task

## Current status

Stages 1-3 complete; Stage 4 in progress.

## Checklist

- [x] Attribute the Lever variance from two real no-submit logs.
- [x] Audit release topology, version, assets, and workflow.
- [x] Implement privacy-safe paired tool timing.
- [x] Remove conflicting Lever/native-select observation guidance.
- [x] Run focused and full local verification.
- [x] Run repeated real Lever plus multi-site no-submit verification in the in-app browser under one owner.
- [ ] Commit, push, open and merge the performance PR.
- [ ] Prepare and merge v0.6.0 release metadata.
- [ ] Tag and verify the GitHub Release and public artifacts.

## Validation evidence

| Command or check | Result |
| --- | --- |
| Baseline live runs | 206.6s / 337.6s; 26 / 41 browser calls; no submit |
| Static performance review | Interaction amplification confirmed; DB/acquisition not a bottleneck |
| Release audit | v0.6.0 recommended; GitHub Release supported; PyPI disabled |
| Focused verification | 467 tests passed; scoped Ruff and `git diff --check` passed |
| Full verification | First run exposed 42 missing-Playwright-runtime failures; after installing the matching Chromium runtime, all 42 reran successfully and the complete suite passed: 2899 tests in 196.60s |
| Release build | `scripts/build_release.py` built wheel, sdist, bundle and `SHA256SUMS` for the current 0.5.2 branch state |
| Clean install smoke | `scripts/smoke_release.py` passed wheel install, `resume-route --help`, version, init, doctor and dashboard generation |
| Browser routing | IAB navigation and live Lever form discovery passed; external warm-up was cancelled and cleaned with zero submit/gate/receipt |
| Repeated Lever IAB direct cycles | 225ms / 193ms / 189ms for five synthetic text writes, three native selects and one grouped readback; final reload cleared the page |
| Attended Lever IAB worker | Exit 0; 12/12 host operations completed; 7 form readbacks reused; action p50 152.4ms, p95/max 221.9ms; observation p50 15.1ms, p95/max 148.4ms; 7/8 requested values verified; location autocomplete correctly reported non-persistence |
| Multi-site IAB checks | Lever full ordinary/native-select path passed; Greenhouse and SmartRecruiters loaded real forms and persisted basic text fields, while controlled email/phone writes were cleared; no submit on any site |

## Open risks and remaining work

- The optimized prompt has direct IAB interaction-consistency evidence, but no valid same-controller end-to-end wall-clock comparison; do not claim that the external-agent baseline was improved by the patch.
- Tool durations are advisory nested measurements and must not be summed as wall-clock time when calls overlap.
- The product's unattended `apply` launcher still owns an Edge/CDP runtime; IAB is the default operator/browser-validation surface, not yet a drop-in unattended backend.
- Greenhouse/SmartRecruiters controlled email/phone fields remain a bounded IAB compatibility gap; Greenhouse emitted React hydration recovery errors. This does not affect the Lever-only prompt rule but prevents claiming universal IAB form compatibility.
- Release title/workflow still use CapyPilot and current package version remains 0.5.2.
