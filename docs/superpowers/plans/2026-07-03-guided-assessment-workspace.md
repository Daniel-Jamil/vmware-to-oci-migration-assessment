# Guided Assessment Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current disconnected setup, workload selection, scenario, and price screens with one Oracle Redwood-aligned four-stage assessment workspace, while preserving existing sizing and pricing calculations and adding readiness, recommendation, portable JSON, and draft/customer-ready export behavior.

**Architecture:** Keep Flask/Jinja and the existing `/`, `/step3`, and `/step4` compatibility routes. Add a pure backend readiness module and a pure portable-package module, have `app.py` adapt current inventory and pricing data into those contracts, and render all stages through a shared server-rendered shell. Move visual tokens and browser interactions out of the large templates into focused static CSS and JavaScript. Server-side state and calculations remain authoritative; JavaScript provides filtering, accessible tabs, dirty-state messaging, bulk-edit Undo, and responsive presentation only.

**Tech Stack:** Python 3, Flask, Jinja2, standard-library `unittest`, vanilla JavaScript, CSS, existing XLSX writer and pricing/sizing helpers.

---

## Working Rules

- Keep existing URLs, scenario aliases, manual sizing, OCVS discounts, workbook formulas, and saved-assessment behavior compatible.
- Do not implement Word proposal generation in this plan.
- Do not create a pull request. Commit each completed task locally and leave the worktree clean.
- Before every completion claim, run the verification commands in the final task and inspect their actual output.
- Treat `unsupported-native` as advisory remediation: Native remains eligible and rankable when pricing is complete.
- Treat missing CPU, memory, and storage as critical inventory data errors.
- Treat missing VCF unit price as a pricing blocker for OCVS or Hybrid only when that scenario has OCVS physical cores.
- Do not persist computed readiness. Persist only inputs, warning acknowledgments, recommendation, and rationale.

## Target Readiness Contract

`assessment_readiness.build_assessment_readiness()` returns this stable shape to every route, template, saved-assessment summary, and export:

```python
{
    "overall_state": "draft_review_required",
    "stages": {
        "setup": {"state": "complete", "blockers": [], "advisories": []},
        "inventory": {"state": "complete", "blockers": [], "advisories": []},
        "scenarios": {"state": "complete", "blockers": [], "advisories": []},
        "results": {"state": "needs_attention", "blockers": [], "advisories": []},
    },
    "scenarios": {
        "native": {
            "technical_eligibility": "eligible",
            "pricing_state": "complete",
            "state": "needs_attention",
            "rankable": True,
            "remediation_required": True,
            "affected_vm_names": ["legacy-vm"],
            "customer_ready": False,
        },
        "ocvs": {
            "technical_eligibility": "eligible",
            "pricing_state": "incomplete",
            "state": "incomplete",
            "rankable": False,
            "remediation_required": False,
            "affected_vm_names": [],
            "customer_ready": False,
        },
        "hybrid": {
            "technical_eligibility": "eligible",
            "pricing_state": "incomplete",
            "state": "incomplete",
            "rankable": False,
            "remediation_required": False,
            "affected_vm_names": [],
            "customer_ready": False,
        },
    },
    "blocking_items": [],
    "advisory_items": [],
    "lowest_complete_scenario": "native",
    "customer_ready_export": False,
}
```

Scenario costs remain in the existing scenario view objects. The readiness builder receives normalized monthly costs only to identify the lowest complete modeled price; it does not recalculate pricing.

## Task 1: Add the Pure Readiness Model

**Files:**
- Create: `assessment_readiness.py`
- Create: `tests/__init__.py`
- Create: `tests/test_assessment_readiness.py`

- [x] **Step 1: Write failing readiness contract tests**

Create `tests/test_assessment_readiness.py` with a reusable complete context and focused tests:

```python
import unittest

from assessment_readiness import build_assessment_readiness


def complete_context() -> dict:
    return {
        "setup": {
            "assessment_name": "Customer migration",
            "customer_name": "Example Customer",
            "has_price_list": True,
            "has_inventory": True,
        },
        "inventory": {
            "included_vm_names": ["app-01", "legacy-01"],
            "placements": {"app-01": "native", "legacy-01": "ocvs"},
            "issues": [
                {
                    "id": "unsupported-native",
                    "severity": "advisory",
                    "vm_names": ["legacy-01"],
                }
            ],
            "acknowledged_warning_ids": ["unsupported-native"],
        },
        "scenarios": {
            "native": {
                "technically_eligible": True,
                "pricing_complete": True,
                "monthly_cost": 100.0,
                "unsupported_vm_names": ["legacy-01"],
            },
            "ocvs": {
                "technically_eligible": True,
                "pricing_complete": True,
                "monthly_cost": 200.0,
                "unsupported_vm_names": [],
            },
            "hybrid": {
                "technically_eligible": True,
                "pricing_complete": True,
                "monthly_cost": 150.0,
                "unsupported_vm_names": [],
            },
        },
        "has_unsaved_scenario_changes": False,
        "recommendation": "",
        "recommendation_rationale": "",
    }


class ReadinessTests(unittest.TestCase):
    def test_native_stays_eligible_and_rankable_with_unsupported_vms(self) -> None:
        result = build_assessment_readiness(complete_context())
        native = result["scenarios"]["native"]
        self.assertEqual("eligible", native["technical_eligibility"])
        self.assertEqual("needs_attention", native["state"])
        self.assertTrue(native["rankable"])
        self.assertEqual("native", result["lowest_complete_scenario"])

    def test_missing_vcf_price_excludes_ocvs_and_hybrid_from_ranking(self) -> None:
        context = complete_context()
        context["scenarios"]["native"]["monthly_cost"] = 300.0
        context["scenarios"]["ocvs"]["pricing_complete"] = False
        context["scenarios"]["ocvs"]["monthly_cost"] = 100.0
        context["scenarios"]["hybrid"]["pricing_complete"] = False
        context["scenarios"]["hybrid"]["monthly_cost"] = 150.0
        result = build_assessment_readiness(context)
        self.assertFalse(result["scenarios"]["ocvs"]["rankable"])
        self.assertFalse(result["scenarios"]["hybrid"]["rankable"])
        self.assertEqual("native", result["lowest_complete_scenario"])

    def test_native_recommendation_requires_acknowledgment_and_treatment_rationale(self) -> None:
        context = complete_context()
        context["recommendation"] = "native"
        context["recommendation_rationale"] = "Remediate legacy-01 before its Native migration wave."
        ready = build_assessment_readiness(context)
        self.assertEqual("customer_ready", ready["overall_state"])
        self.assertTrue(ready["customer_ready_export"])

        context["recommendation_rationale"] = ""
        draft = build_assessment_readiness(context)
        self.assertEqual("draft_review_required", draft["overall_state"])
        self.assertFalse(draft["customer_ready_export"])

    def test_critical_inventory_issue_blocks_stage_two(self) -> None:
        context = complete_context()
        context["inventory"]["issues"].append(
            {"id": "missing-storage", "severity": "critical", "vm_names": ["app-01"]}
        )
        result = build_assessment_readiness(context)
        self.assertEqual("needs_attention", result["stages"]["inventory"]["state"])
        self.assertEqual("incomplete", result["overall_state"])


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run the tests and confirm the expected import failure**

Run:

```bash
./.venv/bin/python -m unittest tests.test_assessment_readiness -v
```

Expected: `ModuleNotFoundError: No module named 'assessment_readiness'`.

- [x] **Step 3: Implement the readiness builder**

Create `assessment_readiness.py` with:

```python
from __future__ import annotations

