# Guided Assessment Workspace Redesign

## Goal

Redesign the VMware to OCI Migration Assessment as one coherent, Oracle Redwood-aligned, four-stage workspace. The workflow must lead an assessor from setup through a defensible customer-facing result without hiding incomplete assumptions, unsupported-workload remediation requirements, or missing licensing inputs.

The redesign preserves the existing OCI Native, OCVS, Hybrid, pricing, saved-assessment, and Excel calculation behavior unless this specification explicitly introduces a readiness or presentation rule.

## Scope

The redesign covers:

- A shared application shell and stage navigation.
- Setup, pricing selection, inventory upload, and manual sizing.
- Inventory quality review, warning review, workload scope, and placement recommendations.
- OCI Native, OCVS, and Hybrid configuration.
- Readiness, comparison, assessor recommendation, and export.
- Saved assessments and the separately approved portable JSON import/export workflow.
- Responsive behavior and accessibility.
- Template and frontend organization needed to support a consistent experience.

Word proposal generation remains paused and is outside this redesign.

## Audit Findings Addressed

The approved redesign directly addresses these observed issues:

- Native received a first-place cost rank without visibly carrying its unsupported-workload remediation warning into the comparison.
- OCVS and Hybrid were ranked while the VCF price was zero, even though the backend generated a missing-price warning.
- The setup left rail expanded beyond its fixed width when an inventory filename was long.
- Setup and Workload Scope produced page-level horizontal overflow at a 390px viewport.
- Native and comparison tables were clipped inside `overflow: hidden` containers.
- Workload selection and sorting depended on mouse-only row and header clicks.
- The Native editor exposed dozens of controls without programmatic labels.
- Progress changed from "Step 1 of 4" to "Step 2 of 3," and the branded shell disappeared after Setup.
- Scenario tabs had insufficient contrast because shared button styling overrode scenario-specific styling.
- Setup displayed internal paths, API details, and a visible explanation of the color system.

## Information Architecture

The application has four stages:

1. Setup & Inventory
2. Inventory Review
3. Scenario Configuration
4. Results & Export

Each stage has one computed state:

- `not_started`: required source data has not been provided.
- `needs_attention`: work can continue, but warnings or unsaved decisions remain.
- `ready`: stage inputs are valid and the user can continue.
- `complete`: stage decisions have been saved and satisfy its completion rules.

Stage state is calculated from current assessment data rather than persisted as an independent source of truth.

## Shared Application Shell

Every stage uses the same shell.

### Header

The compact header contains:

- Oracle identity and product name.
- Active assessment name and customer/project name.
- Saved or unsaved status.
- A concise global assessment menu for save, open, import, and export actions.

The header does not contain long guidance text or technical source paths.

### Stage Navigation

Desktop uses a restrained left navigation rail showing all four stages, their state, and the current location. The rail has a stable width, and every child uses `min-width: 0` plus safe wrapping or truncation.

Mobile replaces the rail with a compact stage selector and `Step N of 4` status. It does not render the full desktop navigation above the page content.

Users may revisit earlier stages. A later stage is not presented as customer-ready when prerequisite data is incomplete. Expert users may open later stages for review, but readiness messages remain visible and scenario recommendation/ranking rules still apply.

### Main Workspace

The main workspace receives the available width. A contextual readiness panel appears only when blockers, warnings, or required actions exist. The permanent three-column layout is removed because it made operational forms and tables too narrow.

Desktop Previous and Continue controls appear at the bottom of the main workflow. Mobile uses a sticky bottom action bar that does not cover content.

## Stage 1: Setup & Inventory

Stage 1 uses three compact sections.

### Assessment Identity

Fields:

- Assessment name.
- Customer or project name.
- Notes.

Assessment and customer names are treated as separate concepts. Assessment name identifies the saved work item; customer/project name is used in customer-facing outputs.

### OCI Pricing

The primary view shows:

- Active currency.
- Price-list status and timestamp when available.
- Refresh or download latest price list.
- Secondary selection of an existing local price list.

API paths, local filenames, and complete source paths move into a collapsed `Source Details` disclosure.

### Inventory Source

A segmented control selects exactly one mode:

- Upload Inventory
- Manual Summary

Upload accepts the existing supported spreadsheet and CSV formats.

Manual Summary contains:

- VM count.
- Total vCPU.
- Total RAM GB.
- Total storage GB.
- OCI-supported VM count.
- Unsupported or legacy VM count.

