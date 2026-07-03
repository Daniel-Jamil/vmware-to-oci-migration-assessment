from __future__ import annotations

import math
from typing import Any, Mapping


VALID_RECOMMENDATIONS = {"", "native", "ocvs", "hybrid"}
CRITICAL_INVENTORY_ISSUES = {"missing-storage", "missing-cpu", "missing-memory"}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_collection(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [text for item in value if (text := _string(item))]


def _issue_collection(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if not isinstance(value, list):
        return []
    return [issue for issue in value if isinstance(issue, Mapping)]


def _finite_monthly_cost(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _normalize_issue(issue: Mapping[str, Any]) -> dict[str, Any]:
    issue_id = _string(issue.get("id"))
    affected_vm_names = issue.get("affected_vm_names")
    if affected_vm_names is None:
        affected_vm_names = issue.get("vm_names")
    return {
        "id": issue_id,
        "title": _string(issue.get("title")) or issue_id.replace("-", " ").title(),
        "detail": _string(issue.get("detail")),
        "stage": _string(issue.get("stage")) or "inventory",
        "affected_vm_names": _string_collection(affected_vm_names),
        "severity": _string(issue.get("severity")).lower() or "advisory",
    }


def build_assessment_readiness(context: Mapping[str, Any]) -> dict[str, Any]:
    setup = _mapping(context.get("setup"))
    inventory = _mapping(context.get("inventory"))
    scenario_inputs = _mapping(context.get("scenarios"))
    recommendation = _string(context.get("recommendation")).lower()
    if recommendation not in VALID_RECOMMENDATIONS:
        recommendation = ""
    rationale = _string(context.get("recommendation_rationale"))

    issues = [
        _normalize_issue(issue)
        for issue in _issue_collection(inventory.get("issues"))
    ]
    acknowledged = set(
        _string_collection(inventory.get("acknowledged_warning_ids"))
    )
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
        source = _mapping(scenario_inputs.get(scenario_id))
        eligible = source.get("technically_eligible") is True
        monthly_cost = _finite_monthly_cost(source.get("monthly_cost"))
        pricing_complete = (
            source.get("pricing_complete") is True and monthly_cost is not None
        )
        unsupported = _string_collection(source.get("unsupported_vm_names"))
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
            "customer_ready": False,
            "monthly_cost": monthly_cost,
        }

    ranked = [
        (values["monthly_cost"], scenario_id)
        for scenario_id, values in scenario_results.items()
        if values["rankable"]
    ]
    lowest_complete = min(ranked)[1] if ranked else ""

    setup_ready = all(
        (
            _string(setup.get("assessment_name")),
            _string(setup.get("customer_name")),
            setup.get("has_price_list") is True,
            setup.get("has_inventory") is True,
        )
    )
    included = _string_collection(inventory.get("included_vm_names"))
    placements = _mapping(inventory.get("placements"))
    inventory_ready = bool(included) and not critical and not unacknowledged and all(
        placements.get(name) in {"native", "ocvs", "review"} for name in included
    )
    unsaved_value = context.get("has_unsaved_scenario_changes")
    scenarios_saved = isinstance(unsaved_value, bool) and not unsaved_value
    scenarios_complete = scenarios_saved and any(
        scenario["rankable"] for scenario in scenario_results.values()
    )

    prerequisites_ready = setup_ready and inventory_ready and scenarios_complete
    selected = scenario_results.get(recommendation)
    native_treatment_ready = not (
        recommendation == "native"
        and selected
        and selected["remediation_required"]
        and ("unsupported-native" not in acknowledged or not rationale)
    )
    for scenario in scenario_results.values():
        scenario["customer_ready"] = bool(
            prerequisites_ready
            and scenario["rankable"]
            and not scenario["remediation_required"]
        )
    if selected:
        selected["customer_ready"] = bool(
            prerequisites_ready
            and selected["rankable"]
            and native_treatment_ready
        )
    customer_ready = bool(selected and selected["customer_ready"])
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
