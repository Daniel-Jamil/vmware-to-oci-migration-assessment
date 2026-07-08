# Full GUI Polish Design

## Goal

Polish the VMware to OCI Migration Assessment as one cohesive internal migration-specialist workbench. Keep the current four-stage workflow, saved assessment behavior, scenario modeling logic, and export semantics intact while improving clarity, visual hierarchy, alignment, wording, and responsive behavior across every visible stage.

## Product Posture

The tool is for internal migration specialists, not a customer-facing marketing experience. The interface should feel dense, calm, readable, and operational. The user should always understand where they are, what changed, what action saves local app state, what action creates a portable JSON file, and what action exports the Excel workbook.

## Scope

Polish the shared shell:

- Header identity, current assessment summary, saved/unsaved state, and assessment menu.
- Stage navigation labels, progress states, spacing, and mobile stage selector.
- Bottom stage actions and primary/secondary action hierarchy.
- Readiness notes and flash/status treatments.

Polish Stage 1 Setup and Inventory:

- Clarify local save/load actions versus portable JSON import/export.
- Improve saved assessment copy and grouping.
- Align upload, saved inventory, manual inventory, and pricing controls.
- Keep existing folder picker behavior and persistence unchanged.

Polish Stage 2 Inventory Review:

- Improve table density, header alignment, warning/readiness hierarchy, and action placement.
- Preserve existing validation, filtering, selected VM behavior, and continuation rules.

Polish Stage 3 Scenario Configuration:

- Unify Native, OCVS, and Hybrid tab styling with the shared shell.
- Make `Recalculate & Save`, `Save & Continue`, and Excel export hierarchy clearer.
- Normalize section headings, form label weights, card padding, status badges, cost summaries, and table controls.
- Preserve existing scenario calculations and saved state transactions.

Polish Stage 4 Results and Export:

- Present the final review as a clear sequence: workload profile, migration path comparison, internal specialist decision, save/export.
- Keep migration cards dense but improve vertical rhythm and label/value alignment.
- Keep rank badges, but reduce decorative emphasis and avoid automatic recommendation language.
- Make `Export Excel` the main final action.
- Make local assessment save and portable JSON import/export secondary utility actions with explicit labels.

## Visual System

- Use the existing Oracle/Redwood-inspired base: neutral canvas, white surfaces, Oracle red only as brand accent, green for primary action and ready/success states.
- Limit accent color use to meaning-bearing areas: Native, OCVS, Hybrid identity and status states.
- Avoid new gradients, large decorative panels, or marketing-style hero sections.
- Keep border radius at 4-8px and maintain compact operational spacing.
- Keep font family unchanged. Standardize heading, label, helper text, and button scales where local sections diverge.

## Copy Rules

- Use `migration specialist` language instead of `assessor` where the user-facing text describes the operator.
- Use `Save assessment` for local app-state snapshot behavior.
- Use `Export assessment JSON` and `Import assessment JSON` for portable assessment files.
- Use `Export Excel` for the workbook.
- Avoid ambiguous labels such as `Export Draft` unless the action genuinely exports a draft-labeled workbook and existing readiness rules require that label.
- Avoid automatic-choice language such as `winner`, `best`, or `recommended` in modeled price comparisons.

## Interaction Rules

- Each area should have one visually dominant primary action.
- Secondary utility actions should be grouped separately or visually quieter.
- Save actions should return the user to the same working stage when used from that stage.
- Native browser file pickers for assessment JSON import/export should keep using the shared picker identity and default start folder.
- Keyboard focus states must remain visible and use the shared workspace focus color.

## Responsive Rules

- No text should overflow buttons, cards, table cells, or the mobile stage selector.
- Fixed-format UI elements such as tabs, action bars, rank badges, KPI cards, and table controls should use stable dimensions or responsive grid constraints.
- Mobile layouts should stack action groups while preserving primary action order.

## Verification

Update or add focused regression checks for:

- Shared navigation and action labels.
- Save/load/import/export language and picker hooks.
- Results card rank display without automatic recommendation copy.
- Final export hierarchy and accessible form controls.
- Responsive CSS contracts for stacked layouts and no gradient-based styling.

Run the focused unit tests that cover readiness, assessment portability, and rendered markup, then run the full regression script.

## Non-Goals

- Do not change pricing formulas, readiness rules, scenario placement logic, workbook sheet structure, or saved assessment JSON schema.
- Do not remove the four-stage workflow.
- Do not introduce a new frontend framework or icon library in this pass.
- Do not redesign hidden legacy Step 4 price-comparison markup unless it is still user-visible through the active route.