from typing import Any, Mapping

VALID_RECOMMENDATIONS = {"", "native", "ocvs", "hybrid"}
CRITICAL_INVENTORY_ISSUES = {"missing-storage", "missing-cpu", "missing-memory"}


def build_assessment_readiness(context: Mapping[str, Any]) -> dict[str, Any]:
    setup = dict(context.get("setup") or {})
    inventory = dict(context.get("inventory") or {})
    scenario_inputs = dict(context.get("scenarios") or {})
    recommendation = str(context.get("recommendation") or "").strip().lower()
    if recommendation not in VALID_RECOMMENDATIONS:
        recommendation = ""
    rationale = str(context.get("recommendation_rationale") or "").strip()

    issues = [dict(issue) for issue in inventory.get("issues") or [] if isinstance(issue, Mapping)]
    acknowledged = {str(value) for value in inventory.get("acknowledged_warning_ids") or []}
    critical = [issue for issue in issues if issue.get("severity") == "critical"]
    unacknowledged = [
        issue
        for issue in issues
        if issue.get("severity") != "critical" and str(issue.get("id") or "") not in acknowledged
    ]

    scenario_results: dict[str, dict[str, Any]] = {}
    for scenario_id in ("native", "ocvs", "hybrid"):
        source = dict(scenario_inputs.get(scenario_id) or {})
        eligible = bool(source.get("technically_eligible"))
        pricing_complete = bool(source.get("pricing_complete"))
        unsupported = [str(name) for name in source.get("unsupported_vm_names") or []]
        remediation_required = scenario_id == "native" and bool(unsupported)
        rankable = eligible and pricing_complete
        state = "incomplete" if not rankable else "needs_attention" if remediation_required else "ready"
        scenario_results[scenario_id] = {
            "technical_eligibility": "eligible" if eligible else "ineligible",
            "pricing_state": "complete" if pricing_complete else "incomplete",
            "state": state,
            "rankable": rankable,
            "remediation_required": remediation_required,
            "affected_vm_names": unsupported,
            "customer_ready": rankable and not remediation_required,
            "monthly_cost": float(source.get("monthly_cost") or 0.0),
        }

    ranked = [
        (values["monthly_cost"], scenario_id)
        for scenario_id, values in scenario_results.items()
        if values["rankable"]
    ]
    lowest_complete = min(ranked)[1] if ranked else ""

    selected = scenario_results.get(recommendation)
    native_treatment_ready = not (
        recommendation == "native"
        and selected
        and selected["remediation_required"]
        and ("unsupported-native" not in acknowledged or not rationale)
    )
    if selected:
        selected["customer_ready"] = bool(selected["rankable"] and native_treatment_ready)
    customer_ready = bool(
        selected
        and selected["rankable"]
        and not critical
        and not unacknowledged
        and not context.get("has_unsaved_scenario_changes")
        and native_treatment_ready
    )

    setup_ready = all(
        (
            str(setup.get("assessment_name") or "").strip(),
            str(setup.get("customer_name") or "").strip(),
            setup.get("has_price_list"),
            setup.get("has_inventory"),
        )
    )
    included = [str(name) for name in inventory.get("included_vm_names") or []]
    placements = dict(inventory.get("placements") or {})
    inventory_ready = bool(included) and not critical and not unacknowledged and all(
        placements.get(name) in {"native", "ocvs", "review"} for name in included
    )
    scenarios_complete = not context.get("has_unsaved_scenario_changes") and any(
        values["rankable"] for values in scenario_results.values()
    )

    overall = "customer_ready" if customer_ready else "incomplete" if not (
        setup_ready and inventory_ready and scenarios_complete
    ) else "draft_review_required"
    return {
        "overall_state": overall,
        "stages": {
            "setup": {"state": "complete" if setup_ready else "needs_attention", "blockers": [], "advisories": []},
            "inventory": {"state": "complete" if inventory_ready else "needs_attention", "blockers": critical, "advisories": unacknowledged},
            "scenarios": {"state": "complete" if scenarios_complete else "needs_attention", "blockers": [], "advisories": []},
            "results": {"state": "complete" if customer_ready else "needs_attention", "blockers": [], "advisories": []},
        },
        "scenarios": scenario_results,
        "blocking_items": critical,
        "advisory_items": unacknowledged,
        "lowest_complete_scenario": lowest_complete,
        "customer_ready_export": customer_ready,
    }
