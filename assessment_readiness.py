from __future__ import annotations

import math
from typing import Any, Mapping


VALID_RECOMMENDATIONS = {"", "native", "ocvs", "hybrid"}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_collection(value: Any) -> tuple[list[str], bool]:
    if value is None:
        return [], True
    if isinstance(value, str):
        text = value.strip()
        return ([text] if text else []), True
    if not isinstance(value, (list, tuple, set, frozenset)):
        return [], False

    normalized: list[str] = []
    valid = True
    for item in value:
        if not isinstance(item, str):
            valid = False
            continue
        text = item.strip()
        if text:
            normalized.append(text)
    return normalized, valid


def _issue_collection(value: Any) -> tuple[list[Mapping[str, Any]], bool]:
    if value is None:
        return [], True
    if isinstance(value, Mapping):
        return [value], True
    if not isinstance(value, list):
        return [], False
    return (
        [issue for issue in value if isinstance(issue, Mapping)],
        all(isinstance(issue, Mapping) for issue in value),
    )


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
    normalized_vm_names, _ = _string_collection(affected_vm_names)
    return {
        "id": issue_id,
        "title": _string(issue.get("title")) or issue_id.replace("-", " ").title(),
        "detail": _string(issue.get("detail")),
        "stage": _string(issue.get("stage")) or "inventory",
        "affected_vm_names": normalized_vm_names,
        "severity": _string(issue.get("severity")).lower() or "advisory",
        "acknowledged": False,
    }


def _integrity_issue(
    issue_id: str,
    title: str,
    detail: str,
    *,
    stage: str = "inventory",
    severity: str = "critical",
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "title": title,
        "detail": detail,
        "stage": stage,
        "affected_vm_names": [],
        "severity": severity,
        "acknowledged": False,
    }


def build_assessment_readiness(context: Mapping[str, Any]) -> dict[str, Any]:
    setup = _mapping(context.get("setup"))
    inventory = _mapping(context.get("inventory"))
    scenario_inputs = _mapping(context.get("scenarios"))
    recommendation = _string(context.get("recommendation")).lower()
    if recommendation not in VALID_RECOMMENDATIONS:
        recommendation = ""
    rationale = _string(context.get("recommendation_rationale"))

    issue_values, issues_valid = _issue_collection(inventory.get("issues"))
    included, included_valid = _string_collection(inventory.get("included_vm_names"))
    acknowledged_values, acknowledged_valid = _string_collection(
        inventory.get("acknowledged_warning_ids")
    )
    acknowledged = set(acknowledged_values)
    issues = [_normalize_issue(issue) for issue in issue_values]
    if not issues_valid:
        issues.append(
            _integrity_issue(
                "invalid-inventory-issues",
                "Invalid inventory issue data",
                "Inventory issues must be a mapping or a list containing only mappings.",
            )
        )
    if not included_valid:
        issues.append(
            _integrity_issue(
                "invalid-included-vm-names",
                "Invalid included VM names",
                "Included VM names must be a string or a collection of strings.",
            )
        )
    if not acknowledged_valid:
        issues.append(
            _integrity_issue(
                "invalid-acknowledged-warning-ids",
                "Invalid warning acknowledgments",
                "Warning IDs must be a string or a collection of strings.",
            )
        )
    for issue in issues:
        issue["acknowledged"] = issue["id"] in acknowledged
    critical = [
        issue
        for issue in issues
        if issue["severity"] == "critical"
    ]
    critical_ids = {id(issue) for issue in critical}
    unacknowledged = [
        issue
        for issue in issues
        if id(issue) not in critical_ids and issue["id"] not in acknowledged
    ]
    inventory_advisories = [
        issue for issue in issues if id(issue) not in critical_ids
    ]

    scenario_issue_values, scenario_issues_valid = _issue_collection(
        context.get("scenario_issues")
    )
    scenario_issues = [_normalize_issue(issue) for issue in scenario_issue_values]
    for issue in scenario_issues:
        issue["stage"] = "scenarios"
    if not scenario_issues_valid:
        scenario_issues.append(
            _integrity_issue(
                "invalid-scenario-issues",
                "Invalid scenario issue data",
                "Scenario issues must be a mapping or a list containing only mappings.",
                stage="scenarios",
            )
        )
    scenario_blockers = [
        issue for issue in scenario_issues if issue["severity"] == "critical"
    ]
    scenario_blocker_ids = {id(issue) for issue in scenario_blockers}
    scenario_advisories = [
        issue for issue in scenario_issues if id(issue) not in scenario_blocker_ids
    ]

    scenario_results: dict[str, dict[str, Any]] = {}
    scenario_integrity_items: list[dict[str, Any]] = []
    for scenario_id in ("native", "ocvs", "hybrid"):
        source = _mapping(scenario_inputs.get(scenario_id))
        eligible = source.get("technically_eligible") is True
        monthly_cost = _finite_monthly_cost(source.get("monthly_cost"))
        pricing_complete = (
            source.get("pricing_complete") is True and monthly_cost is not None
        )
        unsupported, unsupported_valid = _string_collection(
            source.get("unsupported_vm_names")
        )
        unsupported_corrupt = scenario_id == "native" and not unsupported_valid
        if unsupported_corrupt:
            scenario_integrity_items.append(
                _integrity_issue(
                    "invalid-native-unsupported-vms",
                    "Invalid Native compatibility data",
                    "Native unsupported VM names must be a string or a collection of strings.",
                    stage="scenarios",
                    severity="advisory",
                )
            )
        remediation_required = scenario_id == "native" and bool(unsupported)
        rankable = eligible and pricing_complete
        state = (
            "incomplete"
            if not rankable
            else "needs_attention"
            if remediation_required or unsupported_corrupt
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
    placements = _mapping(inventory.get("placements"))
    inventory_ready = (
        bool(included)
        and included_valid
        and acknowledged_valid
        and not critical
        and all(
            placements.get(name) in {"native", "ocvs", "review"}
            for name in included
        )
    )
    unsaved_value = context.get("has_unsaved_scenario_changes")
    scenarios_saved = isinstance(unsaved_value, bool) and not unsaved_value
    scenarios_complete = (
        scenarios_saved
        and not scenario_blockers
        and any(scenario["rankable"] for scenario in scenario_results.values())
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
            and not scenario_integrity_items
        )
    if selected:
        selected["customer_ready"] = bool(
            prerequisites_ready
            and selected["rankable"]
            and native_treatment_ready
            and not scenario_integrity_items
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
                "blockers": scenario_blockers,
                "advisories": scenario_advisories + scenario_integrity_items,
            },
            "results": {
                "state": "complete" if customer_ready else "needs_attention",
                "blockers": [],
                "advisories": [],
            },
        },
        "scenarios": scenario_results,
        "blocking_items": critical + scenario_blockers,
        "advisory_items": (
            unacknowledged + scenario_advisories + scenario_integrity_items
        ),
        "display_advisory_items": (
            inventory_advisories + scenario_advisories + scenario_integrity_items
        ),
        "lowest_complete_scenario": lowest_complete,
        "customer_ready_export": customer_ready,
    }
