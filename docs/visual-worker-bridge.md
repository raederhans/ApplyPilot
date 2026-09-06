# Attended in-app browser workers

## Current migration entry

Discovery, preparation and explicitly authorized submission can run on an actual in-app browser tab, with no
CDP port or shared-session assertion. In the supported Browser session, use:

```js
const host = await createInAppBrowserHost({ directory, tab, phase: 'discovery' });
```

The host derives its target from the returned tab, rejects a second owner for
that tab, and exposes URL/title/tab identity with every observation. Launch
`applypilot --workspace <workspace> browser-work --bridge-dir <directory>
--task-file <goal.txt> --phase discovery` (or `prepare`) with the source-tree
Python environment / workspace wrapper. `scripts/run_browser_worker.py` is also
available as a standalone runner for the same API.
The standalone runner also accepts `--phase submit`, but only for a matching
submit host whose top-level `submission_authorized` is the boolean `true`.
The task text cannot grant that authority or upgrade a prepare host. Discovery
and prepare remain the ordinary starting phases. Authorization is scoped to the
bound application, not permission for a worker to submit other jobs.
The goal is ordinary task prose. The worker uses the configured Codex model
and only the page-operation MCP, without shell, standalone Playwright, native
desktop input or web search. It can observe DOM or screenshots, click, scroll,
type, press keys and navigate to an exact link from its current observation
in the same tab. Tool discovery remains available when MCP tools are deferred.

This is an **attended operational entry**, not just a fixture smoke. The current
Codex task still services `peek/execute` through the supported Browser runtime.
It is not connected to the unattended `apply` submission pipeline and does not
change that pipeline's default browser. A successful child exit means its turn
finished, not that a job was applied to. Read its result, including auth_required,
unknown action outcomes or missing evidence. A total worker timeout terminates
the worker and its child processes and returns 124.

`host.inspect()` independently re-reads the same tab after preparation. Its
result includes the actual page identity and the host's explicit submission
authorization (false outside submit phase). It is a visible-page review, **not** a replacement for
the existing pre-submit audit (uploads, answer provenance, controls, gate and
receipt reconciliation). The explicit submit phase enables attended execution;
it does not connect the worker to the unattended pipeline or prove that its
gates and ledger have been migrated. Confirm those checks with the host before
submitting, and verify the matching receipt after submitting once.

## Authentication, uploads and batch continuation

Workers prefer guest/direct application when available. They may explore an
existing Google SSO option for the user's identified account and application
sign-in consent. Ambiguous accounts and unrelated permission grants need host
handling. The availability of a signed-in Edge or Chrome profile does not prove
that the IAB tab has the same session; do not copy cookies or password stores.

Email OTP retrieval for the current employer and secure password filling are
host handoffs under the user's authorization. Passwords and OTPs must never be
included in ordinary `type_text`, worker goal text, queue payloads or logs.
The host must verify a supported secure capability before claiming automated
login. Otherwise return `auth_required` and preserve this application. After a
handoff, re-observe the same tab before continuing.

For attachments, pass trusted absolute paths in `artifacts: {resume: absolutePath}`
when creating the host. The worker sees only the available `artifact_ids` and
calls `upload_artifact` with `artifact_id` and an observed upload control's
`node_id`. The host starts `waitForEvent('filechooser')` before clicking, then
calls `chooser.setFiles()`. Verify the accepted filename/status on the page.
Do not repeatedly click Upload merely because a native picker is outside the
page screenshot. If the supported chooser fails, preserve progress for host
handling. Selecting a file is not proof that an ATS accepted the attachment.

Use supplied authoritative materials for ordinary questions. A missing required
fact produces `needs_fact` for that job; the coordinator continues other jobs
and collects questions after the batch. Do not fabricate answers. Assessments,
security challenges, sensitive identity/financial material and unsupported legal
declarations likewise preserve the affected job for host handling. An uncertain
submission remains `submission_uncertain` and must not be replayed.

Worker authorization checks and prompt contracts have narrow unit coverage;
this does not establish live SSO, secure password filling, ATS acceptance or a
completed worker submission. Those require separate runtime evidence.

### Follow-up application observations, 2026-09-06

An attending IAB session subsequently completed four real applications across
Workday, Phenom and SuccessFactors. Email OTP verification and Google account
linking succeeded; Google sign-in also worked for a deferred draft. These are
attending-session results, not proof of unattended worker authentication.
The earlier blocked check below is historical.

