# ApplyPilot Local security policy

Do not publish suspected vulnerabilities, exposed credentials, or personal data in a public issue.

Use GitHub's private vulnerability reporting flow from the repository's **Security** tab. Include the affected version, a minimal reproduction, and any recommended mitigation. Do not include live credentials or unnecessary personal data.

The project is local-first. Reports should assume that profiles, resumes,
credentials, application evidence, browser sessions, and SQLite databases are
sensitive even when a specific field is not traditionally classified as a
secret.

The optional CloakBrowser backend executes a third-party, source-patched
Chromium binary. Use only the pinned, signature-verified upstream distribution;
do not use `CLOAKBROWSER_SKIP_CHECKSUM`, custom mirrors, or the `cloakserve`
proxy. Keep its CDP endpoint on `127.0.0.1`, store profiles only under the
ApplyPilot data directory, and never place a license key in the repository.
Browser selection does not weaken portal policy, authorization, CAPTCHA/MFA,
assessment, identity-document, account-recovery, or receipt-admission gates.
