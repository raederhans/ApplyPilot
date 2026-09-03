# CapyPilot brand system

CapyPilot is a reliable, relaxed, cautious and patient job-search copilot. It helps the user understand evidence and take the next safe step. It never promises an interview, offer or successful submission.

## 1. Naming

### User-visible product name

- Use **CapyPilot** in product headers, window titles, help copy, onboarding, screenshots and outward-facing documentation after each surface is migrated.
- Capitalization is fixed: `CapyPilot` with capital C and P.
- Chinese prose may write `CapyPilot 求职副驾` on first mention. Do not invent a separate Chinese brand name.
- The product can describe itself as a “求职副驾 / job-search copilot”, not an agent that guarantees outcomes.

### Technical compatibility names

The following remain unchanged unless a separate compatibility migration is approved:

- CLI command and examples: `applypilot`
- Local workspace/repository label: `applypilot-local`
- Python package/import namespace: `applypilot` and `src/applypilot`
- Environment variables: every `APPLYPILOT_*` identifier
- Existing database and persisted artifacts such as `applypilot.db`
- Existing paths, config keys, schema fields, browser-profile names, receipt contracts and automation identifiers containing `applypilot`

In mixed user/technical copy, use a truthful bridge such as: “CapyPilot 使用 `applypilot` 命令在本地运行。”

Never run a global search-and-replace from ApplyPilot to CapyPilot.

## 2. Brand principles

1. **Evidence before momentum.** Show what is known, missing or uncertain before suggesting action.
2. **Calm, not casual.** Warmth reduces pressure but never softens important warnings.
3. **Companion, not protagonist.** The capybara supports the task and stays visually secondary.
4. **Local and controllable.** Preserve the product’s local/privacy context and avoid cloud-like sync claims.
5. **No false finish line.** Opening a form, preparing materials, clicking submit and verifying a receipt are distinct states.

## 3. Logo and mark

### Approved forms

- Light horizontal lockup: [`assets/capypilot-lockup-light.png`](assets/capypilot-lockup-light.png)
- Compact mark: [`assets/capypilot-mark-compact-master.png`](assets/capypilot-mark-compact-master.png)
- App icon: [`assets/capypilot-app-icon-master.png`](assets/capypilot-app-icon-master.png)
- One-color light-surface lockup: [`assets/capypilot-lockup-monochrome.png`](assets/capypilot-lockup-monochrome.png)
- One-color dark-surface lockup: [`assets/capypilot-lockup-monochrome-inverse.png`](assets/capypilot-lockup-monochrome-inverse.png)
- Dark placement reference: [`assets/capypilot-lockup-dark.png`](assets/capypilot-lockup-dark.png)

### Usage rules

- Prefer the horizontal lockup where at least 32 px of mark height is available.
- Use the compact mark at 24–48 px in a sidebar or mobile header. Use the dedicated favicon exports below 24 px.
- Preserve clear space equal to at least one compact-mark ear diameter around a lockup.
- Do not recolor the full-color mark with semantic success, warning, error or link colors.
- On warm light surfaces use the full-color or deep-cocoa monochrome asset.
- On dark cocoa surfaces use the warm-ivory inverse monochrome asset. The solid-background dark PNG is a placement reference, not a flexible transparent runtime asset.
- Do not rotate, mirror, squash, crop through the ears/muzzle, add a speech bubble or place status badges over the face.
- Do not append `Local` to the display wordmark. “Local” may remain a separate environment/status label.

### Favicon and app icon

- Favicon: `favicon.ico`, `favicon-16.png`, `favicon-32.png`, `favicon-48.png`.
- Web-app icons: `app-icon-192.png` and `app-icon-512.png`.
- The compact mark was visually checked at 16, 24, 32 and 48 px on warm ivory and deep cocoa. See [`assets/qa-small-size-preview.png`](assets/qa-small-size-preview.png).

## 4. Color

The brand palette is warm and earthy. Semantic colors remain independently named and must not be substituted with brand colors.

| Token | Value | Role | Contrast evidence |
| --- | --- | --- | --- |
| `canvas` | `#F7F2E8` | Warm ivory page background | — |
| `surfaceRaised` | `#FFFCF6` | Raised content surface | — |
| `surfaceSand` | `#EEE4D3` | Grouping and selected-neutral surface | — |
| `textPrimary` | `#2B241F` | Primary cocoa text | 13.69:1 on canvas |
| `textMuted` | `#6F635A` | Secondary taupe text | 5.22:1 on canvas |
| `borderSubtle` | `#D8CEC0` | Dividers and non-text boundaries | not for text |
| `brandPrimary` | `#A95F3D` | Terracotta brand/action accent | 4.67:1 on raised surface |
| `brandSecondary` | `#5C3A2A` | Wordmark and strong brand text | 8.99:1 on canvas |
| `brandIllustrationTaupe` | `#B88962` | Mascot fill only | not for body text |
| `success` | `#2F7A55` | Confirmed success/eligible state | 5.08:1 on raised surface |
| `warning` | `#965F12` | Warning/caution state | 5.20:1 on raised surface |
| `error` | `#B4473E` | Error/blocked state | 5.24:1 on raised surface |
| `link` | `#2E68A0` | Links and informational navigation | 5.69:1 on raised surface |
| `focus` | `#2E68A0` | Focus outline | distinct from brand accent |

Recommended semantic tints are `#E6F1EA` success, `#F7ECD5` warning, `#F7E3E0` error and `#E6EEF7` information. A tint never replaces text, icon or explicit status wording.

## 5. Typography

