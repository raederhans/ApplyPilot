# CapyPilot P1 Dashboard and perimeter handoff

This handoff translates the selected P0 brand direction into a bounded P1 migration. P0 did not edit runtime product files.

## Source of truth

- Direction reference: `docs/brand/capypilot/assets/selected-dashboard-1440x1024.png`
- Brand rules: `docs/brand/capypilot/BRAND.md`
- Machine-readable tokens: `docs/brand/capypilot/brand-tokens.json`
- Asset inventory and QA state: `docs/brand/capypilot/assets/asset-manifest.json`

Do not infer product behavior from the generated screenshot. Existing application, receipt, resume-routing, privacy and local-data contracts remain authoritative.

## P1 scope

1. Apply the warm-light surface and text tokens to the existing Dashboard.
2. Change user-visible product display copy from ApplyPilot to CapyPilot where the copy denotes the product brand.
3. Replace the current `AP` brand tile with the approved compact mark or horizontal lockup at the documented size.
4. Keep the existing four-stage left workflow, one active stage, evidence list, metrics, search, filters and source-quality region.
5. Add the desktop companion illustration only where it fits without displacing evidence; hide it first at narrower widths.
6. Update favicon/app-icon references using the approved exports.
7. Extend the same display name and assets to peripheral product surfaces only after each surface is checked for technical identifiers and truthful state copy.

## Non-negotiable compatibility boundary

Do not rename or globally replace:

- the `applypilot` CLI, command examples or entry points;
- `applypilot-local` paths or workspace labels;
- the `src/applypilot` Python package/import namespace;
- any `APPLYPILOT_*` environment variable;
- `applypilot.db`, existing storage paths, config keys or schema fields;
- browser profile names, automation identifiers, receipt fields, status enums or persisted values that contain `applypilot`;
- `submission_uncertain`, authorization, CAPTCHA, MFA, assessment, identity/financial-document or receipt-reconciliation gates.

Use `CapyPilot` only as the display name. In technical help, bridge the two names explicitly instead of hiding the CLI identifier.

## Suggested token mapping

| Current role | P1 token |
| --- | --- |
| page/background ink | `canvas` `#F7F2E8` |
| raised panel | `surfaceRaised` `#FFFCF6` |
| selected/grouped neutral | `surfaceSand` `#EEE4D3` |
| primary text | `textPrimary` `#2B241F` |
| muted/quiet text | `textMuted` `#6F635A` |
| dividers | `borderSubtle` `#D8CEC0` |
| product accent | `brandPrimary` `#A95F3D` |
| strong brand text | `brandSecondary` `#5C3A2A` |
| eligible/confirmed success | `success` `#2F7A55` |
| caution/manual stop | `warning` `#965F12` |
| blocked/error | `error` `#B4473E` |
| link/information/focus | `link` / `focus` `#2E68A0` |

Do not map `brandPrimary` to success. The selected “判断” stage can use terracotta; a confirmed eligibility/receipt state uses success green with explicit text.

## Exact asset references

| P1 surface | Asset |
| --- | --- |
| Left rail, desktop header | `assets/capypilot-lockup-light.png` or compact mark plus accessible text |
| Collapsed rail/mobile header | `assets/capypilot-mark-compact-master.png` |
| Browser favicon | `assets/favicon.ico` with PNG fallbacks |
| Web app icon | `assets/app-icon-192.png`, `assets/app-icon-512.png` |
| Warm-light monochrome | `assets/capypilot-lockup-monochrome.png` |
| Dark-surface monochrome | `assets/capypilot-lockup-monochrome-inverse.png` |
| Desktop help/empty/header support | `assets/capypilot-mascot-companion.png` |

Resolve those paths from `docs/brand/capypilot/` during implementation. If product assets need to live under a runtime static directory, copy the approved files there in P1 and retain this directory as the brand source and provenance record.

## Layout requirements

- Do not introduce a top workflow stepper while the left workflow rail is visible.
- Keep one next-safe-action region. Do not convert every metric, filter or list row into a card.
- Cap the desktop mascot at 210 px and 12% of the viewport; it cannot overlap controls or evidence.
- At narrower widths, hide the decorative mascot before reducing body text below 13 px or targets below 44 px.
- Mobile may collapse the left rail, but must preserve the stage order and active-stage label.
- Verification, authorization, receipt and error regions must remain visually sober and text-led.

## Copy requirements

- Replace brand display copy only after checking the surrounding sentence.
- Keep commands such as `applypilot run enrich score` exactly unchanged.
- Keep public job/company names and source identifiers in their original language.
- Use the bilingual examples and status matrix in `BRAND.md`.
- Never call a preview, form opening or submit click a completed application without verified receipt evidence.

## Accessibility acceptance

- Normal text contrast is at least 4.5:1; large text and essential component boundaries are at least 3:1.
- Focus outlines are visible on every warm surface and keyboard order matches visual order.
- Status meaning remains available without color and without the mascot.
- Wordmark/mark has an accessible name; decorative mascot images have empty alt text.
- 44 × 44 px minimum interactive targets.
- Reduced-motion users receive no continuous mascot animation.
- Desktop and mobile views have no unintended horizontal overflow.

## P1 completion gate

P1 is complete only when:

1. The actual Dashboard visually matches the selected warm-light direction without losing existing workflow/data behavior.
2. Display naming is CapyPilot while technical identifiers remain unchanged.
3. Brand and semantic colors are separate in code and visible states.
4. Desktop, tablet and mobile layouts preserve one workflow navigation and evidence priority.
5. Favicon, app icons, lockup and mascot assets render from product-owned runtime paths.
6. Focus, contrast, target size, alt text and reduced-motion checks pass.
7. Authorization, receipt, error and `submission_uncertain` states remain truthful and restrained.

Any future vector redraw or broader README/CLI/package migration is out of P1 unless separately approved.

