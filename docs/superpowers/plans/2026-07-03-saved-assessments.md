# Saved Assessments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local save, load, and delete support for complete migration assessments from Step 1.

**Architecture:** Extend the existing `downloads/app_state` JSON storage pattern with a `saved_assessments` directory. Add helper functions in `app.py` for snapshot creation, listing, loading, validation, and deletion, then add a compact Redwood-aligned Step 1 panel that posts new actions to the existing `index()` route.

**Tech Stack:** Python Flask, server-side sessions, local JSON files, Jinja templates, existing shell-based regression script.

---

### Task 1: Regression Coverage

**Files:**
- Modify: `tests/regression_check.py`

- [x] **Step 1: Write the failing test**

Add a `validate_saved_assessments()` flow that creates a manual assessment, posts `action=save_assessment`, mutates the active assessment, posts `action=load_assessment`, and asserts the saved values are restored. Also post `action=delete_assessment` and assert the saved assessment disappears from the rendered page.

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python tests/regression_check.py`

Expected: FAIL because the page has no `save_assessment`, `load_assessment`, or saved assessment UI yet.

### Task 2: Saved Assessment Helpers

**Files:**
- Modify: `app.py`

- [x] **Step 1: Implement storage helpers**

Add `SAVED_ASSESSMENT_SCHEMA_VERSION`, `_saved_assessments_dir()`, safe slug/id helpers, `list_saved_assessments()`, `build_saved_assessment_snapshot()`, `save_current_assessment()`, `load_saved_assessment()`, and `delete_saved_assessment()`.

- [x] **Step 2: Validate file dependencies on load**

When loading, restore price list only if it exists in `list_downloaded_price_lists()`. Restore inventory only if the path exists and `load_vms_from_vinfo()` succeeds. Rebuild `rvtools_import_summary` from parsed rows.

- [x] **Step 3: Run the regression test**

Run: `.venv/bin/python tests/regression_check.py`

Expected: still FAIL until the route and template can call the helpers.

### Task 3: Route and UI Integration

**Files:**
- Modify: `app.py`
- Modify: `templates/index.html`

- [x] **Step 1: Add POST actions**

Handle `save_assessment`, `load_assessment`, and `delete_assessment` in `index()`. Keep users on Step 1 after each action and flash success/warning messages.

- [x] **Step 2: Render Saved Assessments panel**

Pass `saved_assessments`, `active_assessment_name`, and `active_assessment_notes` to `index.html`. Add a right-rail panel with name/notes fields, save button, saved assessment dropdown, load button, and delete button.

- [x] **Step 3: Run the regression test**

Run: `.venv/bin/python tests/regression_check.py`

Expected: PASS.

### Task 4: Verification and Commit

**Files:**
- Verify: `app.py`
- Verify: `templates/index.html`
- Verify: `tests/regression_check.py`
- Verify: `docs/superpowers/specs/2026-07-03-saved-assessments-design.md`
- Verify: `docs/superpowers/plans/2026-07-03-saved-assessments.md`

- [x] **Step 1: Compile Python**

Run: `env PYTHONPYCACHEPREFIX=/private/tmp/vmware_to_oci_pycache .venv/bin/python -m py_compile app.py tests/regression_check.py`

Expected: exit code 0.

- [x] **Step 2: Run full regression**

Run: `.venv/bin/python tests/regression_check.py`

Expected: exit code 0.

- [x] **Step 3: Commit local work**

Run: `git add app.py templates/index.html tests/regression_check.py docs/superpowers/specs/2026-07-03-saved-assessments-design.md docs/superpowers/plans/2026-07-03-saved-assessments.md`

Run: `git commit -m "feat: add saved assessments"`

Expected: local commit created. Do not create a PR.
