import copy
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import app as app_module
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


def current_adapter_inputs(vcf_price_per_core_yearly: float = 400.0) -> dict:
    inventory_rows = [
        {
            "name": "app-01",
            "source_name": "app-01",
            "raw_os": "Oracle Linux 8 (64-bit)",
            "cpus": 4,
            "memory_mb": 8192,
            "provisioned_mib": 102400,
        },
        {
            "name": "legacy-01",
            "source_name": "legacy-01",
            "raw_os": "Microsoft Windows Server 2008 (64-bit)",
            "cpus": 2,
            "memory_mb": 4096,
            "provisioned_mib": 51200,
        },
        {
            "name": "excluded-01",
            "source_name": "excluded-01",
            "raw_os": "Oracle Linux 8 (64-bit)",
            "cpus": 2,
            "memory_mb": 4096,
            "provisioned_mib": 51200,
        },
    ]
    modeled_vm_rows = [
        {
            "vm_name": "app-01",
            "os_name": "Oracle Linux 8 (64-bit)",
            "ocpu_unit_price": 0.03,
            "memory_unit_price": 0.002,
            "is_windows_server": False,
            "os_license": "",
        },
        {
            "vm_name": "legacy-01",
            "os_name": "Microsoft Windows Server 2008 (64-bit)",
            "ocpu_unit_price": 0.03,
            "memory_unit_price": 0.002,
            "is_windows_server": True,
            "os_license": "BYOL",
        },
    ]
    scenario_rows = [
        {"id": "native", "monthly_cost": 125.0},
        {"id": "ocvs", "monthly_cost": 825.0},
        {"id": "hybrid", "monthly_cost": 475.0},
    ]
    physical_cores = {"ocvs": 384, "hybrid": 128}
    analysis = {
        "scenario_comparison": {"rows": scenario_rows},
        "oci_unsupported_rows": [{"vm_name": "legacy-01"}],
        "supported_native_rows": [modeled_vm_rows[0]],
        "ocvs_price": {
            "selected": {
                "host_count": 3,
                "host_type": "Dense",
                "pricing_available": True,
            }
        },
        "hybrid_ocvs_price": {
            "selected": {
                "host_count": 1,
                "host_type": "Dense",
                "pricing_available": True,
            }
        },
        "vmware_license_summary": {
            "is_priced": vcf_price_per_core_yearly > 0,
            "price_per_core_yearly": vcf_price_per_core_yearly,
            "ocvs": {"physical_cores": physical_cores["ocvs"]},
            "hybrid": {"physical_cores": physical_cores["hybrid"]},
        },
        "fit_warnings": [
            {
                "severity": "warning" if vcf_price_per_core_yearly == 0 else "info",
                "title": (
                    "VCF license price not set"
                    if vcf_price_per_core_yearly == 0
                    else "VCF license cost included"
                ),
                "detail": (
                    "OCVS and Hybrid costs exclude VCF license cost until a list price per physical core is entered."
                    if vcf_price_per_core_yearly == 0
                    else "VCF license cost is included in the modeled scenarios."
                ),
            }
        ],
    }
    return {
        "inventory_rows": inventory_rows,
        "selected_vm_names": ["app-01", "legacy-01"],
        "scenario_analysis": analysis,
        "scenario_views": [
            {"id": row["id"], "scenario": dict(row)} for row in scenario_rows
        ],
        "app_state": {
            "selected_vm_names": ["app-01", "legacy-01"],
            "step4_hybrid_placements": {
                "app-01": "native",
                "legacy-01": "ocvs",
                "excluded-01": "invalid",
            },
            "acknowledged_warning_ids": ["unsupported-native"],
            "assessor_recommendation": "",
            "assessor_recommendation_rationale": "",
            "step4_vmware_license_price_per_core_yearly": vcf_price_per_core_yearly,
        },
        "setup_metadata": {
            "assessment_name": "Current assessment",
            "customer_name": "Example Customer",
            "has_price_list": True,
            "has_inventory": True,
        },
        "pricing_inputs": {
            "source_pricelist_file": "prices.json",
            "price_lookup": {"available-sku": 1.0},
            "modeled_vm_rows": modeled_vm_rows,
            "block_storage_unit_price": 0.02,
            "block_perf_unit_price": 0.001,
            "windows_os_unit_price": 0.09,
        },
        "has_unsaved_scenario_changes": False,
    }


