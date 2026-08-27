# ApplyPilot Local

**Local-first, evidence-driven job application workspace.**

ApplyPilot Local helps a job seeker discover verifiable openings, score fit,
reuse validated resume variants, prepare application materials, assist with
browser-based forms under explicit authorization, and reconcile submission
receipts. Candidate data, credentials, generated documents, and runtime logs
stay on the user's machine by default.

This repository is an independent continuation of the inactive upstream
[Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot). The
Python import package and CLI remain `applypilot` for compatibility; the
distribution and product identity are `ApplyPilot Local`.

## Product principles

- **Evidence before status:** a clicked Submit control is not treated as a
  successful application. Only decisive, identity-matched confirmation can
  create a durable submitted record.
- **Local-first privacy:** profiles, resumes, databases, credentials, browser
  evidence, and generated artifacts are ignored by Git and remain local.
- **Human authority:** unsupported answers, CAPTCHA, assessments, identity or
  financial documents, account recovery, and security changes stop for review.
- **Source truth:** official jobs stay separate from social or community leads
  until a fresh authoritative listing verifies them.
- **Reusable work:** validated resume artifacts can be reused only when the
  job fingerprint and evidence coverage still match.

## Capabilities

| Surface | What it provides |
| --- | --- |
| Official radar | Structured company/ATS discovery with complete, partial, blocked, and skipped source states |
| Job-board discovery | Optional JobSpy integration for Indeed, LinkedIn, Glassdoor, ZipRecruiter, and Google Jobs |
| Fit and materials | LLM-assisted scoring, truthful resume tailoring, cover letters, PDF validation |
| Resume library | Content-addressed artifacts, subtype routing, validation state, and hard-gap review |
| Application runtime | Explicit authorization manifests, readiness decisions, isolated browser workers, manual takeover |
| Receipt reconciliation | Exact job/company/title matching, decisive confirmation evidence, idempotent status updates |
| Opportunity workbench | Local browser UI for eligible roles, fit evidence, source quality, and verified form links |
| Local status | SQLite-backed application history, unanswered questions, source observations, and terminal dashboards |

## Installation

Python 3.11 or 3.12 is recommended for the complete workflow; the core and
official radar also run on Python 3.13. The quickest released installation is
an isolated command-line app managed by `pipx`:

```bash
pipx install applypilot-local
```

Until the first tagged PyPI release, install the current repository directly:

```bash
pipx install "git+https://github.com/raederhans/ApplyPilot.git"
```

An extracted GitHub release bundle or source checkout also includes a guided,
cross-platform installer. It bootstraps `pipx`, verifies a bundled wheel when
present, and never copies local profiles, credentials, resumes, or databases:

```bash
python install.py
```

The broad third-party job-board connector is intentionally optional because
its upstream package currently pins an older NumPy line:

```bash
python install.py --with-jobboards  # Python 3.11-3.12
```

Then initialize and verify the local workspace:

```bash
applypilot init
applypilot doctor
applypilot --version
```

For a repeatable import from files you already maintain, provide all three
inputs together. ApplyPilot validates them, refuses to replace existing files
unless `--force` is explicit, and does not collect API keys or browser
credentials:

```bash
applypilot init --resume resume.txt --profile profile.json --searches searches.yaml
applypilot dashboard --no-open
```

`applypilot doctor` reports optional capabilities separately; a missing
JobSpy installation does not prevent the official-company radar, resume
library, status tools, or manually imported jobs from working.

## Common workflows

### Discover and prepare

```bash
applypilot radar collect
applypilot radar report --hours 24
applypilot run discover enrich score tailor cover pdf
```

### Reuse validated resumes

```bash
applypilot resume-library-sync
applypilot resume-library-status
applypilot resume-route --url <verified-job-url>
```

### Review and apply

```bash
applypilot review-readiness
applypilot apply --dry-run --url <verified-job-url>
applypilot authorize-batch --url <verified-job-url>
applypilot apply --authorization-file <batch-manifest.json>
applypilot reconcile-receipts --file <receipt.json>
```