Treat upload-before-entry as a useful choice when parsing is offered, not a
mandatory sequence. One observed form cleared unsaved answers after upload;
this does not establish a general ATS behaviour or its cause. Inspect the
settled state, preserve correct values, and repair actual changes only. Roche
listed accepted attachments on final review despite earlier delayed feedback.
A transient alert alone should not trigger a second upload or whole-form refill.
Use current controls to recover from focus changes and verify actual selected
options. No employer-specific reset rule or fixed action-count test is needed.

### Feedback choices and completion boundaries

Choose observations to answer the remaining question. DOM/accessibility is
useful for exact accessible names, required markers and selected values; a
screenshot can resolve a native attachment display or ambiguous layout. A page
URL, settled status or matching receipt establishes outcomes that a successful
click cannot establish. These sources complement each other; there is no fixed
sequence or requirement to collect every source after every action. In this
bridge, an action already returns a fresh observation, which can ground the next
action without another `observe` call.

In the AI Agent batch, an attachment filename was visible in a screenshot even
though DOM text and a file-property read did not expose it. That absent readout
was inconclusive, not evidence to upload again. A button's accessible name also
differed from its short visible text, and required labels included an asterisk.
Inspect current controls when a locator misses, and check actual checkbox or
selection state before choosing another supported interaction.

Return once the requested facts or a decisive blocker are established. Further
scrolling or screenshots should answer a remaining question. The Infineon
inspection reached a guest form but expired at its configured 180-second total
deadline. The retained final bridge request was a screenshot scroll with a
successful response; retained evidence does not isolate model time, host service
latency or redundant observation as the cause. The prompt now makes feedback
reuse and completion boundaries explicit; the timeout is unchanged, and improved
live completion time remains unverified. Employer login, Singpass and required
identity information remain external handoffs.

When a JD is already known but its current platform cannot accept an application,
consider the employer careers site, its official application entry, or another
recruiting platform carrying the same opening. This is a flexible search option,
not a required platform list or sequence. A worker restricted to its bound tab
hands broader searches to the host/coordinator. Before progressing, match the
company, title, location and available requisition ID, and consult the application
ledger across platforms. Reconcile any uncertain prior submission before another
attempt anywhere; changing platforms does not remove that uncertainty. Using a
normal official alternative does not bypass the original site's login or CAPTCHA,
and that site's barrier need not stop legitimate application paths elsewhere.

The 2026-09-06 small-platform test also informs CAPTCHA handling: passive badges
and background frames are not themselves blocking challenges. After an authorized
submit, observe the site's own verification outcome. An explicit rejection should
retain the error and prepared page for host handling; an ambiguous outcome needs
receipt reconciliation before another attempt. Manual clearance requires a fresh
observation, including whether the form already submitted. The worker does not
solve challenges or manipulate tokens. A confirmed rejected route can be handed
to the coordinator for a same-job official alternative without halting the batch.

Phone formatting is a soft reminder in both visual-worker and CLI prompts.
Inspect the rendered prefix/flag and number: some controls separate the country
code, while others infer it from a full international number. The Cynapse widget
interpreted a bare national number as another country's number, then displayed
Singapore correctly with the full +65 number. Consider visual feedback when DOM
values are inconclusive, and recheck after parsing or country changes when useful.
This guidance adds no hard gate, fixed observation sequence or extra required field.

### Upload migration check, 2026-09-06

The actual IAB adapter selected the existing 93,208-byte resume PDF through
the documented chooser API on `scripts/fixtures/upload-smoke.html`. An actual
CLI worker then independently used two MCP operations (observe, upload_artifact)
and read the matching filename and size. No desktop input or network submission
was used. Python bridge/worker checks: 36 passed; JavaScript host checks: 7 passed.

Roche 202607-119061 was reopened, but its application flow required fresh email
verification. MSD R400206 required Create Account/Sign In after Apply Manually
in both IAB and the connected Edge profile; the inspected MSD sign-in page had
email/password and no Google option. The IAB `browserAuth` request capability
was unavailable, and Edge advertised no browserAuth capability. No new
application was submitted. Password export, cookie copying and raw secret
entry are not implemented workarounds. Existing Edge connectivity does not
establish Google SSO or an authenticated employer session.

These changes live in the migration source tree. The unattended apply driver
and installed source copy have not been switched by this validation.

For user login/takeover, `host.pause('auth_required')` invalidates prior
observations and cancels unclaimed pending operations promptly. Preserve the
tab and original target. After the user is done, `host.resume()` observes that
same tab before making the host available; it does not navigate or replay old
input. The supervisor can return to the already recorded job URL in the same
tab if the login flow did not return there, then invalidate/reobserve before
continuing. Close a completed host with `host.close()`. No desktop polling
daemon or automatic per-click execution loop is installed.