After creation, the same fields remain editable. `Update Summary` replaces the generated manual inventory while preserving compatible assessment settings whenever possible.

### Stage 1 Completion

Stage 1 is `ready` when:

- Assessment name is present.
- Customer/project name is present.
- A usable OCI price list is selected.
- A valid inventory source is loaded.

Invalid uploads or manual totals remain in Stage 1 with field-level errors and do not clear a previously valid inventory until a replacement succeeds.

## Stage 2: Inventory Review

Stage 2 combines inventory quality, warnings, workload scope, and initial placement in one workspace.

### Summary

The summary shows VM count, vCPU, RAM, storage, powered-on count, Native-supported count, and review count. It does not repeat full source paths.

### Warning Inbox

Warning categories appear above the inventory list. Examples include:

- Unsupported for OCI Native.
- Missing or invalid storage.
- Missing CPU or memory values.
- Duplicate VM names.
- Unmapped operating system.

Selecting a warning filters the inventory list to affected VMs. Every warning includes detected value, reason, recommended treatment, and affected count.

Critical data errors must be corrected before Stage 2 becomes ready. Advisory warnings may be acknowledged. Acknowledgment is persisted in assessment state and included in saved and portable assessment formats.

### Inventory List

The two-table transfer layout is replaced by one list containing:

- Explicit inclusion checkbox.
- VM name.
- Power state.
- Operating system.
- Native support status.
- vCPU.
- RAM.
- Storage.
- Suggested placement.
- Warning state.
- Details action.

Toolbar controls include search, support status, power state, placement, Select All, inclusion commands, and bulk placement commands.

Rows are keyboard reachable. Selection uses native checkbox behavior. Sort controls are buttons with `aria-sort`. Bulk changes show a reversible confirmation with Undo.

Unsupported VMs remain in scope and default to OCVS placement. The workflow does not describe them as removable "images."

Desktop uses a responsive table with intentional scroll containment and sticky identifier columns. Mobile uses a compact VM list with a details drawer and bulk selection mode.

### Stage 2 Completion

Stage 2 is `ready` when:

- At least one VM is included.
- Critical data errors are resolved.
- Remaining advisory warnings are acknowledged.
- Every included VM has an initial Native, OCVS, or Review placement state.

## Stage 3: Scenario Configuration

Stage 3 contains three secondary tabs:

- OCI Native
- OCVS
- Hybrid

Price Comparison is removed from this stage.

All tabs meet the WAI-ARIA tab pattern, use roving focus and arrow-key navigation, and meet WCAG AA contrast. Shared button styling must not override tab state colors.

### Shared Scenario Structure

Each scenario begins with:

- `ready`, `needs_attention`, or `incomplete` status.
- Blocking assumptions and affected VM count.
- Monthly and annual modeled cost.
- Workload scope and capacity outcome.
- Sticky `Recalculate & Save` action.
- Visible unsaved-change state.

Assumptions and outcomes are separated. Cost and capacity summaries appear once at the top; repeated representations of the same value are removed unless they add a distinct comparison.

### OCI Native

Unsupported Native workloads are labeled `Requires remediation`. They remain included in the modeled full-Native scenario, and Native remains technically eligible and fully comparable when its pricing inputs are complete. The scenario status is `needs_attention`, not `incomplete`, while unsupported VMs are present.

Before a Native recommendation becomes customer-ready, the assessor must review the affected VMs and record the intended treatment in the recommendation rationale, such as remediation before migration, exclusion from a Native migration wave, or a documented exception. Acknowledging the warning does not remove the remediation indicator from results or exports.

The per-VM editor provides:

- Search and filters.
- Explicitly labeled controls.
- Bulk shape, burst, VPU, and licensing defaults.
- Sticky VM name and OS identifiers.
- Pagination for large inventories, initially 50 rows per page.
- Pending-change summary before recalculation.
- Compact mobile details editor.

All selected VMs remain included in server-side totals and exports regardless of the visible editor page.

### OCVS

OCVS inputs are grouped by purpose:

- Profile and commitment term.
- CPU, memory, storage, and vSAN policy.
- Resilience and spare nodes.
- VCF licensing.

The selected shape's 1-year or 3-year discount is shown with the active term. The discount remains shape-specific and is calculated by the existing discount configuration.

When OCVS physical cores are present and VCF price per core/year is zero, pricing status is `incomplete`. OCVS cost may be displayed as a partial infrastructure amount, but it cannot be normally ranked or marked customer-ready.