```

During implementation, keep the returned keys exactly as shown but enrich `blockers` and `advisories` with stable `id`, `title`, `detail`, `stage`, and `affected_vm_names` fields from the adapter in `app.py`.

- [x] **Step 4: Run the readiness tests**

Run:

```bash
./.venv/bin/python -m unittest tests.test_assessment_readiness -v
```

Expected: 4 tests pass.

- [x] **Step 5: Commit**

```bash
git add assessment_readiness.py tests/__init__.py tests/test_assessment_readiness.py
git commit -m "feat: add assessment readiness model"
```

## Task 2: Persist Review and Recommendation Inputs Safely

**Files:**
- Modify: `app.py:319-605`
- Modify: `app.py:683-817`
- Modify: `tests/regression_check.py:442-584`

- [x] **Step 1: Add failing backward-compatibility and round-trip checks**

Extend `validate_saved_assessments()` to verify:

```python
state["acknowledged_warning_ids"] = ["unsupported-native"]
state["assessor_recommendation"] = "native"
state["assessor_recommendation_rationale"] = "Remediate legacy guests before migration."
app_module.save_app_state(state)
```

After load, assert all three fields are restored. Also write an old-format state JSON without those keys and assert `load_app_state()` returns `[]`, `""`, and `""` respectively.

- [x] **Step 2: Run the regression and confirm the new assertion fails**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: the new persisted-state check reports `FAIL`.

- [x] **Step 3: Add defaults and normalization**

Add to `_default_app_state()`:

```python
"acknowledged_warning_ids": [],
"assessor_recommendation": "",
"assessor_recommendation_rationale": "",
```

In `load_app_state()`:

- Keep only unique warning IDs that match `^[a-z0-9][a-z0-9-]{0,79}$`.
- Normalize recommendation to `""`, `"native"`, `"ocvs"`, or `"hybrid"`.
- Limit rationale to 4,000 characters.
- Preserve the current default-update behavior so older snapshots gain the fields automatically.

- [x] **Step 4: Verify local saved-assessment snapshots round-trip the new state**

The current `save_current_assessment()` already writes `app_state`; do not duplicate the fields at the snapshot top level. Confirm `load_saved_assessment()` routes the snapshot through the same normalization before saving it into the active state file.

- [x] **Step 5: Run the full regression**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: `All regression checks passed.`

- [x] **Step 6: Commit**

```bash
git add app.py tests/regression_check.py
git commit -m "feat: persist assessment review decisions"
```

## Task 3: Create the Shared Redwood Workspace Shell

**Files:**
- Create: `templates/base.html`
- Create: `templates/_stage_nav.html`
- Create: `templates/_readiness_panel.html`
- Create: `templates/_assessment_menu.html`
- Create: `static/css/workspace.css`
- Create: `static/js/workspace.js`
- Modify: `templates/index.html`
- Modify: `templates/step3.html`
- Modify: `templates/step4.html`
- Modify: `app.py:5449-6614`
- Modify: `tests/regression_check.py:591-816`

- [x] **Step 1: Add failing shell checks**

For `GET /`, `GET /step3`, and `GET /step4?tab=native`, assert the rendered response contains:

```html
<header class="workspace-header">
<nav class="stage-nav" aria-label="Assessment stages">
<main id="main-workspace">
<div id="workspace-status" role="status" aria-live="polite">
```

Assert each page contains `Step 1 of 4`, `Step 2 of 4`, or `Step 3 of 4` as appropriate and never contains the old color-system explanation.

- [x] **Step 2: Run the regression and confirm shell checks fail**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: the first shared-shell assertion reports `FAIL`.

- [x] **Step 3: Build `base.html`**

Use one document shell with blocks `title`, `head`, `stage_content`, and `scripts`. Include the assets with `url_for("static", filename="css/workspace.css")` and `url_for("static", filename="js/workspace.js")`. Render:

- Oracle wordmark text as the restrained brand signal.
- Product name `VMware to OCI Migration Assessment`.
- Active assessment and customer names with safe truncation.
- Saved/unsaved status.
- `_assessment_menu.html` for Save, Open, Import, and Export actions.
- `_stage_nav.html` on desktop and a native `<select>` stage navigator on mobile.
- `_readiness_panel.html` only when readiness contains blockers or advisories.
- A skip link targeting `#main-workspace`.
- Previous/Continue footer actions supplied by each route.

- [x] **Step 4: Add the common template context adapter**

Create `build_workspace_context(stage_id, readiness, **values)` in `app.py`. It must supply:

```python
{
    "workspace_stage": "setup",
    "workspace_stage_number": 1,
    "workspace_stage_count": 4,
    "workspace_readiness": readiness,
    "workspace_assessment_name": active_assessment_name,
    "workspace_customer_name": customer_name,
    "workspace_is_saved": bool(active_assessment_id),
    "workspace_previous_url": "",
    "workspace_continue_url": url_for("step3"),
}
```

Map stage IDs to visible names and compatibility URLs in one constant. Stage 4 maps to `/step4?tab=price`; Stage 3 maps only to `native`, `ocvs`, and `hybrid` tabs.

- [x] **Step 5: Move shared visual rules into `workspace.css`**

Define Redwood-aligned tokens for neutral surfaces, Oracle red brand accent, green/teal primary actions, amber review states, and red errors. Include:

- Fixed desktop rail track with `minmax(0, 1fr)` workspace.
- `min-width: 0` for every grid child.
- 4px visible focus ring.
- Status icons plus text so color is never the only signal.
- Buttons with stable height and icon slots.
- Cards at 8px radius or less.
- No gradients, decorative orbs, nested cards, or viewport-scaled type.

- [x] **Step 6: Implement common interactions in `workspace.js`**

Implement stage-select navigation, assessment-menu open/close, Escape handling, outside-click close, and focus return. Do not implement readiness rules in JavaScript.

- [x] **Step 7: Convert the three templates to extend `base.html`**

