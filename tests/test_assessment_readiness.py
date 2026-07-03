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

    def test_customer_ready_requires_all_prerequisite_stages(self) -> None:
        cases = (
            ("setup", lambda context: context["setup"].update(assessment_name="")),
            (
                "inventory",
                lambda context: context["inventory"]["placements"].update(
                    {"app-01": "invalid"}
                ),
            ),
            (
                "scenarios",
                lambda context: context.update(has_unsaved_scenario_changes=True),
            ),
        )
        for stage_id, make_incomplete in cases:
            with self.subTest(stage=stage_id):
                context = complete_context()
                context["recommendation"] = "ocvs"
                make_incomplete(context)

                result = build_assessment_readiness(context)

                self.assertEqual(
                    "needs_attention", result["stages"][stage_id]["state"]
                )
                self.assertEqual("incomplete", result["overall_state"])
                self.assertFalse(result["customer_ready_export"])
                self.assertFalse(result["scenarios"]["ocvs"]["customer_ready"])

    def test_boolean_readiness_inputs_require_actual_booleans(self) -> None:
        context = complete_context()
        context["recommendation"] = "ocvs"
        context["scenarios"]["ocvs"]["technically_eligible"] = "false"
        context["scenarios"]["ocvs"]["pricing_complete"] = "false"

        result = build_assessment_readiness(context)
        ocvs = result["scenarios"]["ocvs"]

        self.assertEqual("ineligible", ocvs["technical_eligibility"])
        self.assertEqual("incomplete", ocvs["pricing_state"])
        self.assertFalse(ocvs["rankable"])
        self.assertFalse(result["customer_ready_export"])

        context = complete_context()
        context["recommendation"] = "ocvs"
        context["setup"]["has_price_list"] = "true"
        setup_result = build_assessment_readiness(context)
        self.assertEqual("needs_attention", setup_result["stages"]["setup"]["state"])
        self.assertFalse(setup_result["customer_ready_export"])

        context = complete_context()
        context["recommendation"] = "ocvs"
        context["has_unsaved_scenario_changes"] = "false"
        unsaved_result = build_assessment_readiness(context)
        self.assertEqual(
            "needs_attention", unsaved_result["stages"]["scenarios"]["state"]
        )
        self.assertFalse(unsaved_result["customer_ready_export"])

    def test_names_recommendation_and_rationale_require_actual_strings(self) -> None:
        for field, value in (
            ("assessment_name", {"value": "Customer migration"}),
            ("customer_name", ["Example Customer"]),
        ):
            with self.subTest(setup_field=field):
                context = complete_context()
                context["recommendation"] = "ocvs"
                context["setup"][field] = value

                result = build_assessment_readiness(context)

                self.assertEqual(
                    "needs_attention", result["stages"]["setup"]["state"]
                )
                self.assertEqual("incomplete", result["overall_state"])
                self.assertFalse(result["customer_ready_export"])

        context = complete_context()
        context["recommendation"] = ["ocvs"]
        recommendation_result = build_assessment_readiness(context)
        self.assertEqual("draft_review_required", recommendation_result["overall_state"])
        self.assertFalse(recommendation_result["customer_ready_export"])

        for rationale in ({"treatment": "Remediate legacy-01"}, ["Remediate legacy-01"]):
            with self.subTest(rationale_type=type(rationale).__name__):
                context = complete_context()
                context["recommendation"] = "native"
                context["recommendation_rationale"] = rationale

                result = build_assessment_readiness(context)

                self.assertEqual("draft_review_required", result["overall_state"])
                self.assertFalse(result["customer_ready_export"])
                self.assertFalse(result["scenarios"]["native"]["customer_ready"])

    def test_scalar_nested_mappings_fail_closed_without_crashing(self) -> None:
        context = complete_context()
        context["setup"] = "invalid"
        setup_result = build_assessment_readiness(context)
        self.assertEqual("needs_attention", setup_result["stages"]["setup"]["state"])

        context = complete_context()
        context["inventory"] = 17
        inventory_result = build_assessment_readiness(context)
        self.assertEqual(
            "needs_attention", inventory_result["stages"]["inventory"]["state"]
        )

        context = complete_context()
        context["scenarios"] = "invalid"
        scenarios_result = build_assessment_readiness(context)
        self.assertEqual(
            "needs_attention", scenarios_result["stages"]["scenarios"]["state"]
        )

        context = complete_context()
        context["scenarios"]["native"] = 17
        scenario_result = build_assessment_readiness(context)
        self.assertEqual("incomplete", scenario_result["scenarios"]["native"]["state"])

    def test_scalar_string_collections_fail_closed_without_crashing(self) -> None:
        context = complete_context()
        context["recommendation"] = "ocvs"
        context["inventory"]["included_vm_names"] = 17
        included_result = build_assessment_readiness(context)
        self.assertEqual(
            "needs_attention", included_result["stages"]["inventory"]["state"]
        )
        self.assertFalse(included_result["customer_ready_export"])

        context = complete_context()
        context["recommendation"] = "ocvs"
        context["inventory"]["placements"] = "native"
        placements_result = build_assessment_readiness(context)
        self.assertEqual(
            "needs_attention", placements_result["stages"]["inventory"]["state"]
        )
        self.assertFalse(placements_result["customer_ready_export"])

        context = complete_context()
        context["recommendation"] = "native"
        context["recommendation_rationale"] = "Treatment documented."
        context["scenarios"]["native"]["unsupported_vm_names"] = 17
        unsupported_result = build_assessment_readiness(context)
        self.assertEqual(
            [], unsupported_result["scenarios"]["native"]["affected_vm_names"]
        )
        self.assertEqual(
            "needs_attention", unsupported_result["scenarios"]["native"]["state"]
        )
        self.assertTrue(unsupported_result["scenarios"]["native"]["rankable"])
        self.assertFalse(unsupported_result["customer_ready_export"])
        self.assertIn(
            "invalid-native-unsupported-vms",
            {item["id"] for item in unsupported_result["advisory_items"]},
        )

        context = complete_context()
        context["recommendation"] = "native"
        context["recommendation_rationale"] = "Treatment documented."
        context["inventory"]["acknowledged_warning_ids"] = "unsupported-native"
        acknowledged_result = build_assessment_readiness(context)
        self.assertEqual(
            "complete", acknowledged_result["stages"]["inventory"]["state"]
        )
        self.assertTrue(acknowledged_result["customer_ready_export"])

    def test_scalar_unsupported_vm_name_preserves_native_remediation(self) -> None:
        context = complete_context()
        context["recommendation"] = "native"
        context["scenarios"]["native"]["unsupported_vm_names"] = "legacy-01"

        result = build_assessment_readiness(context)
        native = result["scenarios"]["native"]

        self.assertEqual(["legacy-01"], native["affected_vm_names"])
        self.assertTrue(native["remediation_required"])
        self.assertTrue(native["rankable"])
        self.assertEqual("needs_attention", native["state"])
        self.assertFalse(result["customer_ready_export"])

    def test_malformed_unsupported_vm_collections_deny_native_export(self) -> None:
        malformed_values = (
            17,
            True,
            {"vm": "legacy-01"},
            ["legacy-01", 17],
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed):
                context = complete_context()
                context["recommendation"] = "native"
                context["recommendation_rationale"] = "Treatment documented."
                context["scenarios"]["native"]["unsupported_vm_names"] = malformed

                result = build_assessment_readiness(context)
                native = result["scenarios"]["native"]

                self.assertTrue(native["rankable"])
                self.assertEqual("needs_attention", native["state"])
                self.assertFalse(native["customer_ready"])
                self.assertFalse(result["customer_ready_export"])
                self.assertIn(
                    "invalid-native-unsupported-vms",
                    {item["id"] for item in result["advisory_items"]},
                )

    def test_malformed_warning_id_collections_deny_customer_ready_export(self) -> None:
        malformed_values = (
            17,
            True,
            {"id": "unsupported-native"},
            ["unsupported-native", 17],
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed):
                context = complete_context()
                context["recommendation"] = "ocvs"
                context["inventory"]["acknowledged_warning_ids"] = malformed

                result = build_assessment_readiness(context)

                self.assertEqual(
                    "needs_attention", result["stages"]["inventory"]["state"]
                )
                self.assertFalse(result["customer_ready_export"])

    def test_single_issue_mapping_is_processed(self) -> None:
        context = complete_context()
        context["recommendation"] = "ocvs"
        context["inventory"]["issues"] = {
            "id": "missing-storage",
            "severity": "critical",
            "vm_names": ["app-01"],
        }

        result = build_assessment_readiness(context)

        self.assertEqual("needs_attention", result["stages"]["inventory"]["state"])
        self.assertEqual("missing-storage", result["blocking_items"][0]["id"])
        self.assertEqual("incomplete", result["overall_state"])
        self.assertFalse(result["customer_ready_export"])

    def test_malformed_issue_collections_add_integrity_blocker(self) -> None:
        malformed_values = (
            17,
            True,
            "missing-storage",
            [
                {
                    "id": "unsupported-native",
                    "severity": "advisory",
                    "vm_names": ["legacy-01"],
                },
                17,
            ],
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed):
                context = complete_context()
                context["recommendation"] = "ocvs"
                context["inventory"]["issues"] = malformed

                result = build_assessment_readiness(context)
                blockers = {item["id"]: item for item in result["blocking_items"]}

                self.assertIn("invalid-inventory-issues", blockers)
                self.assertEqual(
                    "critical", blockers["invalid-inventory-issues"]["severity"]
                )
                self.assertEqual(
                    "inventory", blockers["invalid-inventory-issues"]["stage"]
                )
                self.assertEqual(
                    "needs_attention", result["stages"]["inventory"]["state"]
                )
                self.assertFalse(result["customer_ready_export"])

    def test_none_or_missing_issue_collection_is_validly_empty(self) -> None:
        for issues_state in ("none", "missing"):
            with self.subTest(issues_state=issues_state):
                context = complete_context()
                context["recommendation"] = "ocvs"
                if issues_state == "none":
                    context["inventory"]["issues"] = None
                else:
                    context["inventory"].pop("issues")

                result = build_assessment_readiness(context)

                self.assertEqual("complete", result["stages"]["inventory"]["state"])
                self.assertEqual([], result["blocking_items"])
                self.assertTrue(result["customer_ready_export"])

    def test_monthly_cost_requires_a_finite_non_boolean_number(self) -> None:
        invalid_costs = ("100.0", True, float("nan"), float("inf"), float("-inf"))
        for monthly_cost in invalid_costs:
            with self.subTest(monthly_cost=monthly_cost):
                context = complete_context()
                context["scenarios"]["ocvs"]["monthly_cost"] = monthly_cost

                result = build_assessment_readiness(context)
                ocvs = result["scenarios"]["ocvs"]

                self.assertEqual("incomplete", ocvs["pricing_state"])
                self.assertFalse(ocvs["rankable"])
                self.assertIsNone(ocvs["monthly_cost"])

        context = complete_context()
        context["scenarios"]["ocvs"]["monthly_cost"] = 10**400

        large_integer_result = build_assessment_readiness(context)

        self.assertTrue(large_integer_result["scenarios"]["ocvs"]["rankable"])
        self.assertEqual(
            10**400, large_integer_result["scenarios"]["ocvs"]["monthly_cost"]
        )

    def test_missing_scenario_keys_are_safe_and_incomplete(self) -> None:
        context = complete_context()
        context["scenarios"] = {}

        result = build_assessment_readiness(context)

        self.assertEqual("", result["lowest_complete_scenario"])
        self.assertEqual("needs_attention", result["stages"]["scenarios"]["state"])
        for scenario_id in ("native", "ocvs", "hybrid"):
            self.assertEqual("incomplete", result["scenarios"][scenario_id]["state"])
            self.assertFalse(result["scenarios"][scenario_id]["rankable"])


if __name__ == "__main__":
    unittest.main()