### Hybrid

Hybrid begins with the placements saved in Stage 2. Users may search, filter, and override placement individually or in bulk.

OCVS assumptions are shared with the OCVS scenario and clearly labeled as shared. The UI does not present duplicated controls as independent values. Hybrid shows Native and OCVS workload counts, OCVS subset sizing, and manual override count.

Hybrid inherits the VCF pricing blocker when its OCVS subset has physical cores and no VCF unit price.

### Stage 3 Completion

Stage 3 is `complete` when:

- All pending changes are saved.
- At least one scenario is technically eligible and fully priced.

Incomplete scenarios remain visible for review and may proceed to Results, but they cannot receive a normal rank or customer-ready recommendation.

## Stage 4: Results & Export

### Assessment Readiness

Stage 4 opens with one status:

- `customer_ready`
- `draft_review_required`
- `incomplete`

The readiness panel lists unresolved blockers such as unreviewed Native remediation requirements, missing VM data, missing OCI pricing, missing VCF pricing, or unsaved scenario changes.

### Scenario Comparison

The comparison separates:

- Technical eligibility.
- Pricing completeness.
- Modeled cost.

Each scenario shows:

- Status.
- Monthly, annual, and 3-year modeled cost.
- Cost per VM.
- Placement split.
- Key sizing assumptions.
- Benefits and trade-offs.
- Remediation or missing-input requirements.

Medals are removed. The interface may identify the `Lowest complete modeled price`, but incomplete scenarios are excluded from that calculation and price ordering is not presented as the recommendation.

Desktop may use comparison cards plus a responsive detail table. Mobile stacks scenario summaries and opens details in focused views instead of clipping a wide table.

### Assessor Recommendation

The assessor selects:

- OCI Native.
- OCVS.
- Hybrid.
- No recommendation yet.

The assessor may add a short rationale. A scenario may be selected for internal draft review while incomplete, but Stage 4 cannot become `customer_ready` unless the selected recommendation is technically eligible, fully priced, and based on saved assumptions. A Native recommendation with unsupported VMs also requires reviewed remediation treatment in the rationale; it does not require Native to be marked ineligible.

Recommendation and rationale are persisted in app state, local saved assessments, portable JSON, and relevant Excel summary output.

### Export Center

Actions:

- Save current assessment.
- Export Excel.
- Export portable JSON.
- Import assessment JSON.
- Load a previous assessment.

When blockers remain, the Excel action is labeled `Export Draft`. The workbook includes readiness state and unresolved warnings. When customer-ready, it is labeled `Export Excel`.

Portable JSON export remains available for incomplete work so teams can share and continue an assessment. Portable package behavior follows `2026-07-03-portable-assessment-json-design.md`.

## Readiness Model

One backend readiness builder returns a structured result for the shell, stages, Results, saved assessments, and exports.

The result contains:

- Overall readiness state.
- Per-stage state.
- Per-scenario eligibility and pricing state.
- Per-scenario remediation-required state and affected VM count.
- Blocking items.
- Advisory items.
- Affected VM identifiers when relevant.
- Whether customer-ready export is allowed.

Readiness is derived from existing inventory, pricing, placement, and scenario analysis. The UI must not duplicate these rules in JavaScript.

The existing `fit_warnings` calculation is incorporated into this result and rendered in the workspace. Warnings generated by the backend must not be silently omitted from the UI.

## Persisted State Additions

App state adds:

- Acknowledged advisory warning identifiers.
- Assessor recommendation.
- Assessor recommendation rationale.

These fields receive safe defaults when older saved assessments are loaded. Computed readiness and stage state are not persisted.

## Component and Template Boundaries

The current large templates are split into focused, server-rendered components:

- Shared assessment shell.
- Header and global assessment menu.
- Stage navigation.
- Readiness panel.
- Source details.
- Warning inbox.
- Inventory toolbar and list.
- Native editor.
- OCVS editor.
- Hybrid editor.
- Results comparison.
- Export center.

Shared Redwood tokens and layout rules move to static CSS. Page interactions such as tabs, dirty-state tracking, table selection, filters, drawers, and toasts move to focused static JavaScript files. Server-side validation and calculation remain authoritative.

Existing route endpoints remain valid during the redesign:

- `/` renders Stage 1.
- `/step3` renders Stage 2.
- `/step4` with Native, OCVS, or Hybrid selection renders Stage 3.
- `/step4` with Price selection renders Stage 4.
- Existing scenario aliases redirect to the corresponding stage.