Initially wrap their existing inner content in `stage_content`; later tasks replace that content. Remove duplicate `<html>`, `<head>`, header, progress, shared button CSS, and shared scripts from each template.

- [x] **Step 8: Run template and regression checks**

Run:

```bash
./.venv/bin/python -m compileall app.py assessment_readiness.py
./.venv/bin/python tests/regression_check.py
```

Expected: compilation succeeds and all regression checks pass.

- [x] **Step 9: Commit**

```bash
git add app.py templates static tests/regression_check.py
git commit -m "feat: add shared Redwood assessment shell"
```

## Task 4: Redesign Stage 1 Setup and Preserve Valid Sources on Error

**Files:**
- Create: `templates/_source_details.html`
- Create: `static/js/setup.js`
- Modify: `templates/index.html`
- Modify: `app.py:5449-5851`
- Modify: `tests/regression_check.py:315-441`

- [x] **Step 1: Add failing Stage 1 checks**

Add regression checks for:

- Assessment name, customer/project name, and notes in the identity section.
- A pricing status summary plus collapsed `Source Details` disclosure.
- An `inventory_mode` segmented control with `upload` and `manual` values.
- Existing manual values remaining editable after creation.
- A failed replacement upload preserving `session["selected_rvtools_file"]` and the selected VM state.
- No full local path rendered outside the collapsed source disclosure.

For replacement preservation, create a valid manual inventory, capture its path, upload invalid CSV bytes, and assert the path and prior selected VM names remain unchanged.

- [x] **Step 2: Run the regression and confirm the preservation check fails**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: the invalid-replacement preservation check reports `FAIL` until route handling is transactional.

- [x] **Step 3: Separate validation from inventory activation**

Refactor Stage 1 helpers so upload/manual actions:

1. Parse and validate into a generated candidate file.
2. Load the candidate with `load_vms_from_vinfo()`.
3. Build summary and warnings.
4. Only then update session source metadata and reset compatible selection state.
5. Delete the candidate on validation failure.

Do not call the current source-clearing helper before candidate validation succeeds.

- [x] **Step 4: Rebuild Stage 1 content**

Render three unframed sections:

1. Assessment Identity.
2. OCI Pricing.
3. Inventory Source.

Keep the local saved-assessment library in the global menu rather than a fixed right rail. Use `fieldset` and `legend` for the inventory mode. `setup.js` toggles panels with `hidden`, keeps focus on the selected mode, and does not erase values in the inactive panel.

- [x] **Step 5: Add field-level errors**

Return a `field_errors` mapping from failed POST actions and render each error next to its control with `aria-describedby`. Place a linked error summary at the top and focus it after response load.

- [x] **Step 6: Run Stage 1 and full regression checks**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: all checks pass, including manual create/update and replacement preservation.

- [x] **Step 7: Commit**

```bash
git add app.py templates/index.html templates/_source_details.html static/js/setup.js tests/regression_check.py
git commit -m "feat: redesign setup and inventory source flow"
```

## Task 5: Replace Workload Transfer Tables with Inventory Review

**Files:**
- Create: `templates/_warning_inbox.html`
- Create: `templates/_inventory_table.html`
- Create: `static/css/inventory-review.css`
- Create: `static/js/inventory-review.js`
- Modify: `templates/step3.html`
- Modify: `app.py:2072-2255`
- Modify: `app.py:5855-6021`
- Modify: `tests/regression_check.py:230-314`

- [x] **Step 1: Add failing Stage 2 state and markup checks**

Exercise `POST /step3` with a single `save_inventory_review` action containing repeated `included_vm_names` and `placement:<encoded-vm-name>` controls. Assert:

- Included names replace, rather than merge with, the old selection.
- Unsupported VMs default to `ocvs`; supported VMs default to `native`.
- `acknowledged_warning_ids` persists only advisory warning IDs currently present.
- Critical warnings cannot be acknowledged.
- The response has native checkboxes, `aria-sort`, warning filter buttons, bulk placement controls, and an Undo live region.

- [x] **Step 2: Run the focused regression and confirm failure**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: the new `save_inventory_review` assertion reports `FAIL`.

- [x] **Step 3: Normalize warning severity and VM identifiers**

Extend `build_inventory_review_issues()` so every issue includes:

```python
{
    "id": "unsupported-native",
    "severity": "advisory",
    "vm_names": ["legacy-01"],
    "title": "Unsupported for OCI Native",
    "detail": "The guest requires a documented Native treatment.",
    "default_action": "Review treatment",
}
```

Use `critical` for `missing-storage`, `missing-cpu`, and `missing-memory`. Use `advisory` for `unsupported-native`, `unknown-os`, and `duplicate-vm-name`.

- [x] **Step 4: Implement one authoritative save action**

In `/step3`, implement `save_inventory_review` to:

- Validate names against the loaded inventory index.
- Require at least one included VM before continuing.
- Save `selected_vm_names` in source order.
- Save `step4_hybrid_placements` only for included names.
- Default unsupported names to `ocvs`, supported names to `native`, and unknown decisions to `review`.
- Save only valid advisory acknowledgments.
- Redirect to `/step4?tab=native` when `continue_to_scenarios=1` and Stage 2 has no critical or unacknowledged advisory issue.

Keep old `add`, `remove`, and `remove_duplicates` handlers temporarily for bookmarked forms and existing regression compatibility, but do not render them in the redesigned UI.

- [x] **Step 5: Render warning inbox and single inventory list**

The inventory table columns are inclusion, name, power, OS, Native support, vCPU, RAM, storage, suggested placement, warning, and details. Use checkbox IDs derived from row index, never raw VM names. Add programmatic labels and sortable header buttons with `aria-sort`.

- [x] **Step 6: Implement filtering, bulk placement, and Undo**

`inventory-review.js` must:

- Filter rows by search, support, power, placement, and warning ID.
- State whether bulk action scope is page, filtered rows, or all loaded rows.
- Snapshot checkbox and placement values before a bulk action.
- Show a live confirmation with one Undo button.
- Restore the snapshot on Undo.
- Leave final persistence to the form submit.

- [x] **Step 7: Add mobile list behavior**

At widths below 768px, hide nonessential table columns, retain inclusion/name/status, and expose remaining fields through a `<details>` row panel. Do not duplicate form controls into a second DOM tree.

- [x] **Step 8: Run regression checks**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: all checks pass.

- [x] **Step 9: Commit**

```bash
git add app.py templates/step3.html templates/_warning_inbox.html templates/_inventory_table.html static/css/inventory-review.css static/js/inventory-review.js tests/regression_check.py
git commit -m "feat: add guided inventory review workspace"
```

## Task 6: Integrate Readiness into Current Assessment Data

**Files:**
- Modify: `app.py:3434-3830`
- Modify: `app.py:6023-6614`
- Modify: `assessment_readiness.py`
- Modify: `tests/test_assessment_readiness.py`
- Modify: `tests/regression_check.py:911-1030`

- [x] **Step 1: Add failing adapter integration tests**

Build a selected workload with one unsupported VM and complete Native pricing. Assert the route context reports:

- Native `technical_eligibility == "eligible"`.
- Native `state == "needs_attention"`.
- Native `rankable is True`.
- `unsupported-native` remains visible after acknowledgment.

Set OCVS physical cores above zero and VCF unit price to zero. Assert OCVS and Hybrid pricing state is `incomplete`, `rankable is False`, and `fit_warnings` appear in the readiness payload.

- [x] **Step 2: Run tests and confirm adapter assertions fail**

Run:

```bash
./.venv/bin/python -m unittest tests.test_assessment_readiness -v
./.venv/bin/python tests/regression_check.py
```

Expected: pure tests pass; route adapter checks fail.

- [x] **Step 3: Implement `build_current_readiness_context()` in `app.py`**

The adapter must receive already-loaded inventory rows, selected names, scenario views, and app state. It must:

- Convert inventory issues to critical/advisory readiness items.
- Include only selected VMs in placement completion.
- Determine Native unsupported VM names from the same support function used by the scenario engine.
- Set OCVS pricing complete only if infrastructure prices are present and either physical cores are zero or VCF unit price is greater than zero.
- Apply the same rule to Hybrid's OCVS subset.
- Pass existing `fit_warnings` into blocking/advisory items instead of silently dropping them.
- Pass unsaved-change state from the submitted form error path; default false after successful save.
- Call `build_assessment_readiness()` exactly once per request.

- [x] **Step 4: Supply readiness to every stage**

Use the adapter in `/`, `/step3`, and `/step4`. For early stages where scenarios cannot yet be calculated, supply explicitly incomplete scenario inputs rather than inventing zero-cost complete scenarios.

- [x] **Step 5: Run all readiness and regression checks**

Run:

```bash
./.venv/bin/python -m unittest tests.test_assessment_readiness -v
./.venv/bin/python tests/regression_check.py
```

Expected: all tests pass.

- [x] **Step 6: Commit**

```bash
git add app.py assessment_readiness.py tests/test_assessment_readiness.py tests/regression_check.py
git commit -m "feat: connect readiness to assessment calculations"
```

## Task 7: Rebuild Stage 3 Scenario Navigation and Native Editor

**Files:**
- Create: `templates/_scenario_header.html`
- Create: `templates/_scenario_native.html`
- Create: `static/css/scenarios.css`
- Create: `static/js/scenario-editor.js`
- Modify: `templates/step4.html`
- Modify: `app.py:6023-6460`
- Modify: `tests/regression_check.py:591-816`

- [x] **Step 1: Add failing Stage 3 checks**

Assert `/step4?tab=native` renders:

- A `tablist` with only Native, OCVS, and Hybrid.
- Exactly one active tab with `aria-selected="true"` and `tabindex="0"`.
- No Migration Paths or Price Comparison tab.
- A status, cost, workload scope, capacity outcome, pending-change summary, and `Recalculate & Save` action.
- A search field, support filter, page-size 50, and explicit labels for every visible per-VM control.

Use 75 generated VMs and assert page 1 renders 50 editor rows while totals and workbook inputs still include all 75.

- [x] **Step 2: Run regression and confirm pagination checks fail**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: the bounded Native editor assertion reports `FAIL`.

- [x] **Step 3: Restrict visible Stage 3 tabs without changing aliases**

Keep `normalize_step4_scenario_tab()` and `/scenario/<scenario_id>` compatibility. Redirect `paths` to `/step3` and `price` to `/step4?tab=price`. Render only Native, OCVS, and Hybrid in Stage 3.

- [x] **Step 4: Add server-side Native pagination**

Accept validated query parameters:

```text
native_page=1
native_page_size=50
native_search=
native_support=all
```

Allow page sizes 25, 50, and 100. Filter and paginate only the editor rows. Calculate scenario totals from the complete selected VM list. On POST, update only controls present in the form and preserve stored choices for omitted pages.

- [x] **Step 5: Split and render the Native partial**

Use stable IDs based on row index and explicit `<label for>` text containing the VM name and setting. Keep VM and OS columns sticky inside an intentional horizontal scroll container. On mobile, edit one VM in a focused details panel without duplicating controls.

- [x] **Step 6: Implement accessible tab and dirty-state behavior**

`scenario-editor.js` implements roving tab focus with Left/Right/Home/End keys. On any form change:

- Mark the scenario form dirty.
- Update a live region.
- Keep `Recalculate & Save` reachable in a sticky action bar.
- Warn on tab or stage navigation while dirty.
- Clear dirty state after a successful redirected save.

- [x] **Step 7: Verify Native unsupported behavior**

Render `Requires remediation` beside affected Native rows and in the Native scenario header. Never disable Native, remove its cost, or label it ineligible solely because these VMs exist.

- [x] **Step 8: Run regression checks**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: all checks pass.

- [x] **Step 9: Commit**