@contextmanager
def current_step4_client():
    inventory_rows = copy.deepcopy(current_adapter_inputs()["inventory_rows"][:2])
    state = app_module._default_app_state()
    state["selected_vm_names"] = ["app-01", "legacy-01"]
    state["step4_hybrid_placements"] = {
        "app-01": "native",
        "legacy-01": "ocvs",
    }
    state["acknowledged_warning_ids"] = ["unsupported-native"]
    state["step4_vmware_license_price_per_core_yearly"] = 400.0

    price_lookup: dict[str, float] = {
        "Storage - Block Volume - Storage": 0.02,
        "Storage - Block Volume - Performance Units": 0.001,
        "Compute - Windows OS": 0.09,
    }
    for mapping in app_module.load_oci_price_mapping_details().values():
        for key in ("ocpu_display_name", "memory_display_name"):
            display_name = str(mapping.get(key) or "").strip()
            if display_name:
                price_lookup[display_name] = 0.03
    for profile in app_module.OCVS_HOST_PROFILES:
        for key in (
            "ocpu_display_name",
            "memory_display_name",
            "nvme_display_name",
        ):
            display_name = str(profile.get(key) or "").strip()
            if display_name:
                price_lookup[display_name] = 0.03

    def load_state() -> dict:
        return copy.deepcopy(state)

    def save_state(value: dict) -> None:
        state.clear()
        state.update(copy.deepcopy(value))

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                app_module,
                "load_vms_from_vinfo",
                side_effect=lambda _path: (copy.deepcopy(inventory_rows), "fixture.csv"),
            )
        )
        stack.enter_context(
            patch.object(app_module, "load_app_state", side_effect=load_state)
        )
        stack.enter_context(
            patch.object(app_module, "save_app_state", side_effect=save_state)
        )
        stack.enter_context(
            patch.object(app_module, "load_step4_snapshot", return_value={})
        )
        stack.enter_context(
            patch.object(app_module, "save_step4_snapshot", return_value=None)
        )
        stack.enter_context(
            patch.object(
                app_module,
                "load_price_lookup",
                return_value=(price_lookup, "EUR", "prices.json"),
            )
        )
        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
                sess["selected_rvtools_file"] = "fixture.csv"
                sess["selected_pricelist_file"] = "prices.json"
                sess["selected_currency"] = "EUR"
                sess["customer_name"] = "Example Customer"
                sess["active_assessment_name"] = "Current assessment"
            yield client, state


