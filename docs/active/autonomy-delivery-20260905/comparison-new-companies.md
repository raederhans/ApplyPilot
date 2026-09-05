# New-company qualification and requirement parsing

Date: 2026-09-05. The user redirected real qualification toward companies without prior applications. Historical company checks used local jobs, application_attempts, application_receipts, apply_attempts, apply_status and applied_at; absence here is not a claim about unrecorded external applications.

## Candidate selection

| Employer | Source and observed qualification | Action |
| --- | --- | --- |
| Temasek | Official Data Analytics Intern, Performance Analytics (Jan-Jun 2027), requirement ID 12169; no prior local employer record; current fit score 7 | Imported exact official description; qualified existing resume after the parsing repair below; real canary owned by root |
| Allium | Official Ashby Engineering Intern - General / AI page renders; no local application history | Current router has no sufficiently confident subtype for this mixed role; no application started |
| Unravel Carbon | Current official Machine Learning Intern posting differs from the old stored application URL; no local application history | Registered factual sources do not clear the named ML requirement; no application started |
| Syfe | Official ongoing internship expression-of-interest page renders; no local application history | Imported official listing; broad all-role opening has no supported subtype; no application started |

Manus GTM and NBCUniversal Data Analyst leads were excluded after current browser/page observations showed page-not-found and expired respectively. ShopBack, StraitsX, Shift Technology, GoTo, NCS and other previously attempted employers were excluded at company level, even where a different job had no receipt.

## Same-input Temasek comparison

Official source: https://jobs.temasek.com.sg/job/Data-Analytics-%26-Engineering-Intern%2C-Performance-Analytics-%28Jan-Jun-2027%29-238891/1368996857/

The job lists Python/R/SQL as requirements and cloud/visualization/version-control tooling as preferences. The previous parser let the phrase introducing experience override a later preference qualifier and a negated requirement marker. It therefore classified AWS and Git as hard gaps.

With the identical job description, current profile and registered material, the repaired parser moved the optional skills to preferred_skills. The route changed from manual_review (required coverage 0.714286; hard gaps AWS/Git) to reuse_exact (required coverage 1.0; hard gaps empty). Overall routing score changed from 0.876786 to 0.935714. These are deterministic routing scores, not hiring probabilities. No applicant skill, material content, machine-validation threshold or authorization rule was changed.

The repair distinguishes a marker that directly introduces a later skill from a marker qualifying the preceding list, and excludes negated markers. Tests cover trailing preferences, preferred-but-not-required, prefix requirements, mixed clauses, negated preference and later independent required sentences. Root verification: 38 tests passed in 5.00s; Ruff passed.

## Real-run boundaries

The initial NCS case was interrupted in prepare when the user requested new employers. The first turn had reached its 480-second deadline; its single same-page recovery was then stopped by root. The ledger has submit_started=0 and no exact-job receipt. Its 646.5-second wrapper duration is an interrupted observation, not a completed comparison or submission.

The Temasek case uses the original operational wrapper with candidate PYTHONPATH, one worker, one exact job, min-score 7, prepare Sol/medium and the normal high-effort submit default. Semantic batch is canary, App Server is off. Unknown providers safely return to the existing Agent; a feature flag does not grant new provider authority. Actual terminal outcome and receipt reconciliation are recorded in task.md after the run completes.

Broader subtype support and missing factual skill evidence are separate issues. This wave does not classify broad openings by invention or add unverified skills simply to increase the test cohort.

## Live outcomes and remaining login adapter gap

Temasek first attempt: prepare Agent about 79 seconds, followed by an operator wait. The live login host was career2.successfactors.eu; the registry only listed successfactors.com. SAP documents both hosting suffixes ([career-site CSP](https://help.sap.com/docs/successfactors-recruiting/setting-up-and-maintaining-sap-successfactors-recruiting/enabling-content-security-policy-for-career-site), [Recruiting microsites](https://userapps.support.sap.com/sap/support/knowledge/en/2146040)). The narrow provider repair recognizes the European credential-relay host without opening other capabilities. Root checks: 27 passed in 0.53s.

The one retry still returned login_issue after about 69 seconds. Read-only inspection of the owned live tab showed Email Address:* bound to a visible text input whose id/name were username, and both credential fields remained empty. This initially suggested a selector gap, but a later complete-page probe found both the old selector and the new exact-label helper can select the field. Therefore that suggestion is not an established cause of the second failure. The label-only fallback is supported by isolated DOM fixtures, not by a claim that it repaired this full login flow.

Both Temasek attempts remained pre-submit with submit_started=0. Root ended only the exact waiting attempts using finalize_application_attempt with cancellation evidence. The worker then expired its handoff and cleaned its browser. The subsequent stale-attempt mark_result was rejected by the existing guard; this did not change the current job to success or erase receipts. No human-response record was invented. These are explicitly cancelled test attempts, not completed applications.

Final receipt query at this checkpoint: Temasek=0, Allium=0, Unravel Carbon=0, Syfe=0. No new-company application is claimed as submitted. The printed runtime cost of $0.000 is not evidence that provider usage was free; authoritative monetary cost was unavailable. Live timings concern different pages or successive adapter states and cannot establish a model-effort speedup.

Further local review reproduced an optional/required mixed-clause parser bug (Preferred Python, SQL is required.; Python optional, SQL required.). The incremental correction preserves Python as preferred and SQL as required while keeping shared skill lists intact. Root checks: 40 passed in 4.91s; Ruff passed.

## Candidate-source consistency finding

After both live attempts, root verified a test-harness/source consistency gap: the generated Codex STDIO MCP env_vars did not forward PYTHONPATH to the three project-owned Python helpers. A fresh subprocess without candidate PYTHONPATH imports provider_registry from the installed main checkout and rejects successfactors.eu; the same subprocess with candidate PYTHONPATH imports this worktree and recognizes it. OpenAI documents env_vars as the explicit list of variables to allow and forward ([MCP configuration](https://developers.openai.com/codex/mcp/)). Thus changing the wrapper's parent PYTHONPATH alone did not establish that every tool in the live canary used the same candidate revision. This limits conclusions from the two live failures; neither proves the updated relay ran.

The narrow runtime follow-up forwards the already configured trusted parent PYTHONPATH only to credential_relay, applypilot_ats and applypilot_control. It does not forward all environment variables or alter mailbox/Playwright servers. Its direct subprocess/command checks are required before release. No credentials or factual answers are exposed by this source-path correction.

Root's read-only complete-page probe returned known_ats=true, legacy_match=true, label_match=true, selected_id=username, filled=false, submitted=false. The assertion expecting legacy_match=false correctly failed, invalidating that proposed real-page A/B. Synthetic label-only fixtures remain distinct evidence. Whole browser tier passed 53 tests in 92.82s; the subsequent release build/privacy audit passed before the final source-path follow-up.

Source-path correction accepted: root ran 82 affected runtime/identity/provider checks (10.90s), Ruff and the final release build/privacy audit, all passing. No further real submission was used to claim end-to-end qualification. The live receipt total remains zero; source-consistent end-to-end login/application remains a qualification gap.
