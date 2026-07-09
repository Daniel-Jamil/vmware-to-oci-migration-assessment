# VMware to OCI Migration Assessment User Guide

This guide explains how to use the VMware to OCI Migration Assessment tool, what it can model, and how to interpret the results.

The application is a local migration-assessment workbench for comparing three target paths:

- OCI Native
- Oracle Cloud VMware Solution, abbreviated as OCVS
- Hybrid, combining OCI Native and OCVS placement

It is designed for migration specialists and pre-sales or assessment teams. It provides structured inventory review, scenario sizing, modeled cost comparison, readiness notes, and Excel workbook export. It is not a binding quote, not an OCI Cost Estimator replacement, and not a complete TCO or business-case model.

## Contents

- [Core Capabilities](#core-capabilities)
- [What The Tool Does Not Do](#what-the-tool-does-not-do)
- [Local Data And Security](#local-data-and-security)
- [Starting The App](#starting-the-app)
- [Assessment Workflow](#assessment-workflow)
- [Step 1: Setup And Inventory](#step-1-setup-and-inventory)
- [Step 2: Inventory Review](#step-2-inventory-review)
- [Step 3: Scenario Configuration](#step-3-scenario-configuration)
- [Step 4: Results And Export](#step-4-results-and-export)
- [Modeling Logic](#modeling-logic)
- [Readiness Logic](#readiness-logic)
- [Outputs](#outputs)
- [Common Usage Patterns](#common-usage-patterns)
- [Troubleshooting](#troubleshooting)
- [Recommended Operating Practices](#recommended-operating-practices)

## Core Capabilities

The tool helps you:

- Import VMware inventory from RVTools `.xlsx`, `.xlsm`, or `.csv` exports.
- Build a manual workload summary when detailed inventory is unavailable.
- Select the VMs in assessment scope.
- Review inventory issues such as missing sizing values, unknown OS values, duplicate VM names, and Native compatibility findings.
- Select or download an OCI price list.
- Model OCI Native compute, memory, Block Volume capacity, Block Volume performance units, IaaS discount, burst, and Windows license assumptions.
- Model OCVS node count, host shape, term, standard Block Volume storage, dense vSAN usable capacity, headroom, extra spare nodes, and optional VCF licensing.
- Model Hybrid placement, with each VM routed to OCI Native, OCVS, or Review.
- Maintain separate Hybrid OCVS assumptions while inheriting the OCVS scenario assumptions by default.
- Compare monthly, annual, and 3-year modeled run-rate across Native, OCVS, and Hybrid.
- Save local assessments.
- Export and import portable assessment JSON packages.
- Export a multi-sheet Excel workbook for review.

## What The Tool Does Not Do

The tool does not:

- Produce a binding commercial quote.
- Replace official Oracle pricing validation.
- Replace detailed architecture design, network design, security design, backup design, or DR planning.
- Discover live VMware environments by itself.
- Validate application dependencies or migration waves automatically.
- Confirm VMware, Broadcom, Microsoft, or third-party license compliance.
- Host assessments for multiple users. It is a local-only Flask app.

Before external sign-off, validate sizing, licensing, pricing, and architecture with the appropriate official tools and commercial process.

## Local Data And Security

The app runs locally on `127.0.0.1`.

Local working data is stored under:

```text
downloads/
downloads/app_state/
downloads/exports/
downloads/imported_assessments/
rvtools/
```

These folders can contain customer names, VM names, operating systems, resource values, price-list data, assessment notes, and recommendation rationale. Treat them as customer-sensitive.

Portable assessment JSON files can contain enough data to recreate an assessment on another machine. Share them only through approved channels.

## Starting The App

From the project folder:

```bash
python app.py
```

By default, open:

```text
http://127.0.0.1:5000/
```

To choose a port:

```bash
MIGRATION_ASSESSMENT_PORT=5059 python app.py
```

Then open:

```text
http://127.0.0.1:5059/
```

Stop the app with `Ctrl + C` in the terminal running Flask.

## Assessment Workflow

The tool has four stages:

1. **Setup & Inventory**
   Select the price list, assessment identity, and inventory source.

2. **Inventory Review**
   Review warnings, choose in-scope VMs, and set the initial placement basis.

3. **Scenario Configuration**
   Tune Native, OCVS, and Hybrid assumptions.

4. **Results & Export**
   Compare paths, record a specialist recommendation, save the assessment, and export the workbook.

Use `Save & Continue` at the bottom of form-driven stages to persist changes before moving forward.

## Step 1: Setup And Inventory

Use Setup to define the assessment identity and load the source data.

### Assessment Identity

Enter:

- Customer or project name
- Assessment name
- Optional notes

The customer/project name is used in saved assessment metadata and export filenames.

### OCI Price List

You can:

- Select a downloaded local price list.
- Download a price list from Oracle for a supported currency.
- Import a portable assessment JSON that includes a price list.

The app uses the price list to find compute, memory, Block Volume, VPU, Windows OS, and OCVS host pricing signals.

If no price list is selected, sizing can still be reviewed, but cost outputs may be zero or incomplete.

### Inventory Input

Supported inventory formats:

- `.xlsx`
- `.xlsm`
- `.csv`

The tool is optimized for RVTools exports, especially `vInfo` and supporting RVTools detail sheets. It can also consume a simpler table when it has the required fields:

- VM name
- vCPU
- RAM
- Storage
- Operating system

If the detailed VM inventory is not available, use the manual workload summary. Manual mode creates a synthetic sizing inventory from total VM count, total vCPU, RAM, storage, and supported versus unsupported or legacy VM counts.

### Saved Assessments

Setup also provides local assessment save/load behavior.

- **Save assessment** stores the current assessment on this machine.
- **Open/load assessment** restores a saved local assessment.
- **Export assessment JSON** creates a portable package for sharing or backup.
- **Import assessment JSON** validates and imports a portable package as a new local assessment copy.

## Step 2: Inventory Review

Inventory Review is where you decide which VMs are in scope and inspect source-data quality.

### Warning Types

The tool can flag:

- Unsupported or remediation-required OCI Native OS values.
- Unknown or empty operating-system values.
- Missing or zero storage values.
- Missing or zero vCPU values.
- Missing or zero RAM values.
- Duplicate VM names.

Most sizing-data warnings are advisory. They do not necessarily block modeling, but they should be reviewed before external use.

### VM Inclusion

Use the table controls to include or exclude VMs from the assessment.

Only selected VMs flow into:

- Native scenario totals
- OCVS sizing
- Hybrid placement
- Results comparison
- Excel workbook selected VM sheets

Excluded VMs remain available for review and can appear in the non-selected VM sheet.

### Initial Placement

Each included VM receives a placement basis:

- **OCI Native**
- **OCVS**
- **Review**

Review is intentionally conservative. Review placement is priced as OCVS in the Hybrid path until the user confirms a final placement.

### Filters And Bulk Actions

Use filters to narrow the table by support state, power state, placement, or warning category. Bulk controls can include, exclude, or change placement for the filtered scope.

Use Undo when available after bulk changes if the result is not what you intended.

## Step 3: Scenario Configuration

Scenario Configuration contains three scenario tabs:

- Native
- OCVS
- Hybrid

Changes made here affect the complete selected workload, even when a table is paginated.

### Native Scenario

The Native scenario models selected VMs on OCI Compute and Block Volume.

Native inputs include:

- IaaS discount percentage
- Target compute shape family
- OCPU count per VM
- Burst setting
- Block Volume VPU/GB
- Windows Server license mode

Default Native OCPU values are derived from source vCPU and can be edited per VM. Costs are based on selected OCPU, memory, block storage, VPU, burst, discount, and optional Windows license-included pricing.

Native support status does not remove VMs from the Native model. Unsupported or remediation-required VMs remain visible and priced, but they create readiness notes and may require documented treatment before a Native recommendation is customer-ready.

### Native Bulk Defaults

The Native workload editor supports bulk changes for:

- Target shape
- Burst
- VPU
- Windows license

Bulk changes apply to the current Native editor scope. Scenario totals still use all selected VMs.

### OCVS Scenario

The OCVS scenario models the full selected workload on Oracle Cloud VMware Solution.

OCVS assumptions include:

- OCVS node profile
- Commitment term
- vCPU per OCPU
- CPU headroom
- RAM headroom
- Storage headroom
- Dense vSAN usable percentage
- Standard storage VPU/GB
- Additional spare nodes
- VCF list price per physical core/year

The app compares supported OCVS host profiles and selects the cost-optimized profile when `Cost optimized` is selected. You can also force a specific shape.

OCVS Standard shapes use Block Volume storage. Dense shapes model local vSAN capacity and use the configured dense vSAN usable percentage.

The app applies minimum host counts and reports multi-cluster planning notes when node counts exceed the modeled cluster limit.

### OCVS Commitment Terms

The supported commitment terms are:

- Pay as you go
- 1-Year
- 3-Year

Term discounts are read from the local OCVS term-discount configuration and applied to OCVS host cost where applicable.

### VCF Licensing

VCF licensing is modeled as an optional add-on.

If a list price per physical core/year is entered, the app adds a monthly VCF run-rate to OCVS and Hybrid OCVS costs. If the field is zero or blank, infrastructure still remains rankable and the workbook notes the license coverage scope separately.

Always validate Broadcom/VMware license portability, minimums, add-ons, and compliance outside this tool.

### Hybrid Scenario

The Hybrid scenario combines Native and OCVS placement.

Each selected VM can be placed as:

- **OCI Native**
- **OCVS**
- **Review (priced as OCVS)**

Hybrid uses the selected per-VM placements to split workload cost:

- Native-placed VMs use the Native model.
- OCVS-placed VMs use the Hybrid OCVS subset model.
- Review VMs are conservatively priced as OCVS.

Hybrid starts by inheriting the OCVS scenario assumptions. In the Hybrid tab, you can customize separate OCVS assumptions for the OCVS subset. Once saved, Hybrid keeps its own values until changed again.

This lets you model, for example, a full OCVS scenario with one shape or license price while modeling the Hybrid OCVS subset with different sizing or licensing assumptions.

## Step 4: Results And Export

Results is the final review area.

It includes:

- Workload profile
- Migration path comparison
- Specialist recommendation
- Save and export actions

### Workload Profile

The workload profile summarizes:

- Selected VM count
- Total vCPU
- Total RAM
- Total storage
- Power-state mix
- Operating-system mix
- OCI Native readiness split
- Average vCPU, RAM, and storage per VM

Use this section to sanity-check the selected estate before relying on the cost comparison.

### Migration Path Comparison

The comparison cards show Native, OCVS, and Hybrid side by side.

For each path, review:

- Technical eligibility
- Pricing completeness
- Monthly cost
- Annual and 3-year cost
- Cost per VM per month
- Main assumptions and sizing notes
- Benefits and trade-offs

The lowest complete modeled price is a comparison signal only. It is not an automatic recommendation.

### Specialist Recommendation

The recommendation section records a human decision for reporting.

Options:

- OCI Native
- OCVS
- Hybrid
- Undecided

The recommendation does not recalculate costs, change ranking, or modify the model. It is saved as internal/report metadata.

Use Internal notes to record rationale, assumptions, dependencies, or review comments.

### Export Center

Export options:

- **Save assessment**
  Saves the current assessment locally.

- **Export assessment JSON**
  Creates a portable assessment package.

- **Import assessment JSON**
  Imports a portable assessment package.

- **Export Excel**
  Creates the assessment workbook.

The Excel workbook remains available even when readiness is incomplete. The export status indicates whether the workbook is draft or customer-ready.

## Modeling Logic

This section describes the major model assumptions in practical terms.

### Inventory Normalization

Inventory rows are normalized into VM records with:

- Unique VM name
- Source VM name
- Duplicate index when needed
- Power state
- Raw OS value
- Mapped OS value
- vCPU
- RAM MB
- Provisioned MiB

Duplicate VM names are kept and disambiguated with suffixes. Review duplicates before final export.

### OCI Native Support Mapping

The app uses local OS support mapping to decide whether a VM is supported for OCI Native, requires remediation, or needs review.

Unknown, empty, unsupported, or legacy operating systems create review/remediation signals. They are not automatically removed from the Native cost model.

### Native Cost Model

Native cost is modeled from:

- Compute OCPU cost
- Memory cost
- Block Volume capacity cost
- Block Volume performance cost
- Optional Windows Server license-included cost
- IaaS discount
- Burst factor

Windows Server VMs can be modeled as:

- BYOL
- License included

Non-Windows VMs do not use the Windows license selector.

Storage uses a minimum modeled Block Volume size where needed so missing or tiny source values do not create impossible storage pricing.

### OCVS Cost Model

OCVS sizing compares workload demand against host profile capacity.

The tool considers:

- Total selected vCPU
- Total selected RAM
- Total selected storage
- vCPU per OCPU ratio
- CPU, RAM, and storage headroom
- Host profile CPU, RAM, and storage capacity
- Minimum host count
- Maximum modeled cluster size
- Additional spare nodes

For Standard OCVS shapes, storage is modeled separately with Block Volume and VPU/GB. For Dense shapes, storage is modeled from local NVMe/vSAN capacity using the dense vSAN usable percentage.

The selected OCVS profile is the profile with the lowest modeled selection cost unless a specific profile is forced.

### Hybrid Cost Model

Hybrid adds:

- Native subtotal for Native-placed VMs
- OCVS infrastructure subtotal for OCVS and Review VMs
- Optional Hybrid VCF license subtotal

If no VMs are placed on OCVS or Review, the Hybrid OCVS subset can show zero OCVS-priced VMs. That does not mean the assessment has no VMs; it means all Hybrid placements are currently Native.

### Price Ranking

Ranking uses complete, rankable modeled monthly cost.

The recommendation remains independent from price ranking. A migration specialist may recommend a higher-cost path for reasons such as risk, legacy OS support, dependency constraints, operational continuity, licensing, or migration sequencing.

## Readiness Logic

Readiness is designed to separate modeling from sign-off.

### Setup Readiness

Setup is ready when:

- Assessment name is present.
- Customer or project name is present.
- OCI price list is selected.
- Inventory is selected or created.

### Inventory Readiness

Inventory is ready when:

- At least one VM is included.
- Included VMs have valid placement values.
- Critical inventory issues are resolved.

Advisory issues can remain, but they are surfaced in readiness notes.

### Scenario Readiness

Scenario readiness depends on:

- Saved scenario assumptions.
- At least one rankable scenario.
- No critical scenario blockers.

Native can be rankable while still needing remediation treatment for unsupported VMs.

### Customer-Ready Export

Customer-ready status requires a selected recommendation that satisfies readiness rules.

For Native recommendations, unsupported Native VMs require appropriate warning acknowledgment and rationale before customer-ready status is granted.

Draft exports are available while readiness items remain open.

## Outputs

Excel reports are written to:

```text
downloads/exports/
```

The workbook includes:

- Executive Summary
- Price Comparison
- OCI Native Analysis
- OCVS Analysis
- Hybrid Analysis
- Hybrid Placement
- Selected VMs
- Non-Selected VMs
- Price List
- Technical Details

The workbook includes readiness status, specialist recommendation, modeled pricing, sizing details, assumptions, and selected workload detail.

Portable JSON files are exported through the browser download flow and can be imported later.

## Common Usage Patterns

### Quick All-Native Assessment

1. Load inventory and price list.
2. Include the target VM scope.
3. Review Native support findings.
4. In Native, confirm shape, OCPU, VPU, burst, discount, and Windows licensing.
5. Save scenario settings.
6. Open Results and review cost and readiness.
7. Record a Native recommendation only if remediation requirements are understood and documented.

### OCVS Lift-And-Shift Assessment

1. Load full inventory and price list.
2. Include all migration-scope VMs.
3. Open OCVS.
4. Select `Cost optimized` or force a required OCVS profile.
5. Confirm commitment term and headroom.
6. Enter VCF list price if license run-rate should be included.
7. Review node count, storage model, and multi-cluster notes.
8. Export the workbook.

### Hybrid Assessment

1. Complete Setup and Inventory Review.
2. Open Hybrid.
3. Review the initial placement split.
4. Use filters and bulk placement to route legacy, unsupported, dependent, or high-risk workloads to OCVS or Review.
5. Customize Hybrid OCVS assumptions if the OCVS subset should differ from the full OCVS scenario.
6. Save and compare Results.

### Sharing An Assessment

1. Open Results.
2. Click `Save assessment`.
3. Click `Export assessment JSON`.
4. Share the JSON through an approved channel.
5. The recipient imports it with `Import assessment JSON`.

## Troubleshooting

### I See "No VMs Currently Priced On OCVS"

This message refers to the OCVS subset, not the whole assessment.

In Hybrid, it means all selected VMs are currently placed on OCI Native. Change some VM placements to OCVS or Review, then recalculate and save.

### Step 3 Or Results Redirects Back To Setup

The current browser session may not have an active inventory selected.

Return to Setup and select or import an assessment/inventory.

### Costs Are Zero Or Incomplete

Check:

- A price list is selected.
- The price list contains matching compute and memory price items.
- OCVS shape pricing is present for the selected shape.
- Windows license-included VMs have Windows OS pricing available.

### Hybrid Does Not Change When OCVS Changes

Hybrid inherits OCVS assumptions until Hybrid assumptions are customized and saved. Once customized, Hybrid keeps its own OCVS subset assumptions.

To make Hybrid match OCVS again, set the Hybrid OCVS fields to the same values as the OCVS scenario and save Hybrid.

### Imported Assessment Does Not Appear To Load

Portable imports create a new local assessment copy. Check Setup for the imported assessment name and confirm that inventory and pricing were reconstructed.

### Browser Shows Old UI

Restart the Flask process and reload the browser page. The development server does not always hot-reload depending on how it was started.

## Recommended Operating Practices

- Save the assessment before exporting.
- Keep a portable JSON copy for important assessment milestones.
- Treat local output folders as customer-sensitive.
- Review all readiness notes before sending results externally.
- Validate all commercial pricing with official Oracle processes.
- Validate Broadcom/VMware and Microsoft license assumptions separately.
- Use the recommendation field as a human decision record, not as a pricing input.
- Document why the recommended path was chosen, especially when it is not the lowest modeled cost.
