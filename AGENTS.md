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
