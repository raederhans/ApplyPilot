# Job Apply Pilot brand system

**Job Apply Pilot** is a local-first application operations workspace. The name is deliberately literal: it should be immediately discoverable as a job-application product and immediately understandable without a mascot or metaphor.

## Positioning

Job Apply Pilot runs the full evidence-backed loop from verified opportunity discovery to a durable application receipt. It is not a hosted one-click auto-apply service and does not count an opened form or a clicked button as a successful application.

The four-stage signature is always written in this order:

> **Discover → Decide → Prepare → Verify**

## Naming

- Public display name: **Job Apply Pilot**
- Short UI label where space is constrained: **JAP is forbidden**; use **Apply Pilot** only in compact UI copy when the full accessible name remains available.
- Technical identifiers remain unchanged: `applypilot`, `applypilot-local`, `APPLYPILOT_*`, `src/applypilot`, `applypilot.db`, schema keys and repository URLs.
- Historical upstream attribution remains **Pickle-Pixel/ApplyPilot**. Do not rewrite provenance.

## Visual language

The system uses an editorial-operations aesthetic: direct, high-contrast and precise.

| Token | Value | Role |
| --- | --- | --- |
| Signal ink | `#0B1F3A` | Primary brand field and display text |
| Flight blue | `#175CD3` | Links, focus and information |
| Route orange | `#FF5A36` | Decorative route geometry |
| Action orange | `#D83A12` | Accessible active state and primary emphasis |
| Receipt green | `#20B486` | Verified endpoint geometry only |
| Success green | `#0D7A60` | Accessible verified-state text |
| Canvas | `#F3F6FA` | Application background |
| Surface | `#FFFFFF` | Cards and panels |
| Muted text | `#526176` | Secondary evidence |
| Border | `#CBD5E1` | Structure and dividers |

The mark combines a job card, a four-stage route and a verified endpoint. Orange is for movement; green is reserved for evidence-backed completion. Never use the green endpoint to imply that an application was accepted without a receipt.

## Assets

- Runtime lockup: `src/applypilot/frontend/assets/job-apply-pilot/job-apply-pilot-lockup.svg`
- Runtime compact mark: `src/applypilot/frontend/assets/job-apply-pilot/job-apply-pilot-mark.svg`
- Runtime workflow signal: `src/applypilot/frontend/assets/job-apply-pilot/workflow-signal.svg`
- README hero: `docs/brand/job-apply-pilot/assets/readme-hero.svg`

The former CapyPilot assets remain only as historical provenance. New product surfaces must use the Job Apply Pilot assets.

Machine-readable values live in [`brand-tokens.json`](brand-tokens.json). Raster icon exports are rebuilt deterministically with `python tools/build_job_apply_pilot_assets.py`.