- Product UI: `Source Sans 3`, then `Noto Sans SC`, `Microsoft YaHei`, `PingFang SC`, `system-ui`, `sans-serif`.
- Technical evidence and commands: `JetBrains Mono`, `Consolas`, `SFMono-Regular`, `monospace`.
- If Source Sans 3 or Noto Sans SC is not already bundled, use the system fallback in P1. Do not add a runtime font CDN request.
- Body text: 14–16 px; secondary evidence: no smaller than 12 px at desktop and 13 px on mobile.
- Use monospace only for commands, identifiers, scores and compact evidence labels. Do not set whole paragraphs in monospace.
- Use weight and spacing before all-caps. Chinese UI should not imitate Latin all-caps with excessive tracking.

## 6. Mascot

### Personality

The CapyPilot capybara is observant, grounded, patient and slightly dry rather than exuberant. It looks toward the task, not toward applause. The expression is closed-mouth and calm; the body is still rather than bouncing or cheering.

### Size and placement

- Desktop task surfaces: one mascot illustration at a time, normally 160–210 px wide and never more than 12% of the viewport.
- Mobile task surfaces: use the compact mark in navigation. A decorative illustration is optional and capped at 96 px in an empty/help state.
- Results lists, filters, authorization controls and receipt rows take visual priority.
- The mascot may be hidden entirely at constrained widths; the workflow must remain understandable without it.

### Allowed and forbidden use

Allowed: brand identification, onboarding, help, neutral empty state, missing-evidence guidance.

Forbidden: kawaii/chibi proportions, oversized eyes, costume, emoji treatment, speech bubbles, confetti, trophies, fireworks, dancing, thumbs-up, promises, celebrations of authorization/submission, or softening an error/uncertain receipt.

Use [`assets/capypilot-mascot-companion.png`](assets/capypilot-mascot-companion.png) as the approved desktop illustration reference. It is secondary and must not be enlarged into a hero.

## 7. Bilingual voice

### Voice rules

- Start with the current evidence or state, then the next safe action.
- Use short, direct sentences. Avoid marketing adjectives and motivational pressure.
- Separate preparation, opening a form, submission and verified receipt.
- Preserve job/company names, commands, identifiers and source evidence in their original language.
- Do not translate or soften legal, authorization, eligibility, assessment, CAPTCHA, MFA or sensitive-document boundaries.

### Good examples

| Chinese | English |
| --- | --- |
| 还缺 3 项岗位依据，补齐后再判断。 | 3 pieces of role evidence are still missing. Review them before deciding. |
| 已打开申请入口，尚未核验投递回执。 | Application page opened. Submission receipt not yet verified. |
| 需要你的授权后才能继续。 | Your authorization is required before continuing. |
| 已核验投递回执。 | Submission receipt verified. |
| 回执状态不确定，请先核对，暂勿重复投递。 | Receipt status is uncertain. Check it before attempting another submission. |

Avoid: “稳了”, “一键上岸”, “保证拿到面试”, “我们已经帮你投好” without a verified receipt, or cheerful copy around an error.

## 8. Status illustration matrix

| State | Mascot/illustration treatment | Color treatment | Copy requirement |
| --- | --- | --- | --- |
| Discovering / neutral | Compact mark only or no illustration | Brand terracotta may identify the active workflow | State source coverage and next scan action |
| Missing evidence | Small companion illustration allowed | Brand color for guidance; warning color only for actual caution | Say exactly what evidence is missing |
| Ready to prepare | Compact mark only | Success color may label eligibility, not guaranteed outcome | Distinguish ready-to-prepare from applied |
| Authorization required | No decorative mascot near the control | Warning or neutral control treatment | Name the exact authorization needed |
| CAPTCHA, MFA, assessment or sensitive document | No mascot | Warning/error as appropriate | State the manual stop plainly |
| Submission in progress | No celebration; static compact mark at most | Neutral/information | Never label as applied before receipt reconciliation |
| Receipt verified | Small compact mark permitted; no celebration scene | Success with text/icon, restrained | “Receipt verified” with exact evidence/time where available |
| `submission_uncertain` | No decorative mascot | Warning/error plus explicit label | Tell the user not to resubmit until reconciled |
| Error/blocked | No mascot masking the error | Error plus explicit recovery action | Explain cause if known and next safe action |
| Empty search/help | Companion illustration allowed up to documented size | Brand palette | Provide a useful recovery action, not blame |

## 9. Accessibility and responsive use

- Meet WCAG 2.2 AA: 4.5:1 for normal text and 3:1 for large text and essential UI boundaries.
- Do not convey status by color or mascot pose alone; pair color with text and, where useful, a standard icon.
- Focus indicators use `#2E68A0`, remain visible on ivory, sand and cream, and are at least 2 px thick with separation from the control edge.
- Interactive targets are at least 44 × 44 CSS px. Keep keyboard order aligned with visual order.
- Decorative mascot images use empty `alt`; informative use needs concise localized alt text. The wordmark image should expose the accessible name “CapyPilot”.
- Respect `prefers-reduced-motion`. Do not continuously animate the mascot. A single subtle entrance is optional only on help/empty states and must be disabled under reduced motion.
- At desktop widths, preserve the left workflow rail and evidence-first hierarchy from the selected reference.
- At tablet/mobile widths, collapse the rail into one accessible stage control without adding a second simultaneous workflow navigation. Keep the current stage text visible.
- Hide decorative mascot imagery before compressing evidence text or controls.
- Never horizontally scroll the primary job evidence list at common mobile widths.

## 10. Asset production boundary

All P0 raster assets and their inspection status are listed in [`assets/asset-manifest.json`](assets/asset-manifest.json). Assets that baked a checkerboard instead of true alpha were rejected and are not included.

P0 intentionally supplies no hand-authored SVG. A future vector master is a production task, not permission to redesign the mark.