class ReadinessTests(unittest.TestCase):
    def assert_readiness_item_contract(
        self,
        item: dict,
        *,
        item_id: str,
        stage: str,
        severity: str,
        affected_vm_names: list[str],
        acknowledged: bool,
    ) -> None:
        self.assertEqual(item_id, item["id"])
        self.assertTrue(item["title"].strip())
        self.assertTrue(item["detail"].strip())
        self.assertEqual(stage, item["stage"])
        self.assertEqual(affected_vm_names, item["affected_vm_names"])
        self.assertEqual(severity, item["severity"])
        self.assertIs(acknowledged, item["acknowledged"])

    def test_current_adapter_keeps_unsupported_native_eligible_and_visible(self) -> None:
        adapter = getattr(app_module, "build_current_readiness_context", None)
        self.assertTrue(callable(adapter), "current readiness adapter is missing")

        with patch.object(
            app_module,
            "build_assessment_readiness",
            wraps=build_assessment_readiness,
        ) as readiness_builder:
            result = adapter(**current_adapter_inputs())

        native = result["scenarios"]["native"]
        self.assertEqual(1, readiness_builder.call_count)
        self.assertEqual("eligible", native["technical_eligibility"])
        self.assertEqual("needs_attention", native["state"])
        self.assertTrue(native["rankable"])
        self.assertEqual(["legacy-01"], native["affected_vm_names"])
        source_advisories = result["stages"]["inventory"]["advisories"]
        unsupported = next(
            item for item in source_advisories if item["id"] == "unsupported-native"
        )
        self.assert_readiness_item_contract(
            unsupported,
            item_id="unsupported-native",
            stage="inventory",
            severity="advisory",
            affected_vm_names=["legacy-01"],
            acknowledged=True,
        )
        self.assertEqual("complete", result["stages"]["inventory"]["state"])

    def test_invalid_step4_post_marks_only_redirected_get_unsaved(self) -> None:
        real_adapter = app_module.build_current_readiness_context
        adapter_calls: list[tuple[dict, dict]] = []

        def capture_adapter(**kwargs: object) -> dict:
            result = real_adapter(**kwargs)
            adapter_calls.append((dict(kwargs), result))
            return result

        with current_step4_client() as (client, _state), patch.object(
            app_module,
            "build_current_readiness_context",
            side_effect=capture_adapter,
        ):
            response = client.post(
                "/step4",
                data={"action": "save", "active_scenario": "native"},
                follow_redirects=True,
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual(1, len(adapter_calls))
            self.assertIs(
                True, adapter_calls[0][0]["has_unsaved_scenario_changes"]
            )
            unsupported = next(
                item
                for item in adapter_calls[0][1]["stages"]["inventory"]["advisories"]
                if item["id"] == "unsupported-native"
            )
            self.assert_readiness_item_contract(
                unsupported,
                item_id="unsupported-native",
                stage="inventory",
                severity="advisory",
                affected_vm_names=["legacy-01"],
                acknowledged=True,
            )
            fit_item = next(
                item
                for item in adapter_calls[0][1]["advisory_items"]
                if item["id"] == "fit-vcf-license-cost-included"
            )
            self.assert_readiness_item_contract(
                fit_item,
                item_id="fit-vcf-license-cost-included",
                stage="scenarios",
                severity="info",
                affected_vm_names=[],
                acknowledged=False,
            )

            response = client.get("/step4?tab=native")

            self.assertEqual(200, response.status_code)
            self.assertEqual(2, len(adapter_calls))
            self.assertIs(
                False, adapter_calls[1][0]["has_unsaved_scenario_changes"]
            )

    def test_successful_step4_save_clears_pending_unsaved_signal(self) -> None:
        real_adapter = app_module.build_current_readiness_context
        adapter_calls: list[dict] = []

        def capture_adapter(**kwargs: object) -> dict:
            adapter_calls.append(dict(kwargs))
            return real_adapter(**kwargs)

        with current_step4_client() as (client, state), patch.object(
            app_module,
            "build_current_readiness_context",
            side_effect=capture_adapter,
        ):
            with client.session_transaction() as sess:
                sess["_step4_unsaved_scenario_changes"] = True
            response = client.post(
                "/step4",
                data={
                    "action": "save",
                    "active_scenario": "native",
                    **{
                        app_module.inventory_placement_field_name(
                            "hybrid_placement", vm_name
                        ): placement
                        for vm_name, placement in state[
                            "step4_hybrid_placements"
                        ].items()
                    },
                },
                follow_redirects=True,
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual(1, len(adapter_calls))
            self.assertIs(
                False, adapter_calls[0]["has_unsaved_scenario_changes"]
            )
            with client.session_transaction() as sess:
                self.assertNotIn("_step4_unsaved_scenario_changes", sess)

    def test_current_adapter_blocks_ocvs_ranking_without_vcf_unit_price(self) -> None:
        adapter = getattr(app_module, "build_current_readiness_context", None)
        self.assertTrue(callable(adapter), "current readiness adapter is missing")

        result = adapter(**current_adapter_inputs(vcf_price_per_core_yearly=0.0))

        for scenario_id in ("ocvs", "hybrid"):
            with self.subTest(scenario=scenario_id):
                scenario = result["scenarios"][scenario_id]
                self.assertEqual("incomplete", scenario["pricing_state"])
                self.assertFalse(scenario["rankable"])
        self.assertIn(
            "VCF license price not set",
            {item["title"] for item in result["advisory_items"]},
        )
        self.assertIn(
            "VCF license price not set",
            {
                item["title"]
                for item in result["stages"]["scenarios"]["advisories"]
            },
        )

    def test_current_adapter_uses_explicit_incomplete_early_scenarios(self) -> None:
        adapter = getattr(app_module, "build_current_readiness_context", None)
        self.assertTrue(callable(adapter), "current readiness adapter is missing")
        inputs = current_adapter_inputs()
        inputs["scenario_analysis"] = None
        inputs["scenario_views"] = None
        inputs["pricing_inputs"] = None

        result = adapter(**inputs)

        for scenario_id in ("native", "ocvs", "hybrid"):
            with self.subTest(scenario=scenario_id):
                scenario = result["scenarios"][scenario_id]
                self.assertEqual("incomplete", scenario["pricing_state"])
                self.assertFalse(scenario["rankable"])
                self.assertIsNone(scenario["monthly_cost"])

    def test_current_adapter_fails_closed_for_malformed_pricing_inputs(self) -> None:
        adapter = getattr(app_module, "build_current_readiness_context", None)
        self.assertTrue(callable(adapter), "current readiness adapter is missing")
        inputs = current_adapter_inputs()
        inputs["pricing_inputs"]["block_storage_unit_price"] = "not-a-price"

        try:
            result = adapter(**inputs)
        except (TypeError, ValueError) as exc:
            self.fail(f"malformed pricing input escaped the adapter: {exc}")

        self.assertEqual("incomplete", result["scenarios"]["native"]["pricing_state"])
        self.assertFalse(result["scenarios"]["native"]["rankable"])

    def test_ocvs_infrastructure_pricing_requires_every_selected_rate(self) -> None:
        summary = app_module.build_ocvs_price_summary(
            vm_rows=[{"cpus": 4, "memory_gb": 8, "provisioned_gb": 100}],
            price_lookup={"Compute - Standard - E4 - OCPU": 0.03},
            block_storage_unit_price=0.02,
            block_perf_unit_price=0.001,
            iaas_discount_pct=0.0,
            selected_profile="BM.Standard.E4.128",
        )

        self.assertFalse(summary["selected"]["pricing_available"])

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
