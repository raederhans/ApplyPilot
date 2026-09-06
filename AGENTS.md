# ApplyPilot Codex execution rules

- Run operational ApplyPilot commands through `..\run.ps1`. The wrapper binds the
  workspace at `..\data`, loads its profile and standing submission policy, and
  applies the local worker and submission limits.
- If the wrapper cannot be used, invoke the installed CLI with an explicit
  `--workspace ..\data` before the subcommand. Never rely on the process current
  directory or the default user profile directory for application data.
- Routine discovery, preparation, and exact-job submission may use the configured
  standing authorization without asking for another confirmation. Preserve the
  existing hard pauses for CAPTCHA/security challenges, assessments, sensitive
  identity or financial material, unsupported legal declarations, and uncertain
  submission receipts.
- Use the dedicated persistent browser profile for authenticated sites. Do not ask
  for, read, or persist password files when an existing authenticated browser
  session or the supported browser-session bootstrap is sufficient.
- A direct-email application must remain on the mailbox route. A rejected or
  incomplete email plan must fail closed and must never fall back to browser-form
  auditing or sending.

## Discovery and employer coverage

- With a reviewed job description but an unusable application entry, consider
  the employer careers page, its official ATS link, or another listing of the
  same role. This is an optional recovery strategy, not a fixed provider order.
  Match employer, title, location and available requisition ID, then check the
  cross-platform application history before submitting. Reconcile an uncertain
  earlier submission before trying another entry. A login/security barrier on
  one site does not prohibit using a legitimate alternative entry; it does not
  authorize defeating that site's challenge or inventing required answers.
- Choose page feedback according to the unresolved question: current DOM and
  accessible names, screenshots, selected states, navigation and exact receipts
  can complement each other. An empty file-property read or missing filename in
  text alone need not mean upload failure. Consider another observation before
  repeating an action; there is no requirement to collect every channel or use
  them in a fixed order. Report once the requested facts or a decisive blocker
  are established.

- CAPTCHA handling: distinguish passive badges/background frames from a visible
  blocking challenge or an explicit verification rejection. Passive infrastructure
  alone need not block normal preparation or an otherwise authorized final click.
  Let the normal verification callback settle and inspect the actual outcome;
  never submit merely to probe, repeatedly retry, manipulate tokens or use solvers.
  Preserve the tab, prepared values, visible error and whether Submit was clicked.
  A blocking challenge can be handed off while the batch continues elsewhere.
  After manual clearance, observe again and check whether the application already
  completed before resuming. For a confirmed rejected attempt, consider the
  exact job's official entry or listing-authorized email after duplicate checks;
  an ambiguous attempted submission still needs reconciliation before another
  route is used. Direct email stays on the mailbox audit/send/receipt route.
- Phone reminder (soft check): inspect the rendered country/flag and number for
  a duplicated prefix or incorrect inferred country. A separate prefix selector
  usually takes the national number; an international-number widget can require
  the full profile number even when it also displays a flag. Recheck when useful
  after resume parsing or country changes. If DOM values are inconclusive, a
  screenshot can help. This adds no hard gate or mandatory observation sequence.

- Indeed popup recovery (observed 2026-09-06): a signed-in job page may emit a
  `Page.windowOpen` request for SmartApply without the in-app browser creating a
  tab. Missing navigation alone is not an authentication failure. If needed,
  inspect the supported browser's new-tab/window evidence and consider opening
  the exact freshly observed application URL in a new tab, or using Edge when
  the user's browser choice allows it. Verify the resulting employer/job and
  form before continuing; never synthesize application IDs or replay a final
  submission. This is a situational recovery option, not a required CDP step
  for every application. Check prefilled surname/given-name fields against the
  configured profile rather than assuming the account's saved order is correct.
  Also review saved contact details: for a separate country-code selector use
  `phone_country_code` there and `phone_national_number` in the number field.
  A signed-in Indeed profile may still prefill an old number or duplicate its
  prefix; check the rendered field if the DOM snapshot omits phone values.
  Resume replacement can reparse and overwrite saved contacts (observed: phone
  reverted to US +1 and city cleared). Recheck contact fields after the resume
  has finished saving, and correct from the configured profile when authorized.

- For a broad search, preserve useful official-company monitoring and spend a
  bounded part of the run discovering employers outside the current shortlist.
  Use `..\run-radar.ps1 radar explore` (default two rotating role queries on both
  LinkedIn and Indeed, five results each), then `radar advance --limit 5`.
  Default to a final batch of 5–10 relevant jobs. Query/field choices are agent
  judgments; no company-size quota or automatic exclusion of familiar companies.
- Inspect each platform's `search_status`, metadata gaps and `search_url`.
  `empty` is one search result, not proof the platform has no jobs. At a network
  failure or missing company/description/link, use the existing visible browser
  session to review a small result page and the employer link. Stop at access
  challenges; do not spend the whole budget retrying one provider.
- Use simple role queries plus Singapore in LinkedIn Jobs and Indeed. Rotate
  fields or broaden one unsuccessful query; do not require a company name or
  the word “startup”. Supplement sparse fields with CareerAxis, SGInnovate or
  Startup SG visible directory review (up to three companies per batch).
- When internship queries return experienced roles, the agent may use
  `radar explore --query "business analyst" --job-type internship --limit 5`.
  Treat this as search refinement, not a permanent eligibility rule. Indeed's
  JobSpy adapter cannot combine its date filter with job type; explicit type
  wins and the run reports the missing date filter. LinkedIn's public results
  can ignore filters even when the request succeeds: inspect the returned
  browser search URL and its visible selected filters before judging coverage.
- Review employer careers pages reached from results/directories. Persist
  reviewed links with `radar import-leads` / `radar import-company-seeds` through
  `run-radar.ps1 -AttendedReview`, then advance them. This flag identifies an
  explicit source-review session, which the authorized agent can conduct; it
  does not require the user to review every record personally. Imported and
  searched records remain unverified until fresh employer evidence is obtained.
- Job-board employer targets also require independent identity review. After
  actually checking the employer page and its company relationship in the
  current visible review session, import that lead with
  `--official-targets-reviewed`. This records a short-lived exact-target review;
  a field claimed by the source file cannot grant trust. Then `radar advance`
  still checks the live structured job evidence. Do not set this flag merely
  because the board provided a link or because the target repeats its name.
- Keep the existing 1–10 score and admission threshold. Missing title taxonomy
  is a reason to inspect duties, not reject. Prefer another company among
  equally suitable jobs; fit evidence and explicit user targets take priority.
- See `docs/multisource-radar.md` for the bounded search and verification contract.
