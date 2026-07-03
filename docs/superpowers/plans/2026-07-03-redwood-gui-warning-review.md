# Redwood GUI And Warning Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the approved green-forward Oracle Redwood-aligned GUI direction and make inventory warnings actionable with affected VM review.

**Architecture:** Keep the existing Flask/Jinja structure and add a shared Redwood theme include instead of replacing the app with a new frontend. Step 1 gets the structural setup-first shell with left navigation, main setup content, and right warning/action rail. Inventory warning details are generated in `app.py` from loaded VM rows and rendered in Step 1.

**Tech Stack:** Flask, Jinja templates, embedded CSS, existing regression script.

---

### Task 1: Regression Coverage

**Files:**
- Modify: `tests/regression_check.py`

- [x] **Step 1: Add failing GUI assertions**

Validate the Step 1 page contains the Redwood shell markers: `redwood-app-shell`, `ORACLE`, and `Setup & Inventory`.

- [x] **Step 2: Add failing warning review assertions**

After creating manual sizing with one unsupported VM, validate the page renders `Warning Review`, `Unsupported for OCI Native`, `manual-vm-006`, `Solaris 11.4`, and `Set OCVS`.

- [x] **Step 3: Run regression and confirm failure**

Run: `.venv/bin/python tests/regression_check.py`

Expected initial failure: `redwood setup shell renders failed`.

### Task 2: Warning Review Backend

**Files:**
- Modify: `app.py`

- [x] **Step 1: Add inventory issue helpers**

Add helpers that build issue groups for unsupported OCI Native rows, missing storage, missing CPU, missing RAM, unknown OS, and duplicate VM names.

- [x] **Step 2: Pass issue context to Step 1**

Pass `inventory_review_issues` into every `index.html` render path.

### Task 3: Redwood UI Layer

**Files:**
- Modify: `.gitignore`
- Create: `templates/_redwood_theme.html`
- Modify: `templates/index.html`
- Modify: `templates/step3.html`
- Modify: `templates/step4.html`

- [x] **Step 1: Ignore brainstorm artifacts**

Add `.superpowers/` to `.gitignore`.

- [x] **Step 2: Add shared Redwood theme include**

Create a shared Jinja include with warm neutral surfaces, green/teal workflow color, restrained Oracle red, amber warnings, shell layout, action rail, cards, status chips, and warning review table styles.

- [x] **Step 3: Apply Step 1 shell**

Wrap Step 1 in a `redwood-app-shell` with left navigation, setup-first main content, and right warning/action rail.

- [x] **Step 4: Render warning review tables**

Render affected VM rows for each issue group below the selected inventory quality summary and link right-rail warning cards to the matching review section.

- [x] **Step 5: Apply theme to Step 2 and Migration Paths**

Include the shared Redwood theme and body class in `step3.html` and `step4.html`.

### Task 4: Verification

**Files:**
- Verify: `app.py`
- Verify: `templates/*.html`
- Verify: `tests/regression_check.py`

- [x] **Step 1: Run regression**

Run: `.venv/bin/python tests/regression_check.py`

- [x] **Step 2: Run compile check**

Run: `env PYTHONPYCACHEPREFIX=/private/tmp/vmware_to_oci_pycache .venv/bin/python -m py_compile app.py tests/regression_check.py`

- [x] **Step 3: Smoke check local server**

Restart the local Flask server and confirm Step 1 renders the Redwood shell and warning review.