## Roles and tools

- Coordinator: candidate queue and status; no click-by-click workflow planning.
- Discovery agent: optional recency, list, Posts and people exploration. It owns
  a discovery tab and returns leads with visible evidence.
- Application agent: one job and one application tab. DOM and visual controls
  are alternatives within this role, not separate agents or website-specific
  worker types. Material preparation can run independently without page input.
- Host review/submission/receipt: retain the current application gates and
  ledger. A different browser with the same URL is never audit evidence.
- Operator handoff: actual login/security/desktop exceptions; native Computer
  Use is not a dependency of ordinary web discovery or preparation.

Prefer guest or direct routes when offered. Search can continue while signed
out if the page permits it; only a real authentication barrier needs login.
24-hour and optional 8-hour searches are preferences, not eligibility gates.
Treat requested URL filters as unverified, and distinguish reposted from first
publication for reporting only. Reposts remain eligible under recency preferences; only exact prior submissions are excluded as duplicates. Ordinary exploration choices do not require new approval steps.

## Migration checks, 2026-09-06

- Actual discovery worker: opened Infineon's Internship - AI Solutions &
  Analytics from LinkedIn preferences in its original IAB tab. It distinguished
  the list's Posted wording from Reposted in the details and identified the
  company application entry without clicking Apply. Host inspection read back
  the same tab and job. This used the operational worker, not the fixture runner.
- Actual prepare worker: read Indeed's Workato Intern, Data Engineering (2942)
  and reported auth_required from the explicit account requirement. It left the
  original tab untouched. Login completion/return remains untested without a
  user login; this is not evidence that authentication is automated.
- Local guest fixture: the main `browser-work` CLI ran a worker which selected
  guest, entered Migration Test, and read B-202 and the entered value back. Host
  inspection and pause/resume retained that same tab. No final submit existed.
- Two real tool-contract mismatches were fixed: actions may request their result
  observation format with `mode`, and browser text entry may specify an observed
  text-input node. The latter focuses and types in that input; action controls
  are not accepted as text-entry targets. Focused-input typing remains supported.
  The targeted-input fix was also exercised in the real local Browser runtime.
- Unit checks cover IAB/CDP identity separation, stale observations, pause
  cancellation, interrupted inputs, observed-link navigation, targeted typing,
  worker phase/tool isolation and process timeout cleanup. Earlier application
  navigation and SuccessFactors identity cases also received a narrow rerun.

These checks qualify the attended discovery/prepare entry and same-page visible
review. They do not qualify full unattended submission, file uploads, a migrated
SubmissionGate, receipts, or end-to-end throughput.

The final guest rerun also used per-invocation worker context slimming:
`skills.max_context_tokens=1`, `features.plugins=false`, `features.apps=false`,
`features.multi_agent=false`, and `agents.enabled=false`. The attending Codex
host still applies the Browser skill. Global Codex configuration is untouched.
On this same local goal, page calls fell from five (including one rejected
targeted-input attempt) to three without errors. Reported total input tokens,
including cached input, fell from 228,128 to 65,731. Both context slimming and
fewer turns contributed; this is not an isolated benchmark or a throughput
claim. The final worker and independent host observation both confirmed B-202
and Migration Test. Combined narrow checks: 13 worker, 10 transport, 2 legacy
wiring, 4 host, 10 search, 32 earlier navigation/SuccessFactors cases passed.

## Legacy CDP visual attachment

This is an opt-in, prepare-only bridge from an isolated worker to an attending
Codex task's supported Browser or Computer Use tools. It is not an unattended
desktop daemon. The attending task reads the applicable Browser/Computer Use
skills, selects one returned tab/window, and services one request at a time.
The worker receives the actual observation and continues its own turn.

## Attach and operate

After supported Browser setup and reading its documentation, import
`scripts/visual-bridge-host.mjs` in that same supported JavaScript session.
Call `createVisualHost({directory, adapter: browserAdapter(tab), target})` with
a fresh queue directory and an already selected tab. For Computer Use use
`computerAdapter(sky, returnedWindow)` only after its required setup and target
selection. Never invoke this module as a standalone desktop automation process.

Attachment now first performs a real observation; a failed observation does not
advertise an active host or invite a worker to wait for an unusable controller.

The target records `application_url` and `cdp_port`. These are binding metadata,
not connection instructions. For the real application launcher, the attending
task must first verify that this is the worker's actual browser page/session,
then set `worker_session_verified: true` in the target. An unrelated Codex App
tab is not a worker CDP page just because its URL matches. Do not transfer
cookies, claim a shared session without evidence, or set this flag for the
standalone local fixture. Shared-session handoff into the application pipeline
has not yet been live-qualified.

