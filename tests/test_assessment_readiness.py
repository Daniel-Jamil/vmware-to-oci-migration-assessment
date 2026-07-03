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

    def test_incomplete_ocvs_and_hybrid_pricing_excludes_them_from_ranking(self) -> None:
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
        context["recommendation_rationale"] = (
            "Remediate legacy-01 before its Native migration wave."
        )

        ready = build_assessment_readiness(context)

        self.assertEqual("customer_ready", ready["overall_state"])
        self.assertTrue(ready["customer_ready_export"])

        context["inventory"]["acknowledged_warning_ids"] = []
        unacknowledged = build_assessment_readiness(context)

        self.assertEqual("incomplete", unacknowledged["overall_state"])
        self.assertFalse(unacknowledged["customer_ready_export"])

        context["inventory"]["acknowledged_warning_ids"] = ["unsupported-native"]
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