```bash
git add app.py templates/step4.html templates/_scenario_header.html templates/_scenario_native.html static/css/scenarios.css static/js/scenario-editor.js tests/regression_check.py
git commit -m "feat: rebuild Native scenario workspace"
```

## Task 8: Rebuild OCVS and Hybrid Configuration

**Files:**
- Create: `templates/_scenario_ocvs.html`
- Create: `templates/_scenario_hybrid.html`
- Modify: `templates/step4.html`
- Modify: `static/css/scenarios.css`
- Modify: `static/js/scenario-editor.js`
- Modify: `app.py:6023-6460`
- Modify: `tests/regression_check.py:718-812`

- [x] **Step 1: Add failing OCVS and Hybrid checks**

Assert:

- OCVS fields are grouped under Profile & Term, Capacity Policy, Resilience, and VCF Licensing.
- The selected shape's 1-year or 3-year discount is rendered from `ocvs_term_discount_pct()`.
- Zero VCF price with physical cores renders `Pricing incomplete` and the partial infrastructure amount.
- Hybrid renders one shared OCVS assumptions notice and does not render a second independent VCF price input.
- Hybrid workload counts, OCVS subset sizing, and manual override count are present.
- OCVS and Hybrid become rankable after a positive VCF price is saved.

- [x] **Step 2: Run regression and confirm grouped/shared-control checks fail**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: the first new OCVS grouping assertion reports `FAIL`.

- [x] **Step 3: Split OCVS and Hybrid partials**

Move existing controls without changing their POST field names. Render `step4_ocvs_*` inputs only in the OCVS partial. Hybrid links to the OCVS tab for shared assumptions and renders only placement override controls.

- [x] **Step 4: Make blocker and discount status explicit**

Show:

- Commitment term.
- Shape-specific discount percentage.
- Infrastructure subtotal.
- VCF subtotal or `Unit price required`.
- Complete/partial total label.
- Readiness status from the backend contract.

Do not derive any of these status labels in JavaScript.

- [x] **Step 5: Add Hybrid filtering and bulk placement**

Reuse the Stage 2 placement vocabulary. Bulk actions snapshot changes for Undo and clearly state their scope. Count manual overrides by comparing saved placement to the initial supported/unsupported recommendation.

- [x] **Step 6: Run regression checks**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: all checks pass, including all existing OCVS discount invariants.

- [x] **Step 7: Commit**

```bash
git add app.py templates/step4.html templates/_scenario_ocvs.html templates/_scenario_hybrid.html static/css/scenarios.css static/js/scenario-editor.js tests/regression_check.py
git commit -m "feat: rebuild OCVS and Hybrid scenarios"
```

## Task 9: Build Results, Ranking, and Assessor Recommendation

**Files:**
- Create: `templates/_results_comparison.html`
- Create: `templates/_export_center.html`
- Create: `static/css/results.css`
- Modify: `templates/step4.html`
- Modify: `app.py:2419-2435`
- Modify: `app.py:6023-6614`
- Modify: `tests/test_assessment_readiness.py`
- Modify: `tests/regression_check.py:591-816`

- [x] **Step 1: Add failing Results checks**

Assert `/step4?tab=price` renders Stage 4 with:

- Overall readiness banner.
- Separate Technical eligibility, Pricing completeness, and Modeled cost fields.
- Monthly, annual, 3-year, and cost-per-VM values.
- Placement split, assumptions, benefits, trade-offs, and remediation requirements.
- `Lowest complete modeled price` on the cheapest rankable scenario only.
- No medal or automatic recommendation language.
- A recommendation radio group with Native, OCVS, Hybrid, and No recommendation yet.
- A rationale field.

POST recommendation and rationale, reload the page, then save/load the assessment and assert both persist.

- [x] **Step 2: Run tests and confirm recommendation action fails**

Run:

```bash
./.venv/bin/python -m unittest tests.test_assessment_readiness -v
./.venv/bin/python tests/regression_check.py
```

Expected: the Results recommendation persistence assertion reports `FAIL`.

- [x] **Step 3: Add `save_recommendation` handling**

Validate recommendation against the four allowed values, normalize rationale to at most 4,000 characters, save app state, and redirect back to `tab=price`. Allow incomplete scenarios to be selected for internal draft review.

- [x] **Step 4: Render comparison from readiness plus scenario views**

Use readiness only for eligibility/status/ranking. Use existing scenario views for cost and sizing details. Exclude non-rankable scenarios from `lowest_complete_scenario` but keep their partial cost visible with an `Incomplete pricing` label.

- [x] **Step 5: Enforce Native customer-ready treatment**

When Native is recommended and unsupported VMs exist, require both:

- `unsupported-native` is acknowledged.
- Recommendation rationale is nonempty and visible in Results.

Native remains eligible and rankable before these actions; only `customer_ready_export` stays false.

- [x] **Step 6: Render the initial export center**

Show Save assessment and Excel actions. Label Excel `Export Draft` unless `customer_ready_export` is true. Do not render portable JSON actions until Task 10 implements their complete server-side behavior.

- [x] **Step 7: Run all tests**

Run:

```bash
./.venv/bin/python -m unittest tests.test_assessment_readiness -v
./.venv/bin/python tests/regression_check.py
```

Expected: all tests pass.

- [x] **Step 8: Commit**

```bash
git add app.py assessment_readiness.py templates/step4.html templates/_results_comparison.html templates/_export_center.html static/css/results.css tests/test_assessment_readiness.py tests/regression_check.py
git commit -m "feat: add readiness results and recommendation"
```

## Task 10: Implement Portable Assessment JSON

**Files:**
- Create: `assessment_portability.py`
- Create: `tests/test_assessment_portability.py`
- Modify: `app.py:585-850`
- Modify: `app.py:5449-5851`
- Modify: `templates/_assessment_menu.html`
- Modify: `templates/_export_center.html`
- Modify: `tests/regression_check.py:442-584`