Set `APPLYPILOT_VISUAL_BRIDGE_DIR` for the attended application launch. Only a
fresh host bound to the same CDP port and starting application URL is exposed
in prepare. Other phases and ordinary unattended launches keep their existing
tools. The tool is `applypilot_visual.visual_operation`; Codex may require
tool discovery before it is callable.

Use `host.peek()` to inspect a pending request without acting, then inspect the
current visible observation before `host.execute(request_id)`. This performs
one operation and returns a fresh observation to the waiting worker. Requests
support DOM/accessibility observations, screenshots, click, scroll, text and
navigation keys; arbitrary code and target selection are not accepted. Browser
navigation is restricted to exact links in the current observation. These
primitives do not themselves grant authorization for any
external action. The attending task applies the tool's policies and the user's
scope before executing a request. This legacy prepare attachment excludes final submission,
credential entry, security challenges and assessments from visual handoff.

Inputs require the latest `observation_id`. Use `host.invalidate()` after
another controller/user changes the page, then re-observe before further input.
Never issue Playwright and visual inputs concurrently. The bridge serializes
its own requests; cross-tool coordination remains the attending task's job.
`host.peek()` refreshes the heartbeat; `host.close()` marks the host stopped.
There is deliberately no unattended polling pump. Runtime/adapter failures
stop the host, invalidate observations and require deliberate fresh attachment.

Unclaimed requests expire and are cancelled atomically. Claimed requests with
no response produce `outcome_unknown` and block new requests while unresolved.
Inspect the page and reconcile that request before starting a fresh session;
do not delete a live claim or replay its input. Default request wait is 45 s,
optionally up to 120 s for attended smoke tests.

## Narrow smoke test, 2026-09-06

Serve `scripts/fixtures` on loopback port 8766 and attach the selected Browser
tab to `visual-worker-smoke.html`. Run `scripts/smoke_visual_worker.py` with
`--bridge-dir` from the source environment while the attending task services
requests. The script only accepts this local fixture. It takes the configured
Codex model; `--codex-executable` can select a verified installed executable.

Observed results:

- An actual isolated Codex worker called the MCP four times: observe, choose
  Last 8 hours, open Product intern, and enter Apply as guest. It read B-202
  before proceeding and confirmed `Guest application entry: B-202` afterwards.
- A separate child-process probe received one real PNG observation. A subsequent
  coordinate scroll moved the list down to Research intern, verified visually.
- Transport lifecycle checks: 7 passed. Worker configuration/session binding:
  2 passed. Search-window tests: 10 passed. No full regression run.
- The PATH CLI could not run the configured gpt-6-astra model. The already
  installed Codex App executable ran the smoke. No global installation changed.
- Windows Computer Use listed windows, but its runtime then stopped because it
  could not confidently determine the browser URL for policy enforcement.
  Desktop input stopped there. Its adapter is implemented but not live-qualified.

This proves supervised Browser control and result return on a local fixture.
It does not prove real LinkedIn/Indeed filtering, authentication, cross-browser
session sharing, native Computer Use input, or unattended application throughput.
Do not promote the visual bridge to the default driver based on this smoke.

## Follow-up: executable selection and screenshot control

The Windows worker resolver now compares verified stable versions from the
installed App bundle and PATH/npm candidates, selecting the newest. It resolves
this machine to 0.153.4 without a smoke-only executable override. An explicit
`APPLYPILOT_CODEX_EXECUTABLE` takes precedence and fails clearly if missing.
The global npm installation and PATH are unchanged. Four focused resolver tests
passed; the actual resolved executable returned `codex-cli 0.153.4`.

`smoke_visual_worker.py --visual` starts with a screenshot and asks the worker
to choose Last 24 hours by coordinates before receiving DOM data. The actual
worker clicked (488, 188) from that image, observed Last 24 hours, then used DOM
node clicks to open Product intern / B-202 and confirm its guest entry. This
demonstrates screenshot control and structured reading within one bound tab.

For a native Computer Use control, the supported Edge extension successfully
opened and read https://example.com/. The returned native window title was
Example Domain, but native `get_window_state` still stopped on the same URL
policy check. The issue is therefore not confined to an empty/new tab. No native
input or protection bypass was attempted after that stop. The in-app Browser
visual API works independently; this does not repair native Windows Computer Use.
The two focused host tests cover stale observation/interruption and rejecting
an unobservable target before advertising readiness.
