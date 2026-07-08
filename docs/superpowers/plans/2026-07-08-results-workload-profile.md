# Results Workload Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the workload analytics band in Results so assessors can see workload size, OS distribution, power state, OCI Native readiness, and per-VM averages before comparing migration paths.

**Architecture:** Reuse the existing `workload_summary` object produced by `build_price_analysis_from_rows()` and already passed to `step4.html`. Add a focused `_workload_profile.html` partial rendered above `_results_comparison.html` for the Results tab, with CSS in `static/css/results.css`. Do not add new dependencies or client-side chart libraries; use accessible HTML, CSS bars, and existing backend percentages.

**Tech Stack:** Flask, Jinja templates, plain CSS, Python unittest/regression script.

## Global Constraints

- Keep the Results screen as the first customer-facing analysis surface; do not add a landing page or new navigation step.
- Use the existing Results design language, not the old monolithic Step 4 styling.
- Use existing backend workload summary data; do not duplicate inventory parsing.
- Keep charts accessible as text plus CSS bars.
- Follow TDD: write failing tests before production changes.

---

### Task 1: Add Results Workload Profile Rendering

**Files:**
- Create: `templates/_workload_profile.html`
- Modify: `templates/step4.html`
- Modify: `static/css/results.css`
- Test: `tests/test_assessment_readiness.py`
- Test: `tests/regression_check.py`

**Interfaces:**
- Consumes: `workload_summary: dict[str, Any]` from `step4()` render context.
- Produces: Rendered section with `data-workload-profile`, `data-workload-profile-os`, and text labels `Workload profile`, `Operating system mix`, `Power state`, `OCI Native readiness`, `Avg vCPU / VM`, `Avg RAM / VM`, `Avg storage / VM`.

- [ ] **Step 1: Write the failing unit test**

Add assertions to `ReadinessTests.test_results_page_treats_blank_vcf_price_as_optional_extra`:

```python
self.assertIn('data-workload-profile', html)
self.assertIn("Workload profile", html)
self.assertIn("Operating system mix", html)
self.assertIn("Power state", html)
self.assertIn("OCI Native readiness", html)
self.assertIn("Avg vCPU / VM", html)
```

- [ ] **Step 2: Write the failing regression source/render checks**

Add checks to `validate_task7_native_scenario_workspace()` for rendered Results HTML:

```python
check(
    "Task 9 Results restores workload profile analytics",
    'data-workload-profile' in price_html
    and "Operating system mix" in price_html
    and "Power state" in price_html
    and "OCI Native readiness" in price_html
    and "Avg vCPU / VM" in price_html,
)
```

Add checks to `validate_workspace_source_contracts()` for the partial and CSS:

```python
workload_profile_template = (ROOT / "templates" / "_workload_profile.html").read_text(encoding="utf-8")
check(
    "Results workload profile uses existing summary data and CSS bars",
    "workload_summary.top_os_rows" in workload_profile_template
    and "workload-profile__bar-fill" in workload_profile_template
    and ".workload-profile" in results_css
    and ".workload-profile__bar-fill" in results_css,
)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_assessment_readiness.ReadinessTests.test_results_page_treats_blank_vcf_price_as_optional_extra`

Expected: FAIL because `data-workload-profile` is not rendered yet.

- [ ] **Step 4: Add the workload profile partial**

Create `templates/_workload_profile.html` with KPI cards, OS bars, power-state stacked bar, readiness stacked bar, and average VM stats using `workload_summary`.

- [ ] **Step 5: Render the partial on Results**

In `templates/step4.html`, include `_workload_profile.html` immediately before `_results_comparison.html` when `active_scenario == 'price'`.

- [ ] **Step 6: Add Results CSS**

In `static/css/results.css`, add `.workload-profile` rules with responsive grids and stable bar dimensions.

- [ ] **Step 7: Run targeted tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_assessment_readiness.ReadinessTests.test_results_page_treats_blank_vcf_price_as_optional_extra`

Expected: PASS.

- [ ] **Step 8: Run broader verification**

Run: `.venv/bin/python -m unittest tests.test_assessment_readiness tests.test_assessment_portability`

Expected: PASS.

Run: `.venv/bin/python tests/regression_check.py`

Expected: `REGRESSION_OK`.
