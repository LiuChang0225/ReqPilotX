# ReqPilotX Design System — Master

This artifact records the UI/UX Pro Max input after product-specific correction. `PRODUCT.md` and `DESIGN.md` are authoritative; runtime tokens are in `prototype/app/static/design-tokens.css`.

## Dials

- Variance 3/10: familiar engineering software, not a marketing layout.
- Motion 2/10: restrained state transitions only.
- Density 8/10: data-first desktop workbench.

## Master pattern

Evidence Review Workbench: virtualized Requirement queue/table + source/evidence + selected-item inspector. Review, AI, and Compliance Matrix share one `selectedRequirementId`, scoped to the selected project and SOR.

## Required rules

- Use the official Schaeffler SVG as the company brand, ReqPilotX as the product name, Schaeffler green as the semantic accent, and system fonts.
- Use a persistent `220px / 64px` sidebar. Default expanded at 1440px and above, compact from 1121–1439px, and use an accessible overlay drawer from 1024–1120px.
- Keep one primary action per stage.
- Keep tables and source content in the main work surface; move editing to one inspector.
- Keep Review workbench top geometry at 100px and its queue toolbar at no more than 96px.
- Keep `HEADING` out of decision counts, bulk updates, and export rows.
- Treat stale AI/evidence/decision inheritance as a reference that requires confirmation.
- Support keyboard navigation, visible focus, announced errors, and WCAG AA contrast.
- Validate at 1920px, 1440px, 1280px, and 1024px in Chinese and English; mobile is intentionally out of scope.
- Use semantic values from `design-tokens.css`.

## Forbidden rules

- No blue/purple gradient, gradient text, glass effect, or scroll-reveal dependency.
- No comparison-page/landing-page pattern, KPI-card decoration, card nesting, or large empty hero area.
- No full dataset DOM, row-level forms, or row-level image expansion.
- No new font, animation, icon, or component library solely for this page.
