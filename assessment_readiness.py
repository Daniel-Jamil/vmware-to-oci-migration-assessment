from __future__ import annotations

from typing import Any, Mapping


VALID_RECOMMENDATIONS = {"", "native", "ocvs", "hybrid"}
CRITICAL_INVENTORY_ISSUES = {"missing-storage", "missing-cpu", "missing-memory"}


def _normalize_issue(issue: Mapping[str, Any]) -> dict[str, Any]:
    issue_id = str(issue.get("id") or "").strip()
    affected_vm_names = issue.get("affected_vm_names")
    if affected_vm_names is None:
        affected_vm_names = issue.get("vm_names")
    return {
        "id": issue_id,
        "title": str(issue.get("title") or issue_id.replace("-", " ").title()).strip(),
        "detail": str(issue.get("detail") or "").strip(),
        "stage": str(issue.get("stage") or "inventory").strip(),
        "affected_vm_names": [str(name) for name in affected_vm_names or []],
        "severity": str(issue.get("severity") or "advisory").strip().lower(),
    }


def build_assessment_readiness(context: Mapping[str, Any]) -> dict[str, Any]:
    setup = dict(context.get("setup") or {})
    inventory = dict(context.get("inventory") or {})
    scenario_inputs = dict(context.get("scenarios") or {})
    recommendation = str(context.get("recommendation") or "").strip().lower()
    if recommendation not in VALID_RECOMMENDATIONS:
        recommendation = ""
    rationale = str(context.get("recommendation_rationale") or "").strip()

    issues = [
        _normalize_issue(issue)
        for issue in inventory.get("issues") or []
        if isinstance(issue, Mapping)
    ]
    acknowledged = {
        str(value) for value in inventory.get("acknowledged_warning_ids") or []
    }
    critical = [
        issue
        for issue in issues
        if issue["id"] in CRITICAL_INVENTORY_ISSUES
        or issue["severity"] == "critical"
    ]
    critical_ids = {id(issue) for issue in critical}
    unacknowledged = [
        issue
        for issue in issues
        if id(issue) not in critical_ids and issue["id"] not in acknowledged
    ]

    scenario_results: dict[str, dict[str, Any]] = {}
    for scenario_id in ("native", "ocvs", "hybrid"):
        source = dict(scenario_inputs.get(scenario_id) or {})
        eligible = bool(source.get("technically_eligible"))
        pricing_complete = bool(source.get("pricing_complete"))
        unsupported = [str(name) for name in source.get("unsupported_vm_names") or []]
        remediation_required = scenario_id == "native" and bool(unsupported)
        rankable = eligible and pricing_complete
        state = (
            "incomplete"
            if not rankable
            else "needs_attention"
            if remediation_required
            else "ready"
        )
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
        selected["customer_ready"] = bool(
            selected["rankable"] and native_treatment_ready
        )
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

    prerequisites_ready = setup_ready and inventory_ready and scenarios_complete
    overall = (
        "customer_ready"
        if customer_ready
        else "incomplete"
        if not prerequisites_ready
        else "draft_review_required"
    )
    return {
        "overall_state": overall,
        "stages": {
            "setup": {
                "state": "complete" if setup_ready else "needs_attention",
                "blockers": [],
                "advisories": [],
            },
            "inventory": {
                "state": "complete" if inventory_ready else "needs_attention",
                "blockers": critical,
                "advisories": unacknowledged,
            },
            "scenarios": {
                "state": "complete" if scenarios_complete else "needs_attention",
                "blockers": [],
                "advisories": [],
            },
            "results": {
                "state": "complete" if customer_ready else "needs_attention",
                "blockers": [],
                "advisories": [],
            },
        },
        "scenarios": scenario_results,
        "blocking_items": critical,
        "advisory_items": unacknowledged,
        "lowest_complete_scenario": lowest_complete,
        "customer_ready_export": customer_ready,
    }
