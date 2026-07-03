# OCVS Term Discounts and Manual Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OCVS term-based shape discounts and manual summary-based synthetic inventory input.

**Architecture:** Keep the existing VM-row model as the system boundary. Manual sizing writes generated inventory CSV rows that the current parser, selection, costing, Hybrid, and Excel paths can reuse. OCVS term discounts are normalized into app state and applied inside `build_ocvs_price_summary` after the existing IaaS host discount.

**Tech Stack:** Python Flask, Jinja templates, local JSON/CSV files, custom XLSX generation, regression script in `tests/regression_check.py`.

---

### Task 1: Failing Regression Coverage

**Files:**
- Modify: `tests/regression_check.py`

- [ ] **Step 1: Add tests for manual sizing and OCVS term discounts**

Add assertions that post a valid manual sizing form to `/`, verify the generated inventory totals and selected VM count, post invalid manual counts, and compare OCVS pay-as-you-go, 1-year, and 3-year costs through the Flask client.

- [ ] **Step 2: Run regression to verify failure**

Run: `.venv/bin/python tests/regression_check.py`
Expected: FAIL because `manual_sizing` and `ocvs_commitment_term` actions/fields do not exist yet.

### Task 2: OCVS Discount Model

**Files:**
- Modify: `app.py`
- Create: `config/ocvs_term_discounts.json`

- [ ] **Step 1: Add constants and normalization**

Add supported OCVS term values, display labels, a default JSON discount table, and helpers to load/normalize terms and retrieve discounts by shape.

- [ ] **Step 2: Apply discounts in OCVS pricing**

Thread `ocvs_commitment_term` into `build_ocvs_price_summary` and `build_price_analysis_from_rows`. Apply the term discount only to OCVS host monthly cost and include `commitment_term`, `commitment_label`, and `commitment_discount_pct` in selected/profile rows.

- [ ] **Step 3: Persist term selection**

Add `step4_ocvs_commitment_term` to default app state, restoration, POST parsing, save state, and snapshot payloads.

- [ ] **Step 4: Run regression**

Run: `.venv/bin/python tests/regression_check.py`
Expected: manual sizing tests may still fail; OCVS term discount tests should pass.

### Task 3: Manual Sizing Input

**Files:**
- Modify: `app.py`
- Modify: `templates/index.html`

- [ ] **Step 1: Add manual sizing helpers**

Add helpers to validate manual counts, distribute integer totals across generated VMs, write `rvtools/manual/manual_inventory_<timestamp>.csv`, and select all generated VMs in app state.

- [ ] **Step 2: Add Step 1 form and POST action**

Add a Manual Workload Summary section with numeric inputs. On valid submit, generate the synthetic inventory, select it, clear rejected inventory state, reset Step 4 snapshot, and flash success. On invalid submit, keep the existing assessment untouched and flash an error.

- [ ] **Step 3: Run regression**

Run: `.venv/bin/python tests/regression_check.py`
Expected: all manual sizing and OCVS term tests pass or reveal template/export gaps.

### Task 4: UI and Excel Export Surfacing

**Files:**
- Modify: `templates/step4.html`
- Modify: `app.py`
- Modify: `tests/regression_check.py`

- [ ] **Step 1: Add OCVS term controls**

Add term selectors to the OCVS and Hybrid OCVS controls, using existing `data-sync-control` behavior so both panels stay in sync.

- [ ] **Step 2: Include term assumptions in Excel**

Add selected OCVS term and discount percentage to OCVS analysis, price list/assumption rows, and technical details.

- [ ] **Step 3: Verify regression**

Run: `.venv/bin/python tests/regression_check.py`
Expected: PASS with `REGRESSION_OK`.

### Task 5: Final Verification

**Files:**
- Read: `git status --short`

- [ ] **Step 1: Run full regression**

Run: `.venv/bin/python tests/regression_check.py`
Expected: PASS with `REGRESSION_OK`.

- [ ] **Step 2: Inspect git status**

Run: `git status --short`
Expected: Only intended feature files plus the pre-existing deleted screenshot are shown.
