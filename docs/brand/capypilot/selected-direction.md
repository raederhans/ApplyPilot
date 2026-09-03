# CapyPilot P0 selected direction

Status: selected and revised on 2026-09-02.

## Decision

The selected direction is **Quiet Copilot / 安静陪驾**, based on the first of the three visual explorations and revised after user feedback.

The revision is the approved P0 direction:

- Keep the existing left-side four-stage workflow: **发现 → 判断 → 准备 → 核验**.
- Keep the evidence list, score filters, metrics, source-quality panel and one next-safe-action region.
- Replace the near-black workbench with a clearly lighter, warmer and cleaner system: warm ivory canvas, light sand grouping surfaces and cream raised surfaces.
- Replace the photographic capybara with a professional editorial illustration. It is a small-to-medium secondary visual, not the focal task.
- Use terracotta and wood brown as brand colors. Keep success, warning, error and link colors semantically separate.
- Keep verification, authorization, receipt and uncertain states restrained. No celebration, guarantee or implied submission.

## Approved visual reference

Use [`assets/selected-dashboard-1440x1024.png`](assets/selected-dashboard-1440x1024.png) as the visual target for P1. It is a direction reference, not production UI code and not a source of truth for data.

The visual target preserves the current information architecture:

1. CapyPilot lockup and local/privacy context in the left rail.
2. Four numbered workflow stages with one active stage.
3. Page heading and one next-safe-action region.
4. Four evidence metrics.
5. Search, sort and score filters.
6. Continuous evidence-backed opportunity list.
7. Match distribution and source quality in the right rail.

## Review evidence

- Visual inspection confirmed a light warm workspace, non-photographic illustration, one workflow navigation and preserved evidence hierarchy.
- The generated reference was exported to an exact 1440 × 1024 PNG.
- Mean luminance changed from 23.3 in the original selected dark concept to 242.0 in the revised concept.
- Pixels below luminance 64 changed from 94.12% to 0.49%; pixels above luminance 192 are 94.97%.
- The capybara remains confined to the upper-right support area and does not cover controls, metrics or evidence.

## Deliberate limits

- P0 did not edit `dashboard.html`, README files, `pyproject.toml`, CLI code or tests.
- P0 did not rename Python packages, commands, paths, environment variables or data contracts.
- The raster assets are approved implementation references and runtime-ready PNG/ICO exports. No SVG master is supplied in P0.
- If a vector master is later commissioned, redraw from the approved compact mark without changing pose, proportions, expression or palette; inspect it at 16, 24, 32 and 48 px before replacement.

