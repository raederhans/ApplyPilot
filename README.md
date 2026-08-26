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
| Local status | SQLite-backed application history, unanswered questions, source observations, and dashboards |

## Installation

ApplyPilot Local currently installs from this repository. Python 3.11 or 3.12
is recommended for the complete workflow; the core and official radar also run
on Python 3.13.

```bash
git clone https://github.com/raederhans/ApplyPilot.git
cd ApplyPilot
python -m venv .venv

# Windows
.venv\Scripts\python -m pip install -e .

# macOS/Linux
.venv/bin/python -m pip install -e .
```

The broad third-party job-board connector is intentionally optional because
its upstream package currently pins an older NumPy line:

```bash
python -m pip install -e ".[jobboards]"  # Python 3.11-3.12
```

Then initialize and verify the local workspace:

```bash
applypilot init
applypilot doctor
applypilot --version
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
- application screenshots, receipts, logs, or worker attachments.

Credentials are read only at execution time. The project does not endorse
CAPTCHA bypass, identity-document automation, account recovery, or hidden
submission. Review [SECURITY.md](SECURITY.md) before reporting a vulnerability.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check src
pytest -q
python -m build
```

CI runs lint, the complete test suite, and package build checks on pushes and
pull requests. See [CONTRIBUTING.md](CONTRIBUTING.md) for repository conventions.

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