- [x] **Step 1: Write failing pure package tests**

Create tests for:

- `package_type == "vmware_to_oci_assessment"` and `schema_version == 1`.
- Deterministic JSON serialization.
- Required `assessment`, `inventory`, and `pricing` sections.
- Duplicate VM names, negative numbers, oversized strings, wrong package type, missing sections, and unsupported versions raising `PortableAssessmentError`.
- Imported filesystem path fields being ignored rather than opened.

Use this public module API:

```python
from assessment_portability import (
    PortableAssessmentError,
    build_portable_package,
    dumps_portable_package,
    validate_portable_package,
)
```

- [x] **Step 2: Run tests and confirm import failure**

Run:

```bash
./.venv/bin/python -m unittest tests.test_assessment_portability -v
```

Expected: `ModuleNotFoundError: No module named 'assessment_portability'`.

- [x] **Step 3: Implement package build and validation**

Use constants:

```python
PACKAGE_TYPE = "vmware_to_oci_assessment"
SCHEMA_VERSION = 1
MAX_PACKAGE_BYTES = 25 * 1024 * 1024
MAX_VM_ROWS = 100_000
MAX_TEXT_LENGTH = 4_000
```

The validator must return a new normalized object containing only supported keys. It must never return or dereference a supplied inventory path, price-list path, generated export path, or local assessment ID.

- [x] **Step 4: Add export helpers and action**

Build the package from the current session, normalized inventory rows, selected pricing JSON, app state, and Step 4 snapshot. If the active assessment already has a local ID, refresh its snapshot first. Return deterministic UTF-8 JSON with `send_file()` and a sanitized filename. An unsaved export must not create a local saved assessment.

- [x] **Step 5: Add transactional import helpers and action**

For `import_assessment`:

1. Reject non-`.json` and files over 25 MiB.
2. Parse and fully validate before changing session or saved library.
3. Generate a new local assessment ID.
4. Create inventory and price files under `downloads/imported_assessments/<new-id>/` using generated names.
5. Write a local snapshot atomically only after both dependencies are ready.
6. Use deterministic name suffixes ` (Imported 2)`, ` (Imported 3)`.
7. Load the snapshot through `load_saved_assessment()`.
8. On failure, remove generated temporary artifacts and preserve current state exactly.

- [x] **Step 6: Add end-to-end regression coverage**

Extend `tests/regression_check.py` to export an unsaved current assessment, delete or rename original inventory/pricing dependencies, import the JSON, and confirm:

- A new saved assessment is created and loaded immediately.
- Customer, notes, currency, normalized inventory, selected VMs, placements, discounts, commitment term, acknowledgments, recommendation, and rationale are restored.
- Invalid packages leave session state and saved library byte-for-byte unchanged.

- [x] **Step 7: Enable JSON controls**

Replace disabled placeholders in the global assessment menu and Stage 4 export center with labeled Export current assessment and Import assessment controls. Keep Load previous assessment visible and disabled when the library is empty.

- [x] **Step 8: Run portability and full regression tests**

Run:

```bash
./.venv/bin/python -m unittest tests.test_assessment_portability -v
./.venv/bin/python tests/regression_check.py
```

Expected: all tests pass.

- [x] **Step 9: Commit**

```bash
git add assessment_portability.py app.py templates/_assessment_menu.html templates/_export_center.html tests/test_assessment_portability.py tests/regression_check.py
git commit -m "feat: add portable assessment JSON"
```

## Task 11: Add Readiness and Recommendation to Excel

**Files:**
- Modify: `app.py:4206-5335`
- Modify: `tests/regression_check.py:818-910`

- [x] **Step 1: Add failing workbook assertions**

Open the generated workbook and assert its executive summary includes:

- Assessment readiness: `Draft review required`, `Incomplete`, or `Customer ready`.
- Assessor recommendation.
- Recommendation rationale.
- Unresolved blockers and advisories.
- Native remediation status and affected count.
- OCVS/Hybrid pricing completeness.
- `Draft` in the workbook title or status cell when not customer-ready.

Also assert existing price, discount, sizing, and scenario formula checks remain unchanged.

- [x] **Step 2: Run regression and confirm workbook metadata checks fail**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: the first readiness workbook assertion reports `FAIL`.

- [x] **Step 3: Pass readiness into workbook generation**

Add a required `readiness` argument and optional recommendation fields to `build_migration_price_workbook_xlsx()`. Render readiness metadata in the executive summary and warning section. Do not change calculation cells or shape-price formulas.

- [x] **Step 4: Distinguish draft and customer-ready exports**

The HTTP action and workbook use `Export Draft`/`Draft` unless `readiness["customer_ready_export"]` is true. Draft export remains available even when scenarios are incomplete.

- [x] **Step 5: Run workbook and full regression checks**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: all checks pass.

- [x] **Step 6: Commit**

```bash
git add app.py tests/regression_check.py
git commit -m "feat: include assessment readiness in Excel"
```

## Task 12: Responsive and Accessibility Hardening

**Files:**
- Modify: `static/css/workspace.css`
- Modify: `static/css/inventory-review.css`
- Modify: `static/css/scenarios.css`
- Modify: `static/css/results.css`
- Modify: `static/js/workspace.js`
- Modify: `static/js/inventory-review.js`
- Modify: `static/js/scenario-editor.js`
- Modify: `templates/base.html`
- Modify: `templates/_inventory_table.html`
- Modify: `templates/_scenario_native.html`
- Modify: `templates/_results_comparison.html`
- Modify: `tests/regression_check.py`

- [x] **Step 1: Add automated markup checks**

Assert:

- Every rendered form control has a `<label>`, wrapping label, `aria-label`, or `aria-labelledby`.
- Sort buttons carry `aria-sort` on their column headers.
- Tabs follow the tablist/tab/tabpanel relationship.
- Flash and dirty-state messages have live regions.
- No duplicate IDs exist on each rendered stage.
- Every sticky mobile action bar reserves matching bottom padding in the main content.