The default browser backend is the installed Edge/Chrome runtime. An optional,
isolated CloakBrowser backend is available for authorized sites that reject a
normal Playwright/CDP session because of automation fingerprinting:

```bash
python -m pip install -e ".[stealth]"
applypilot apply --dry-run --url <verified-job-url> --browser-backend cloak
applypilot apply --dry-run --url <verified-job-url> --browser-backend auto
```

`auto` starts with Edge and makes at most one CloakBrowser retry only for an
explicit bot/WAF block. It never retries a CAPTCHA, assessment, selector
failure, authentication failure, or any operation after submission begins.
CloakBrowser is a browser runtime, not a second form-filling agent: Playwright
remains the structured interaction driver on both Edge and Cloak. Independent
Edge workers may run in parallel; workers that actually need Cloak share one
serialized fallback lane unless the configured license explicitly permits
concurrency.

`--interaction-mode auto` also teaches the isolated Playwright agent when to
request a bounded Windows Computer Use handoff for a genuinely visual-only or
native control. Computer Use is not injected into the isolated CLI process, so
the request fails closed for an outer agent to handle with a fresh observation;
it is never used for file pickers, CAPTCHA/MFA, permissions, assessments, or a
second final-submit attempt. Use `--interaction-mode playwright` to disable
that handoff request.

CloakBrowser uses its own `cloak-workers/` profiles and never clones the daily
Edge profile. ApplyPilot disables CloakBrowser auto-updates and pins the
keyless binary unless `APPLYPILOT_CLOAK_VERSION` or a separately managed
`CLOAKBROWSER_LICENSE_KEY` selects another admitted build. The upstream binary
license prohibits automated account creation and redistribution; those flows
must continue with the ordinary/manual path.

Dry-run and preview states are not submission receipts. The application
runtime deliberately keeps uncertain outcomes as `submission_uncertain` until
reconciliation proves the exact application was accepted.

## Architecture

```text
official ATS / optional boards / manual imports
                    |
                    v
        discovery observations + jobs
                    |
                    v
        SQLite identity and state contracts
          |            |             |
          v            v             v
      fit scoring  resume library  status history
          \            |             /
           \           v            /
            readiness + authorization
                       |
                       v
              isolated browser worker
                       |
                       v
              receipt reconciliation
```

The architecture keeps discovery, preparation, authorization, browser
execution, and receipt admission as separate boundaries. A producer cannot
declare its own output successful merely because it completed without an
exception.

## Local data and security

The default workspace is `~/.applypilot/`. Do not commit or share:

- `profile.json`, resumes, generated PDFs, cover letters, or SQLite databases;
- `.env`, API keys, credential records, browser profiles, or verification codes;
- application screenshots, receipts, logs, worker attachments, or
  `chrome-workers/` / `cloak-workers/` browser profiles.

Credentials are read only at execution time. The project does not endorse
CAPTCHA bypass, identity-document automation, account recovery, or hidden
submission. Review [SECURITY.md](SECURITY.md) before reporting a vulnerability.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check src
pytest -q
python scripts/build_release.py
```

The release builder audits archive contents and metadata, creates wheel and
source distributions under `dist/python/`, produces a verified install bundle,
and writes SHA-256 checksums. On Windows, `--no-isolation` is available when an
antivirus aggressively scans temporary PEP 517 build environments. CI runs
lint, the complete test suite, and these package checks on pushes and pull
requests. See [CONTRIBUTING.md](CONTRIBUTING.md) for repository conventions and
[docs/product-core.md](docs/product-core.md) for the product and frontend boundary.

## License and provenance

ApplyPilot Local remains licensed under
[GNU AGPL-3.0-only](LICENSE). The original ApplyPilot authors retain their
copyright in the upstream work; fork modifications are distributed under the
same license. See [NOTICE.md](NOTICE.md) for provenance and the network-source
obligation that matters if this code is later offered as an interactive
service.

This repository is not affiliated with applypilot.app, useapplypilot.com, or
other products using a similar name. “ApplyPilot Local” identifies this
independent fork and its local-first operating model.