The visible UI consistently uses the four-stage names even if compatibility routes retain their current paths.

## Error Handling

- Field errors appear beside the affected input and are summarized in the readiness panel.
- A failed inventory replacement preserves the currently valid inventory.
- A failed scenario save preserves the last saved result and keeps pending values visible where possible.
- An import failure does not change the active assessment or saved library.
- Export failures return the user to the same stage with a specific message.
- Focus moves to the first error summary after submission.
- Success and failure messages use accessible live regions and remain visible long enough to read.

## Oracle Redwood Visual Direction

- Oracle red is a restrained brand signal, not the dominant action color.
- Green and teal identify progress, readiness, and primary commands.
- Amber identifies review states.
- Red is reserved for destructive actions and critical failures.
- Scenario identity may use restrained blue, teal, and green accents, but all text and controls meet contrast requirements.
- No visible panel explains the color system to the user.
- Operational screens remain dense, aligned, and scan-friendly. They avoid marketing-style composition, nested cards, decorative gradients, and oversized headings.
- Familiar icons are used for compact commands where available, with accessible names and tooltips for unfamiliar controls.

## Accessibility Requirements

- WCAG AA contrast for text, controls, focus, and status indicators.
- Logical heading hierarchy.
- Complete keyboard access to navigation, tabs, tables, selection, dialogs, and drawers.
- Explicit labels or accessible names for every form control.
- Native checkboxes for VM scope selection.
- `aria-sort` on sortable columns.
- Roving tab focus with arrow-key behavior.
- Visible focus indicators.
- Status changes announced through live regions.
- Color is never the only status signal.
- Touch targets remain usable at mobile widths.

## Responsive Requirements

The supported validation widths are 390px, 768px, 1280px, and 1440px.

At every width:

- No page-level horizontal overflow.
- Long customer names and filenames wrap or truncate safely.
- No content is hidden by sticky navigation or actions.
- Tables use explicit scroll containers, sticky identifiers, or mobile detail views.
- Button text fits without clipping or arbitrary viewport-scaled font sizes.
- The primary action remains reachable.
- Scenario and result content does not rely on fixed desktop widths.

## Performance Requirements

- Large inventories remain fully included in calculations and exports.
- Inventory and Native editor pages display bounded row sets with pagination.
- Filters and bulk actions operate on an explicit scope and communicate whether they affect the current page, filtered set, or complete inventory.
- Repeated DOM copies of all VM controls are avoided.

## Compatibility

- Existing local saved assessments continue to load with defaults for new fields.
- Existing app-state files continue to normalize through the current default-state mechanism.
- Existing OCVS discount behavior and manual sizing behavior remain unchanged.
- Existing workbook calculation invariants remain unchanged except for added readiness and recommendation metadata.
- Portable JSON schema includes the new persisted recommendation and acknowledgment fields while remaining versioned.
- Existing URLs and scenario aliases remain valid.

## Testing and Acceptance

Automated regression coverage includes:

- Existing pricing, discount, sizing, saved-assessment, and workbook invariants.
- Four-stage navigation and status transitions.
- Stage 1 replacement failure preserving the previous inventory.
- Stage 2 checkbox selection, filters, bulk actions, warning filtering, acknowledgment, and Undo behavior.
- Native remaining eligible and rankable while clearly showing unsupported-workload remediation status.
- OCVS and Hybrid VCF pricing blockers.
- Exclusion of incomplete scenarios from lowest-complete-price ranking.
- Assessor recommendation and rationale persistence.
- Draft and customer-ready Excel behavior.
- Saved assessment and portable JSON round trips with new fields.
- Backward loading of older saved snapshots.

Browser verification includes:

- Desktop, tablet, and mobile screenshots for all four stages.
- No page-level overflow at 390px, 768px, 1280px, and 1440px.
- Keyboard-only completion of core workflows.
- Tab and sorting keyboard behavior.
- Accessible names for interactive controls.
- Contrast checks for normal, hover, active, disabled, warning, error, and focus states.
- Long assessment names, customer names, inventory filenames, and warning text.
- Large inventory pagination and responsive editing.

## Out of Scope

- Word proposal generation.
- Cloud-hosted assessment storage.
- Multi-user concurrent editing.
- Authentication and role management.
- Changes to Oracle pricing source semantics.
- A new frontend framework or SPA rewrite.