- [x] **Step 2: Run regression and fix markup failures**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: all automated checks pass before browser verification.

- [x] **Step 3: Verify all stages in the in-app browser**

Use the `browser:control-in-app-browser` skill. Start the app on a free localhost port and inspect these widths for all four stages:

```text
390 x 844
768 x 1024
1280 x 800
1440 x 900
```

For each viewport verify:

- `document.documentElement.scrollWidth === document.documentElement.clientWidth`.
- Header, rail/stage selector, tables, sticky actions, and dialogs do not overlap.
- Long assessment name, customer name, filename, and warning text do not escape containers.
- Native and inventory tables scroll only inside their explicit containers.
- The primary action remains visible and reachable.
- Mobile details editors do not duplicate submitted controls.

- [x] **Step 4: Complete keyboard-only workflow verification**

Using the browser, complete Setup, Inventory Review, Scenario tabs, recommendation, and export without pointer clicks. Verify focus order, visible focus, Escape behavior, tab arrow keys, native checkbox behavior, sortable headers, Undo, dialogs/drawers, and error-summary focus.

- [x] **Step 5: Inspect contrast and status semantics**

Check normal, hover, active, disabled, warning, error, success, and focus states. Verify scenario tabs are not overridden by global button CSS. Use text/icon labels in addition to green, amber, and red.

- [x] **Step 6: Capture final screenshots**

Save representative Stage 1, Stage 2, Stage 3 Native, and Stage 4 screenshots at desktop and 390px under `artifacts/gui-review/`. Inspect each image before accepting it. Do not commit screenshots unless the repository already tracks review artifacts.

- [x] **Step 7: Run final frontend regression after fixes**

Run:

```bash
./.venv/bin/python tests/regression_check.py
```

Expected: all checks pass.

- [x] **Step 8: Commit**

```bash
git add static templates tests/regression_check.py
git commit -m "fix: harden workspace accessibility and responsive layout"
```

## Task 13: Documentation and Full Verification

**Files:**
- Modify: `readme.MD`
- Modify: `docs/superpowers/plans/2026-07-03-guided-assessment-workspace.md`

- [x] **Step 1: Update user documentation**

Replace the five-step legacy workflow with:

1. Setup & Inventory.
2. Inventory Review.
3. Scenario Configuration.
4. Results & Export.

Document manual summary editing, warning acknowledgment, Native remediation behavior, VCF pricing completeness, local saved assessments, portable JSON import/export, assessor recommendation, draft/customer-ready Excel, and the continued exclusion of Word proposal generation.

- [x] **Step 2: Run the complete automated verification suite**

Run:

```bash
./.venv/bin/python -m compileall app.py assessment_readiness.py assessment_portability.py
./.venv/bin/python -m unittest tests.test_assessment_readiness tests.test_assessment_portability -v
./.venv/bin/python tests/regression_check.py
```

Expected:

- Compileall exits 0.
- All unit tests report `OK`.
- Regression output ends with `All regression checks passed.`

- [x] **Step 3: Run a clean-server smoke test**

Stop the old local process, start `app.py` on a free port, and request:

```text
/
/step3
/step4?tab=native
/step4?tab=ocvs
/step4?tab=hybrid
/step4?tab=price
/scenario/native
/scenario/ocvs
/scenario/hybrid
```

Expected: configured routes return 200; prerequisite routes redirect to Setup with a readable message when no assessment is loaded; aliases reach the correct stage.

- [x] **Step 4: Re-run browser acceptance on the clean server**

Verify the saved assessment library, JSON export/import, Stage 2 warning filtering, Native eligibility with unsupported VMs, VCF blocker, recommendation persistence, and draft/customer-ready Excel labels.

- [x] **Step 5: Review spec coverage and remove temporary markers**

Run:

```bash
rg -n "TODO|FIXME|TBD|NOT_IMPLEMENTED_YET|Available after portable export is enabled" app.py assessment_readiness.py assessment_portability.py templates static tests readme.MD
git diff --check
git status --short
```

Expected: no temporary implementation markers, no whitespace errors, and only the intended documentation changes remain unstaged.

- [x] **Step 6: Commit documentation**

```bash
git add readme.MD docs/superpowers/plans/2026-07-03-guided-assessment-workspace.md
git commit -m "docs: describe guided assessment workflow"
```

- [x] **Step 7: Confirm the repository is clean**

Run:

```bash
git status --short --branch
```

Expected: no modified or untracked files. The local branch may be ahead of `origin/main`; do not push or create a PR without a new explicit user request.

## Final Acceptance Checklist

- [x] Four stages use one persistent Redwood shell and consistent `Step N of 4` progress.
- [x] Setup keeps assessment name and customer/project name distinct.
- [x] Upload and manual inventory modes are clear, editable, and transactional.
- [x] Stage 2 uses one accessible inventory list with warning filtering, placement, bulk actions, and Undo.
- [x] Native remains eligible and rankable with unsupported VMs, while remediation stays visible.
- [x] Native customer-ready recommendation requires warning review and a treatment rationale.
- [x] OCVS and Hybrid are excluded from normal ranking when required VCF pricing is missing.
- [x] Scenario configuration has only Native, OCVS, and Hybrid tabs with saved/dirty state.
- [x] Results separate technical eligibility, pricing completeness, and modeled cost.
- [x] Lowest complete modeled price is not presented as an automatic recommendation.
- [x] Recommendation and rationale persist through local saves and portable JSON.
- [x] Excel clearly distinguishes draft and customer-ready output.
- [x] JSON export is self-contained; import is validated, transactional, and creates a new local copy.
- [x] Existing routes, saved assessments, manual sizing, discounts, calculations, and workbook invariants remain compatible.
- [x] No page-level overflow or incoherent overlap at 390px, 768px, 1280px, or 1440px.
- [x] Core workflow is keyboard operable and all controls have accessible names.
- [x] Automated and browser verification pass from a clean server.
- [x] Worktree is clean, with no PR or push performed.
