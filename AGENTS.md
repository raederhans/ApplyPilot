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
