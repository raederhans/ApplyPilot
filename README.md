# CapyPilot

[English](README.md) | [简体中文](README.zh-CN.md)

**A local-first, evidence-driven workspace for running a careful job search.**

CapyPilot helps one job seeker discover verifiable openings, compare fit,
prepare truthful materials, complete supported application forms under explicit
authorization, and confirm what was actually submitted. Profiles, resumes,
credentials, browser sessions, receipts, and runtime logs stay on the user's
machine by default.

CapyPilot is built for supervised execution, not blind bulk submission. A
clicked **Submit** button is not counted as success; only decisive evidence
matched to the exact job can create a durable submitted record.

## Current status

- **Beta, local-first, and CLI-first.** The product runs on the user's machine;
  there is no hosted CapyPilot service, account system, or cloud sync.
- **Useful end to end today.** The current code supports official-source
  discovery and manual imports, fit scoring, validated resume reuse, truthful
  tailoring, cover-letter and PDF preparation, authorized browser assistance,
  application history, and receipt reconciliation.
- **The browser dashboard is read-only.** It presents the same four stages—
  Discover, Decide, Prepare, and Verify—without mutating the database or
  executing commands.
- **Human review remains part of the product.** Unsupported material answers,
  CAPTCHA or MFA, assessments, identity or financial documents, account
  recovery, security changes, and uncertain submission outcomes stop for
  review.
- **The CapyPilot identity is unreleased.** The latest public release is v0.4.0
  under the former **ApplyPilot Local** name. The current repository contains
  the CapyPilot brand migration and keeps existing technical identifiers for
  compatibility.

## Product workflow

| Stage | What CapyPilot does | What it does not assume |
| --- | --- | --- |
| Discover | Collects official openings, optional board results, and manual leads with source state | A lead is not automatically a verified job |
| Decide | Checks eligibility, enriches descriptions, scores fit, and records readiness | A high score is not authorization to apply |
| Prepare | Routes validated resumes, identifies evidence gaps, tailors content, and validates PDFs | Missing facts are not invented |
| Verify | Separates authorization, browser observations, platform state, and durable receipts | A preview or final click is not proof of acceptance |

The safe application path is:

```text
prepare -> audit -> authorize -> submit -> observe -> reconcile receipt
```

If acceptance cannot be proved, the application remains
`submission_uncertain` and is not submitted again automatically.

## Install

Python 3.11 or 3.12 is recommended for the full workflow. Core commands and
the official-source radar also support Python 3.13.

There is currently no `applypilot-local` release on PyPI. Install from the
[latest GitHub release](https://github.com/raederhans/ApplyPilot/releases) or
directly from the repository:

```bash
pipx install "git+https://github.com/raederhans/ApplyPilot.git"
```

A release bundle or source checkout also provides a guided installer:

```bash
python install.py
```

Third-party job-board discovery is optional and currently intended for Python
3.11–3.12:

```bash
python install.py --with-jobboards
```

## Quick start

Initialize the local workspace and check available capabilities:

```bash
applypilot init
applypilot doctor
applypilot dashboard
```

Discover and prepare opportunities:

```bash
applypilot radar collect
applypilot radar report --hours 24
applypilot run discover enrich score tailor cover pdf
```

Review one exact job before any submission:

```bash
applypilot review-readiness
applypilot apply --dry-run --url <verified-job-url>
applypilot authorize-batch --url <verified-job-url>
applypilot apply --authorization-file <batch-manifest.json>
applypilot reconcile-receipts --file <receipt.json>
```

Validated resume variants can be inspected and routed independently:

```bash
applypilot resume-library-sync
applypilot resume-library-status
applypilot resume-route --url <verified-job-url>
```

Run `applypilot --help` for the complete command list. Optional browser
backends, interaction modes, provider behavior, and operating details belong
in the command help and product documentation rather than this overview.

## Compatibility and local data

The public product name is **CapyPilot**, while these technical identifiers
remain unchanged during the migration:

- distribution: `applypilot-local`
- Python package and CLI: `applypilot`
- environment variables: `APPLYPILOT_*`
- default workspace: `~/.applypilot/`
- database schema, storage keys, entry points, and repository URL

Do not commit or share the local workspace. It may contain profile data,
resumes, generated documents, SQLite databases, API keys, browser profiles,
screenshots, receipts, logs, or verification codes. CapyPilot does not endorse
CAPTCHA bypass, hidden submission, identity-document automation, or account
recovery automation. See [SECURITY.md](SECURITY.md) before reporting a
vulnerability.

## Development and documentation

```bash
python -m pip install -e ".[dev]"
ruff check src
pytest -q
python scripts/build_release.py
```

- [Product and frontend boundaries](docs/product-core.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [License and provenance](NOTICE.md)

CapyPilot is licensed under [GNU AGPL-3.0-only](LICENSE). It is an independent
continuation of [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot);
the original authors retain copyright in the upstream work. This repository is
not affiliated with applypilot.app, useapplypilot.com, or similarly named
products.
