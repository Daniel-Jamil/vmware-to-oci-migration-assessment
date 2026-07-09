import copy
import html as html_lib
import re
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import app as app_module
from assessment_readiness import build_assessment_readiness
from werkzeug.datastructures import MultiDict


def visible_page_text(markup: str) -> str:
    without_assets = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        markup,
        flags=re.I | re.S,
    )
    return " ".join(
        html_lib.unescape(re.sub(r"<[^>]+>", " ", without_assets)).split()
    )


def result_scenario_card(markup: str, scenario_id: str) -> str:
    match = re.search(
        rf'<article\b(?=[^>]*data-result-scenario="{re.escape(scenario_id)}")[^>]*>.*?</article>',
        markup,
        flags=re.S,
    )
    if match is None:
        raise AssertionError(f"Results card not found for {scenario_id}")
    return match.group(0)


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
        {
            "id": "native",
            "monthly_cost": 125.0,
            "native_vm_count": 2,
            "ocvs_vm_count": 0,
        },
        {
            "id": "ocvs",
            "monthly_cost": 825.0,
            "native_vm_count": 0,
            "ocvs_vm_count": 2,
        },
        {
            "id": "hybrid",
            "monthly_cost": 475.0,
            "native_vm_count": 1,
            "ocvs_vm_count": 1,
        },
    ]
    physical_cores = {"ocvs": 384, "hybrid": 128}
    native_plan_row = {
        **modeled_vm_rows[0],
        "hybrid_placement": "native",
        "hybrid_effective_target": "native",
    }
    ocvs_plan_row = {
        **modeled_vm_rows[1],
        "hybrid_placement": "ocvs",
        "hybrid_effective_target": "ocvs",
    }
    analysis = {
        "scenario_comparison": {"rows": scenario_rows},
        "oci_unsupported_rows": [{"vm_name": "legacy-01"}],
        "supported_native_rows": [modeled_vm_rows[0]],
        "unsupported_ocvs_rows": [modeled_vm_rows[1]],
        "hybrid_placement_plan": {
            "rows": [native_plan_row, ocvs_plan_row],
            "native_rows": [native_plan_row],
            "ocvs_rows": [ocvs_plan_row],
            "review_rows": [],
            "explicit_ocvs_rows": [ocvs_plan_row],
            "native_count": 1,
            "ocvs_count": 1,
            "review_count": 0,
            "ocvs_priced_count": 1,
        },
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
        "fit_warnings": (
            [
                {
                    "severity": "info",
                    "title": "VCF license cost included",
                    "detail": "VCF license cost is included in the modeled scenarios.",
                }
            ]
            if vcf_price_per_core_yearly > 0
            else []
        ),
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


def configure_all_native_hybrid(inputs: dict) -> None:
    modeled_rows = copy.deepcopy(inputs["pricing_inputs"]["modeled_vm_rows"])
    native_plan_rows = [
        {
            **row,
            "hybrid_placement": "native",
            "hybrid_effective_target": "native",
        }
        for row in modeled_rows
    ]
    hybrid_row = next(
        row
        for row in inputs["scenario_analysis"]["scenario_comparison"]["rows"]
        if row["id"] == "hybrid"
    )
    hybrid_row.update(native_vm_count=2, ocvs_vm_count=0)
    inputs["scenario_analysis"]["supported_native_rows"] = modeled_rows
    inputs["scenario_analysis"]["unsupported_ocvs_rows"] = []
    inputs["scenario_analysis"]["hybrid_placement_plan"] = {
        "rows": copy.deepcopy(native_plan_rows),
        "native_count": 2,
        "ocvs_count": 0,
        "review_count": 0,
        "ocvs_priced_count": 0,
        "native_rows": copy.deepcopy(native_plan_rows),
        "ocvs_rows": [],
        "review_rows": [],
        "explicit_ocvs_rows": [],
    }
    inputs["scenario_analysis"]["hybrid_ocvs_price"] = None
    inputs["scenario_analysis"]["vmware_license_summary"]["hybrid"] = {}
    inputs["app_state"]["step4_hybrid_placements"] = {
        "app-01": "native",
        "legacy-01": "native",
    }
    inputs["app_state"]["assessor_recommendation"] = "hybrid"


@contextmanager
def current_step4_client(vcf_price_per_core_yearly: float = 400.0):
    inventory_rows = copy.deepcopy(current_adapter_inputs()["inventory_rows"][:2])
    state = app_module._default_app_state()
    state["selected_vm_names"] = ["app-01", "legacy-01"]
    state["step4_hybrid_placements"] = {
        "app-01": "native",
        "legacy-01": "ocvs",
    }
    state["step4_ocvs_profile"] = "BM.Standard3.64"
    state["acknowledged_warning_ids"] = ["unsupported-native"]
    state["step4_vmware_license_price_per_core_yearly"] = vcf_price_per_core_yearly

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
        source_advisories = result["display_advisory_items"]
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
                for item in adapter_calls[0][1]["display_advisory_items"]
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
            self.assertIn(b"Unsupported for OCI Native", response.data)
            self.assertIn(
                b"These VMs remain in scope but require remediation review before using a Native placement.",
                response.data,
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
            response.close()

    def test_step4_early_redirect_preserves_pending_unsaved_signal(self) -> None:
        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
                sess["_step4_unsaved_scenario_changes"] = True

            response = client.get("/step4?tab=native")

            self.assertEqual(302, response.status_code)
            with client.session_transaction() as sess:
                self.assertIs(True, sess["_step4_unsaved_scenario_changes"])

    def test_successful_step4_export_clears_pending_unsaved_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, current_step4_client() as (
            client,
            state,
        ), patch.object(
            app_module,
            "EXPORTS_DIR",
            Path(temp_dir),
        ), patch.object(
            app_module,
            "build_migration_price_workbook_xlsx",
            return_value=b"regression workbook",
        ):
            with client.session_transaction() as sess:
                sess["_step4_unsaved_scenario_changes"] = True
            response = client.post(
                "/step4",
                data={
                    "action": "export_excel",
                    "active_scenario": "price",
                    **{
                        app_module.inventory_placement_field_name(
                            "hybrid_placement", vm_name
                        ): placement
                        for vm_name, placement in state[
                            "step4_hybrid_placements"
                        ].items()
                    },
                },
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                response.mimetype,
            )
            with client.session_transaction() as sess:
                self.assertNotIn("_step4_unsaved_scenario_changes", sess)
            response.close()

    def test_results_page_treats_blank_vcf_price_as_optional_extra(self) -> None:
        with current_step4_client(vcf_price_per_core_yearly=0.0) as (client, _state):
            response = client.get("/step4?tab=price")

        self.assertEqual(200, response.status_code)
        html = response.data.decode("utf-8", errors="replace")
        self.assertIn('data-results-comparison', html)
        self.assertIn('data-overall-readiness="draft_review_required"', html)
        self.assertEqual(3, html.count('data-result-scenario="'))
        self.assertEqual(3, html.count("Technical eligibility"))
        self.assertEqual(3, html.count("Pricing completeness"))
        self.assertEqual(3, html.count("Modeled cost"))
        self.assertEqual(3, html.count("Scenario readiness"))
        for scenario_id, state, label, tone in (
            ("native", "needs_attention", "Needs attention", "attention"),
            ("ocvs", "ready", "Ready", "ready"),
            ("hybrid", "ready", "Ready", "ready"),
        ):
            with self.subTest(scenario_id=scenario_id):
                card = result_scenario_card(html, scenario_id)
                self.assertIn(f'data-readiness-state="{state}"', card)
                self.assertRegex(
                    card,
                    rf'(?s)result-status--{tone}"[^>]*>.*?{label}',
                )
        for label in (
            "Monthly",
            "Annual",
            "3-year",
            "Cost per VM",
            "Placement split",
            "Assumptions and sizing",
            "Benefits",
            "Trade-offs",
        ):
            self.assertIn(label, html)
        self.assertNotIn("Remediation requirements", html)
        self.assertNotIn("Partial pricing", html)
        self.assertNotIn("Incomplete pricing", html)
        self.assertNotIn("Partial modeled amount", html)
        self.assertNotIn("VCF license price not set", html)
        self.assertIn("Complete pricing", html)
        self.assertIn("Complete modeled amount", html)
        self.assertNotIn("Lowest complete modeled price", html)
        self.assertNotRegex(
            visible_page_text(html).lower(),
            r"\b(medal|winner|best)\b",
        )
        self.assertIn('name="recommendation"', html)
        for value in ("native", "ocvs", "hybrid", ""):
            self.assertIn(f'value="{value}"', html)
        self.assertIn("Migration specialist recommendation", html)
        self.assertIn("Recommended path", html)
        self.assertIn("Undecided", html)
        self.assertIn("Internal notes", html)
        self.assertIn("Optional notes explaining the recommendation.", html)
        self.assertIn("Save decision", html)
        self.assertNotIn("Assessor recommendation", html)
        self.assertNotIn("Required for customer-ready Native treatment", html)
        self.assertIn('name="recommendation_rationale"', html)
        self.assertIn('maxlength="4000"', html)
        self.assertIn("data-workload-profile", html)
        self.assertIn("Workload profile", html)
        self.assertIn("Operating system mix", html)
        self.assertIn("Power state", html)
        self.assertIn("OCI Native readiness", html)
        self.assertIn("Avg vCPU / VM", html)
        self.assertIn("Save assessment", html)
        self.assertRegex(
            html,
            r'<button type="submit" class="results-button">\s*Export Excel\s*</button>',
        )
        self.assertNotIn("export_json", html)
        self.assertNotIn("Portable JSON", html)

    def test_results_save_assessment_returns_to_results_page(self) -> None:
        def fake_save_current_assessment(name: object, notes: object) -> dict:
            normalized_name = app_module.normalize_assessment_name(name)
            normalized_notes = app_module.normalize_assessment_notes(notes)
            app_module.session["active_assessment_id"] = "saved-from-results"
            app_module.session["active_assessment_name"] = normalized_name
            app_module.session["active_assessment_notes"] = normalized_notes
            return {
                "id": "saved-from-results",
                "name": normalized_name,
                "notes": normalized_notes,
            }

        with current_step4_client(vcf_price_per_core_yearly=0.0) as (
            client,
            _state,
        ), patch.object(
            app_module,
            "save_current_assessment",
            side_effect=fake_save_current_assessment,
        ):
            page = client.get("/step4?tab=price")
            html = page.data.decode("utf-8", errors="replace")
            self.assertIn('name="return_to"', html)
            self.assertIn('value="/step4?tab=price"', html)

            response = client.post(
                "/",
                data={
                    "action": "save_assessment",
                    "assessment_name": "Current assessment",
                    "assessment_notes": "",
                    "return_to": "/step4?tab=price",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/step4?tab=price", response.headers.get("Location"))

    def test_results_save_assessment_uses_referrer_when_return_target_is_missing(self) -> None:
        def fake_save_current_assessment(name: object, notes: object) -> dict:
            normalized_name = app_module.normalize_assessment_name(name)
            normalized_notes = app_module.normalize_assessment_notes(notes)
            app_module.session["active_assessment_id"] = "saved-from-stale-results"
            app_module.session["active_assessment_name"] = normalized_name
            app_module.session["active_assessment_notes"] = normalized_notes
            return {
                "id": "saved-from-stale-results",
                "name": normalized_name,
                "notes": normalized_notes,
            }

        with current_step4_client(vcf_price_per_core_yearly=0.0) as (
            client,
            _state,
        ), patch.object(
            app_module,
            "save_current_assessment",
            side_effect=fake_save_current_assessment,
        ):
            response = client.post(
                "/",
                data={
                    "action": "save_assessment",
                    "assessment_name": "Current assessment",
                    "assessment_notes": "",
                },
                headers={"Referer": "http://localhost/step4?tab=price"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/step4?tab=price", response.headers.get("Location"))

        ready_readiness = {
            "overall_state": "draft_review_required",
            "lowest_complete_scenario": "native",
            "scenarios": {
                scenario_id: {
                    "state": "ready",
                    "technical_eligibility": "eligible",
                    "pricing_state": "complete",
                    "rankable": True,
                }
                for scenario_id in ("native", "ocvs", "hybrid")
            },
        }
        ready_views = [
            {
                "id": scenario_id,
                "title": scenario_id.upper(),
                "scenario": {"monthly_cost": 100.0},
            }
            for scenario_id in ("native", "ocvs", "hybrid")
        ]
        with app_module.app.test_request_context("/step4?tab=price"):
            ready_results = app_module.build_results_page_context(
                ready_readiness,
                ready_views,
                {},
            )
            ready_markup = app_module.render_template(
                "_results_comparison.html",
                results=ready_results,
                pricing_symbol="$",
            )
        for scenario_id in ("native", "ocvs", "hybrid"):
            ready_card = result_scenario_card(ready_markup, scenario_id)
            self.assertIn('data-readiness-state="ready"', ready_card)
            self.assertRegex(
                ready_card,
                r'(?s)result-status--ready"[^>]*>.*?Ready',
            )

    def test_recommendation_save_persists_optional_vcf_selection_and_rationale(self) -> None:
        rationale = "Retain the legacy workload on OCVS during the first migration wave."
        with current_step4_client(vcf_price_per_core_yearly=0.0) as (client, state):
            response = client.post(
                "/step4",
                data={
                    "action": "save_recommendation",
                    "recommendation": "ocvs",
                    "recommendation_rationale": rationale,
                },
                follow_redirects=False,
            )

            self.assertEqual(303, response.status_code)
            self.assertTrue(response.headers.get("Location", "").endswith("/step4?tab=price"))
            self.assertEqual("ocvs", state["assessor_recommendation"])
            self.assertEqual(rationale, state["assessor_recommendation_rationale"])

            reloaded = client.get("/step4?tab=price")

        html = reloaded.data.decode("utf-8", errors="replace")
        self.assertRegex(html, r'value="ocvs"\s+checked')
        self.assertIn(rationale, html)
        self.assertIn("Complete pricing", html)
        self.assertNotIn("VCF license price not set", html)
        self.assertRegex(
            html,
            r'<button type="submit" class="results-button">\s*Export Excel\s*</button>',
        )

    def test_recommendation_submission_rejects_invalid_payloads_transactionally(self) -> None:
        invalid_forms = {
            "duplicate action": MultiDict(
                [
                    ("action", "save_recommendation"),
                    ("action", "save_recommendation"),
                    ("recommendation", "native"),
                    ("recommendation_rationale", "Documented."),
                ]
            ),
            "duplicate recommendation": MultiDict(
                [
                    ("action", "save_recommendation"),
                    ("recommendation", "native"),
                    ("recommendation", "ocvs"),
                    ("recommendation_rationale", "Documented."),
                ]
            ),
            "duplicate rationale": MultiDict(
                [
                    ("action", "save_recommendation"),
                    ("recommendation", "native"),
                    ("recommendation_rationale", "First"),
                    ("recommendation_rationale", "Second"),
                ]
            ),
            "missing recommendation": MultiDict(
                [
                    ("action", "save_recommendation"),
                    ("recommendation_rationale", "Documented."),
                ]
            ),
            "invalid recommendation": MultiDict(
                [
                    ("action", "save_recommendation"),
                    ("recommendation", "automatic"),
                    ("recommendation_rationale", "Documented."),
                ]
            ),
            "oversized rationale": MultiDict(
                [
                    ("action", "save_recommendation"),
                    ("recommendation", "native"),
                    ("recommendation_rationale", "x" * 4001),
                ]
            ),
            "unknown field": MultiDict(
                [
                    ("action", "save_recommendation"),
                    ("recommendation", "native"),
                    ("recommendation_rationale", "Documented."),
                    ("automatic_choice", "ocvs"),
                ]
            ),
            "unknown action": MultiDict(
                [
                    ("action", "choose_winner"),
                    ("recommendation", "native"),
                    ("recommendation_rationale", "Documented."),
                ]
            ),
        }

        with current_step4_client() as (client, state):
            prior_state = copy.deepcopy(state)
            for label, form in invalid_forms.items():
                with self.subTest(label=label):
                    response = client.post(
                        "/step4",
                        data=form,
                        follow_redirects=False,
                    )
                    self.assertIn(response.status_code, {302, 303})
                    self.assertEqual(prior_state, state)

        non_string_values = {
            "integer": 1,
            "list": ["native"],
            "mapping": {"value": "native"},
            "none": None,
        }
        for field_name, expected_error in (
            ("recommendation", "Specialist recommendation must be text."),
            ("recommendation_rationale", "Recommendation rationale must be text."),
        ):
            for value_label, malformed_value in non_string_values.items():
                with self.subTest(field=field_name, value=value_label):
                    form_values = {
                        "action": "save_recommendation",
                        "recommendation": "ocvs",
                        "recommendation_rationale": "Keep the prior rationale.",
                    }
                    form_values[field_name] = malformed_value
                    malformed_form = MultiDict(form_values.items())

                    _parsed, errors = app_module.parse_recommendation_submission(
                        malformed_form
                    )

                    with current_step4_client() as (_client, state):
                        state["assessor_recommendation"] = "native"
                        state["assessor_recommendation_rationale"] = "Prior rationale."
                        prior_state = copy.deepcopy(state)
                        with app_module.app.test_request_context(
                            "/step4",
                            method="POST",
                        ):
                            app_module.session["_app_instance_id"] = (
                                app_module.APP_INSTANCE_ID
                            )
                            app_module.session["selected_rvtools_file"] = "fixture.csv"
                            app_module.session["selected_pricelist_file"] = "prices.json"
                            app_module.session["selected_currency"] = "EUR"
                            app_module.session["customer_name"] = "Example Customer"
                            app_module.session["active_assessment_name"] = (
                                "Current assessment"
                            )
                            app_module.request.form = malformed_form
                            route_result = app_module.step4()

                        self.assertIsInstance(route_result, tuple)
                        self.assertEqual(303, route_result[1])
                        self.assertEqual(prior_state, state)
                    self.assertIn(expected_error, errors)

    def test_recommendation_persistence_failure_keeps_prior_state(self) -> None:
        with current_step4_client() as (client, state), patch.object(
            app_module,
            "save_app_state",
            side_effect=OSError("injected recommendation persistence failure"),
        ):
            prior_state = copy.deepcopy(state)
            response = client.post(
                "/step4",
                data={
                    "action": "save_recommendation",
                    "recommendation": "hybrid",
                    "recommendation_rationale": "Keep both landing zones in draft review.",
                },
                follow_redirects=False,
            )

            self.assertEqual(303, response.status_code)
            self.assertEqual(prior_state, state)
            redirected = client.get(response.headers["Location"])

        self.assertIn(b"prior decision was kept", redirected.data)

    def test_native_treatment_rationale_enables_customer_ready_excel_label(self) -> None:
        rationale = "Remediate the legacy guest before placing it on OCI Native."
        with current_step4_client() as (client, _state):
            draft_response = client.post(
                "/step4",
                data={
                    "action": "save_recommendation",
                    "recommendation": "native",
                    "recommendation_rationale": "",
                },
                follow_redirects=True,
            )
            self.assertRegex(
                draft_response.data.decode("utf-8", errors="replace"),
                r'<button type="submit" class="results-button">\s*Export Excel\s*</button>',
            )

            ready_response = client.post(
                "/step4",
                data={
                    "action": "save_recommendation",
                    "recommendation": "native",
                    "recommendation_rationale": rationale,
                },
                follow_redirects=True,
            )

        self.assertEqual(200, ready_response.status_code)
        ready_html = ready_response.data.decode("utf-8", errors="replace")
        self.assertRegex(
            ready_html,
            r'<button type="submit" class="results-button">\s*Export Excel\s*</button>',
        )
        for scenario_id, state, label, tone in (
            ("native", "needs_attention", "Needs attention", "attention"),
            ("ocvs", "ready", "Ready", "ready"),
            ("hybrid", "ready", "Ready", "ready"),
        ):
            with self.subTest(scenario_id=scenario_id):
                card = result_scenario_card(ready_html, scenario_id)
                self.assertIn(f'data-readiness-state="{state}"', card)
                self.assertRegex(
                    card,
                    rf'(?s)result-status--{tone}"[^>]*>.*?{label}',
                )
        self.assertIn(rationale.encode(), ready_response.data)
        self.assertIn(b'role="status"', ready_response.data)

    def test_results_banner_uses_draft_copy_when_setup_identity_is_missing(self) -> None:
        with current_step4_client(vcf_price_per_core_yearly=0.0) as (client, _state):
            with client.session_transaction() as sess:
                sess["active_assessment_name"] = ""
                sess["customer_name"] = ""

            response = client.get("/step4?tab=price")

        self.assertEqual(200, response.status_code)
        html = response.data.decode("utf-8", errors="replace")
        text = visible_page_text(html)
        self.assertIn('data-overall-readiness="incomplete"', html)
        self.assertIn("Draft results available", text)
        self.assertIn("customer-ready export", text)
        self.assertIn("Export Excel", text)
        self.assertNotIn("Assessment incomplete", text)
        self.assertNotIn("Review outstanding setup, inventory, scenario, or pricing requirements.", text)

    def test_inventory_review_stage_footer_exposes_save_continue_submit(self) -> None:
        with current_step4_client() as (client, _state):
            response = client.get("/step3")

        self.assertEqual(200, response.status_code)
        html = response.data.decode("utf-8", errors="replace")
        self.assertIn('id="continue_step4_form"', html)
        self.assertRegex(
            html,
            r'(?s)<footer[^>]*class="workspace-stage-actions"[^>]*>.*?'
            r'<button[^>]*class="[^"]*workspace-action--primary[^"]*"[^>]*'
            r'form="continue_step4_form"[^>]*name="continue_to_scenarios"[^>]*'
            r'value="1"[^>]*>.*?Save &amp; Continue.*?</button>',
        )

    def test_scenario_stage_footer_exposes_save_continue_submit(self) -> None:
        with current_step4_client() as (client, _state):
            response = client.get("/step4?tab=ocvs")

        self.assertEqual(200, response.status_code)
        html = response.data.decode("utf-8", errors="replace")
        self.assertIn('id="step4-form"', html)
        self.assertRegex(
            html,
            r'(?s)<footer[^>]*class="workspace-stage-actions"[^>]*>.*?'
            r'<button[^>]*class="[^"]*workspace-action--primary[^"]*"[^>]*'
            r'form="step4-form"[^>]*name="continue_to_results"[^>]*'
            r'value="1"[^>]*>.*?Save &amp; Continue.*?</button>',
        )

    def test_scenario_footer_save_continue_redirects_to_results(self) -> None:
        with current_step4_client() as (client, _state):
            response = client.post(
                "/step4",
                data={
                    "active_scenario": "ocvs",
                    "continue_to_results": "1",
                    "iaas_discount_pct": "0",
                    "ocvs_profile": "best_fit",
                    "ocvs_commitment_term": "payg",
                    "ocvs_vcpu_per_ocpu": "4",
                    "ocvs_cpu_headroom_pct": "20",
                    "ocvs_memory_headroom_pct": "20",
                    "ocvs_storage_headroom_pct": "20",
                    "ocvs_dense_vsan_usable_pct": "80",
                    "ocvs_standard_storage_vpu": "10",
                    "ocvs_dr_nodes": "0",
                    "vmware_license_price_per_core_yearly": "400.00",
                },
                follow_redirects=False,
            )

        self.assertIn(response.status_code, {302, 303})
        self.assertTrue(response.headers.get("Location", "").endswith("/step4?tab=price"))

    def test_critical_fit_warning_centrally_blocks_customer_ready_export(self) -> None:
        inputs = current_adapter_inputs()
        inputs["app_state"]["assessor_recommendation"] = "ocvs"
        inputs["scenario_analysis"]["fit_warnings"] = [
            {
                "id": "host-limit",
                "severity": "critical",
                "title": "OCVS host limit exceeded",
                "detail": "The selected OCVS shape exceeds the supported cluster limit.",
            }
        ]

        result = app_module.build_current_readiness_context(**inputs)

        self.assertTrue(result["scenarios"]["ocvs"]["rankable"])
        self.assertEqual("needs_attention", result["stages"]["scenarios"]["state"])
        self.assertEqual("needs_attention", result["stages"]["results"]["state"])
        self.assertEqual("incomplete", result["overall_state"])
        self.assertFalse(result["customer_ready_export"])
        self.assertIn(
            "OCVS host limit exceeded",
            {item["title"] for item in result["blocking_items"]},
        )

    def test_advisory_fit_warning_does_not_block_rankability_or_export(self) -> None:
        inputs = current_adapter_inputs()
        inputs["app_state"]["assessor_recommendation"] = "ocvs"
        inputs["scenario_analysis"]["fit_warnings"] = [
            {
                "id": "capacity-review",
                "severity": "warning",
                "title": "Review spare capacity",
                "detail": "Confirm the selected spare-node policy with the platform team.",
            }
        ]

        result = app_module.build_current_readiness_context(**inputs)

        self.assertTrue(result["scenarios"]["ocvs"]["rankable"])
        self.assertEqual("complete", result["stages"]["scenarios"]["state"])
        self.assertTrue(result["customer_ready_export"])
        self.assertIn(
            "Review spare capacity",
            {item["title"] for item in result["advisory_items"]},
        )

    def test_fit_warning_ids_are_collision_safe_and_deterministic(self) -> None:
        inputs = current_adapter_inputs()
        inputs["inventory_issues"] = [
            {
                "id": "fit-capacity-alert",
                "title": "Inventory capacity alert",
                "detail": "Inventory source advisory.",
                "severity": "advisory",
                "vm_names": ["app-01"],
            }
        ]
        inputs["app_state"]["acknowledged_warning_ids"] = ["fit-capacity-alert"]
        inputs["scenario_analysis"]["fit_warnings"] = [
            {
                "id": "fit-capacity-alert",
                "severity": "warning",
                "title": "Scenario capacity alert",
                "detail": "First scenario advisory.",
            },
            {
                "id": "fit-capacity-alert",
                "severity": "warning",
                "title": "Scenario capacity alert",
                "detail": "Second scenario advisory.",
            },
        ]

        first = app_module.build_current_readiness_context(**copy.deepcopy(inputs))
        second = app_module.build_current_readiness_context(**copy.deepcopy(inputs))
        first_ids = [
            item["id"]
            for item in first["advisory_items"]
            if item["title"] == "Scenario capacity alert"
        ]
        second_ids = [
            item["id"]
            for item in second["advisory_items"]
            if item["title"] == "Scenario capacity alert"
        ]

        self.assertEqual(first_ids, second_ids)
        self.assertEqual(2, len(first_ids))
        self.assertEqual(2, len(set(first_ids)))
        self.assertNotIn("fit-capacity-alert", first_ids)

    def test_ocvs_pricing_fails_closed_for_missing_or_malformed_host_pricing(self) -> None:
        mutations = {
            "missing summary": lambda values: values["scenario_analysis"].update(
                ocvs_price=None
            ),
            "missing host count": lambda values: values["scenario_analysis"][
                "ocvs_price"
            ].update(selected={"pricing_available": True}),
            "zero host count": lambda values: values["scenario_analysis"][
                "ocvs_price"
            ]["selected"].update(host_count=0),
            "malformed host count": lambda values: values["scenario_analysis"][
                "ocvs_price"
            ]["selected"].update(host_count="not-a-count"),
            "pricing unavailable": lambda values: values["scenario_analysis"][
                "ocvs_price"
            ]["selected"].update(pricing_available=False),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                inputs = current_adapter_inputs()
                mutate(inputs)

                result = app_module.build_current_readiness_context(**inputs)

                self.assertEqual(
                    "incomplete", result["scenarios"]["ocvs"]["pricing_state"]
                )
                self.assertFalse(result["scenarios"]["ocvs"]["rankable"])

    def test_hybrid_pricing_distinguishes_empty_and_malformed_ocvs_subsets(self) -> None:
        empty_inputs = current_adapter_inputs()
        configure_all_native_hybrid(empty_inputs)

        empty_result = app_module.build_current_readiness_context(**empty_inputs)

        self.assertEqual(
            "complete", empty_result["scenarios"]["hybrid"]["pricing_state"]
        )
        self.assertTrue(empty_result["scenarios"]["hybrid"]["rankable"])

        malformed_values = (None, -1, "one", 1.5, True)
        for malformed_count in malformed_values:
            with self.subTest(malformed_count=malformed_count):
                inputs = current_adapter_inputs()
                hybrid_row = next(
                    row
                    for row in inputs["scenario_analysis"]["scenario_comparison"]["rows"]
                    if row["id"] == "hybrid"
                )
                hybrid_row["ocvs_vm_count"] = malformed_count

                result = app_module.build_current_readiness_context(**inputs)

                self.assertEqual(
                    "incomplete", result["scenarios"]["hybrid"]["pricing_state"]
                )
                self.assertFalse(result["scenarios"]["hybrid"]["rankable"])

    def test_hybrid_empty_ocvs_subset_requires_native_rows_and_models(self) -> None:
        mutations = {
            "missing supported Native rows": lambda values: values[
                "scenario_analysis"
            ].pop("supported_native_rows"),
            "missing modeled Native rows": lambda values: values[
                "pricing_inputs"
            ].update(modeled_vm_rows=[]),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                inputs = current_adapter_inputs()
                configure_all_native_hybrid(inputs)
                mutate(inputs)

                result = app_module.build_current_readiness_context(**inputs)

                self.assertEqual(
                    "incomplete", result["scenarios"]["hybrid"]["pricing_state"]
                )
                self.assertFalse(result["scenarios"]["hybrid"]["rankable"])
                self.assertFalse(result["customer_ready_export"])

    def test_hybrid_incomplete_native_subset_count_fails_closed(self) -> None:
        inputs = current_adapter_inputs()
        configure_all_native_hybrid(inputs)
        inputs["scenario_analysis"]["supported_native_rows"] = [
            copy.deepcopy(inputs["pricing_inputs"]["modeled_vm_rows"][0])
        ]

        result = app_module.build_current_readiness_context(**inputs)

        self.assertEqual(
            "incomplete", result["scenarios"]["hybrid"]["pricing_state"]
        )
        self.assertFalse(result["scenarios"]["hybrid"]["rankable"])
        self.assertFalse(result["customer_ready_export"])

    def test_hybrid_complete_all_native_partition_remains_rankable(self) -> None:
        inputs = current_adapter_inputs()
        configure_all_native_hybrid(inputs)

        result = app_module.build_current_readiness_context(**inputs)

        self.assertEqual(
            "complete", result["scenarios"]["hybrid"]["pricing_state"]
        )
        self.assertTrue(result["scenarios"]["hybrid"]["rankable"])
        self.assertTrue(result["customer_ready_export"])

    def test_hybrid_partition_mismatch_and_duplicate_names_fail_closed(self) -> None:
        mutations = {
            "count mismatch": lambda values: next(
                row
                for row in values["scenario_analysis"]["scenario_comparison"]["rows"]
                if row["id"] == "hybrid"
            ).update(native_vm_count=1),
            "duplicate Native names": lambda values: values["scenario_analysis"].update(
                supported_native_rows=[
                    copy.deepcopy(values["pricing_inputs"]["modeled_vm_rows"][0]),
                    copy.deepcopy(values["pricing_inputs"]["modeled_vm_rows"][0]),
                ]
            ),
            "conflicting OCVS names": lambda values: values["scenario_analysis"].update(
                unsupported_ocvs_rows=[
                    copy.deepcopy(values["pricing_inputs"]["modeled_vm_rows"][1])
                ]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                inputs = current_adapter_inputs()
                configure_all_native_hybrid(inputs)
                mutate(inputs)

                try:
                    result = app_module.build_current_readiness_context(**inputs)
                except (TypeError, ValueError) as exc:
                    self.fail(f"Hybrid partition mismatch escaped the adapter: {exc}")

                self.assertEqual(
                    "incomplete", result["scenarios"]["hybrid"]["pricing_state"]
                )
                self.assertFalse(result["scenarios"]["hybrid"]["rankable"])
                self.assertFalse(result["customer_ready_export"])

    def test_hybrid_plan_native_rows_must_match_top_level_partition(self) -> None:
        def duplicate_native_rows(values: dict) -> None:
            native_row = copy.deepcopy(
                values["scenario_analysis"]["hybrid_placement_plan"]["native_rows"][0]
            )
            values["scenario_analysis"]["hybrid_placement_plan"]["native_rows"] = [
                native_row,
                copy.deepcopy(native_row),
            ]

        def excluded_native_row(values: dict) -> None:
            excluded_row = copy.deepcopy(
                values["scenario_analysis"]["hybrid_placement_plan"]["native_rows"][0]
            )
            excluded_row["vm_name"] = "excluded-01"
            values["scenario_analysis"]["hybrid_placement_plan"]["native_rows"] = [
                excluded_row
            ]

        def conflicting_native_row(values: dict) -> None:
            values["scenario_analysis"]["hybrid_placement_plan"]["native_rows"] = [
                copy.deepcopy(
                    values["scenario_analysis"]["hybrid_placement_plan"]["ocvs_rows"][0]
                )
            ]

        for label, mutate in {
            "duplicate": duplicate_native_rows,
            "excluded": excluded_native_row,
            "conflicting": conflicting_native_row,
        }.items():
            with self.subTest(case=label):
                inputs = current_adapter_inputs()
                inputs["app_state"]["assessor_recommendation"] = "hybrid"
                mutate(inputs)

                result = app_module.build_current_readiness_context(**inputs)

                self.assertEqual(
                    "incomplete", result["scenarios"]["hybrid"]["pricing_state"]
                )
                self.assertFalse(result["scenarios"]["hybrid"]["rankable"])
                self.assertFalse(result["customer_ready_export"])

    def test_hybrid_plan_ocvs_rows_must_match_top_level_partition(self) -> None:
        inputs = current_adapter_inputs()
        inputs["app_state"]["assessor_recommendation"] = "hybrid"
        plan = inputs["scenario_analysis"]["hybrid_placement_plan"]
        plan["ocvs_rows"] = [copy.deepcopy(plan["native_rows"][0])]

        result = app_module.build_current_readiness_context(**inputs)

        self.assertEqual(
            "incomplete", result["scenarios"]["hybrid"]["pricing_state"]
        )
        self.assertFalse(result["scenarios"]["hybrid"]["rankable"])
        self.assertFalse(result["customer_ready_export"])

    def test_hybrid_plan_rows_fail_closed_when_malformed_or_inconsistent(self) -> None:
        def duplicate_rows(values: dict) -> None:
            plan = values["scenario_analysis"]["hybrid_placement_plan"]
            plan["rows"] = [
                copy.deepcopy(plan["rows"][0]),
                copy.deepcopy(plan["rows"][0]),
            ]

        def missing_row(values: dict) -> None:
            plan = values["scenario_analysis"]["hybrid_placement_plan"]
            plan["rows"] = [copy.deepcopy(plan["rows"][0])]

        def conflicting_target(values: dict) -> None:
            plan = values["scenario_analysis"]["hybrid_placement_plan"]
            plan["rows"][1]["hybrid_effective_target"] = "native"

        mutations = {
            "scalar rows": lambda values: values["scenario_analysis"][
                "hybrid_placement_plan"
            ].update(rows="not-a-list"),
            "duplicate rows": duplicate_rows,
            "missing selected row": missing_row,
            "conflicting effective target": conflicting_target,
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                inputs = current_adapter_inputs()
                inputs["app_state"]["assessor_recommendation"] = "hybrid"
                mutate(inputs)

                try:
                    result = app_module.build_current_readiness_context(**inputs)
                except (TypeError, ValueError) as exc:
                    self.fail(f"malformed Hybrid plan rows escaped the adapter: {exc}")

                self.assertEqual(
                    "incomplete", result["scenarios"]["hybrid"]["pricing_state"]
                )
                self.assertFalse(result["scenarios"]["hybrid"]["rankable"])
                self.assertFalse(result["customer_ready_export"])

    def test_modeled_hybrid_analysis_requires_placement_plan(self) -> None:
        inputs = current_adapter_inputs()
        inputs["app_state"]["assessor_recommendation"] = "hybrid"
        inputs["scenario_analysis"].pop("hybrid_placement_plan")

        result = app_module.build_current_readiness_context(**inputs)

        self.assertEqual(
            "incomplete", result["scenarios"]["hybrid"]["pricing_state"]
        )
        self.assertFalse(result["scenarios"]["hybrid"]["rankable"])
        self.assertFalse(result["customer_ready_export"])

    def test_hybrid_real_shaped_review_plan_remains_complete(self) -> None:
        inputs = current_adapter_inputs()
        inputs["app_state"]["assessor_recommendation"] = "hybrid"
        plan = inputs["scenario_analysis"]["hybrid_placement_plan"]
        review_row = copy.deepcopy(plan["rows"][1])
        review_row["hybrid_placement"] = "review"
        review_row["hybrid_effective_target"] = "ocvs"
        plan.update(
            rows=[copy.deepcopy(plan["rows"][0]), review_row],
            native_rows=[copy.deepcopy(plan["native_rows"][0])],
            ocvs_rows=[copy.deepcopy(review_row)],
            review_rows=[copy.deepcopy(review_row)],
            explicit_ocvs_rows=[],
            native_count=1,
            ocvs_count=0,
            review_count=1,
            ocvs_priced_count=1,
        )

        result = app_module.build_current_readiness_context(**inputs)

        self.assertEqual(
            "complete", result["scenarios"]["hybrid"]["pricing_state"]
        )
        self.assertTrue(result["scenarios"]["hybrid"]["rankable"])
        self.assertTrue(result["customer_ready_export"])

    def test_hybrid_positive_ocvs_subset_requires_hosts_and_pricing(self) -> None:
        mutations = {
            "missing summary": lambda values: values["scenario_analysis"].update(
                hybrid_ocvs_price=None
            ),
            "zero hosts": lambda values: values["scenario_analysis"][
                "hybrid_ocvs_price"
            ]["selected"].update(host_count=0),
            "pricing unavailable": lambda values: values["scenario_analysis"][
                "hybrid_ocvs_price"
            ]["selected"].update(pricing_available=False),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                inputs = current_adapter_inputs()
                mutate(inputs)

                result = app_module.build_current_readiness_context(**inputs)

                self.assertEqual(
                    "incomplete", result["scenarios"]["hybrid"]["pricing_state"]
                )
                self.assertFalse(result["scenarios"]["hybrid"]["rankable"])

    def test_adapter_rejects_malformed_selected_vm_names_without_crashing(self) -> None:
        for malformed_names in (17, True, "app-01", {"app-01"}, ["app-01", 17]):
            with self.subTest(malformed_names=malformed_names):
                inputs = current_adapter_inputs()
                inputs["selected_vm_names"] = malformed_names

                try:
                    result = app_module.build_current_readiness_context(**inputs)
                except (TypeError, ValueError) as exc:
                    self.fail(f"malformed selected VM names escaped the adapter: {exc}")

                self.assertIn(
                    "invalid-selected-vm-names",
                    {item["id"] for item in result["blocking_items"]},
                )
                self.assertEqual(
                    "needs_attention", result["stages"]["inventory"]["state"]
                )
                self.assertFalse(result["customer_ready_export"])

    def test_adapter_preserves_malformed_unsupported_rows_as_integrity_advisory(self) -> None:
        malformed_values = (
            17,
            True,
            "legacy-01",
            {"vm_name": "legacy-01"},
            [{"vm_name": "legacy-01"}, 17],
        )
        for malformed_rows in malformed_values:
            with self.subTest(malformed_rows=malformed_rows):
                inputs = current_adapter_inputs()
                inputs["app_state"]["assessor_recommendation"] = "native"
                inputs["app_state"]["assessor_recommendation_rationale"] = (
                    "Remediate unsupported workloads before migration."
                )
                inputs["scenario_analysis"]["oci_unsupported_rows"] = malformed_rows

                result = app_module.build_current_readiness_context(**inputs)

                self.assertEqual(
                    "needs_attention", result["scenarios"]["native"]["state"]
                )
                self.assertTrue(result["scenarios"]["native"]["rankable"])
                self.assertFalse(result["customer_ready_export"])
                self.assertIn(
                    "invalid-native-unsupported-vms",
                    {item["id"] for item in result["advisory_items"]},
                )

    def test_adapter_malformed_issue_and_scenario_inputs_fail_closed(self) -> None:
        inventory_inputs = current_adapter_inputs()
        inventory_inputs["app_state"]["assessor_recommendation"] = "ocvs"
        inventory_inputs["inventory_issues"] = 17

        inventory_result = app_module.build_current_readiness_context(
            **inventory_inputs
        )

        self.assertIn(
            "invalid-inventory-issues",
            {item["id"] for item in inventory_result["blocking_items"]},
        )
        self.assertFalse(inventory_result["customer_ready_export"])

        malformed_scenarios = (
            ("analysis", 17, []),
            ("views", current_adapter_inputs()["scenario_analysis"], 17),
            (
                "comparison rows",
                {
                    **current_adapter_inputs()["scenario_analysis"],
                    "scenario_comparison": {"rows": "not-a-list"},
                },
                [],
            ),
            (
                "fit warnings",
                {
                    **current_adapter_inputs()["scenario_analysis"],
                    "fit_warnings": "not-a-list",
                },
                [],
            ),
        )
        for label, analysis, views in malformed_scenarios:
            with self.subTest(case=label):
                inputs = current_adapter_inputs()
                inputs["app_state"]["assessor_recommendation"] = "ocvs"
                inputs["scenario_analysis"] = analysis
                inputs["scenario_views"] = views

                try:
                    result = app_module.build_current_readiness_context(**inputs)
                except (TypeError, ValueError) as exc:
                    self.fail(f"malformed scenario input escaped the adapter: {exc}")

                self.assertEqual(
                    "needs_attention", result["stages"]["scenarios"]["state"]
                )
                self.assertFalse(result["customer_ready_export"])

    def test_current_adapter_allows_ocvs_ranking_without_vcf_unit_price(self) -> None:
        adapter = getattr(app_module, "build_current_readiness_context", None)
        self.assertTrue(callable(adapter), "current readiness adapter is missing")

        result = adapter(**current_adapter_inputs(vcf_price_per_core_yearly=0.0))

        for scenario_id in ("ocvs", "hybrid"):
            with self.subTest(scenario=scenario_id):
                scenario = result["scenarios"][scenario_id]
                self.assertEqual("complete", scenario["pricing_state"])
                self.assertTrue(scenario["rankable"])
        self.assertNotIn(
            "VCF license price not set",
            {item["title"] for item in result["advisory_items"]},
        )
        self.assertNotIn(
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

    def test_optimized3_ocvs_profile_uses_standard_storage_model(self) -> None:
        summary = app_module.build_ocvs_price_summary(
            vm_rows=[{"cpus": 60, "memory_gb": 800, "provisioned_gb": 1000}],
            price_lookup={
                "Compute - Optimized - X9 - OCPU": 0.04,
                "Compute - Optimized - X9 - Memory": 0.002,
            },
            block_storage_unit_price=0.02,
            block_perf_unit_price=0.001,
            iaas_discount_pct=0.0,
            selected_profile="BM.Optimized3.36",
        )

        selected = summary["selected"]

        self.assertEqual("BM.Optimized3.36", selected["shape"])
        self.assertEqual("Standard", selected["host_type"])
        self.assertEqual(36, selected["ocpus_per_host"])
        self.assertEqual(512, selected["memory_gb_per_host"])
        self.assertEqual(0, selected["hosts_by_storage"])
        self.assertEqual(10, selected["standard_storage_vpu"])
        self.assertGreater(selected["storage_monthly_cost"], 0.0)
        self.assertTrue(selected["pricing_available"])

    def test_native_stays_eligible_and_rankable_with_unsupported_vms(self) -> None:
        result = build_assessment_readiness(complete_context())
        native = result["scenarios"]["native"]

        self.assertEqual("eligible", native["technical_eligibility"])
        self.assertEqual("needs_attention", native["state"])
        self.assertTrue(native["rankable"])
        self.assertEqual("native", result["lowest_complete_scenario"])

    def test_pure_model_critical_scenario_issue_blocks_completion(self) -> None:
        context = complete_context()
        context["recommendation"] = "ocvs"
        context["scenario_issues"] = [
            {
                "id": "fit-host-limit",
                "title": "OCVS host limit exceeded",
                "detail": "The modeled host count exceeds the supported cluster limit.",
                "stage": "scenarios",
                "severity": "critical",
                "affected_vm_names": [],
            }
        ]

        result = build_assessment_readiness(context)

        self.assertTrue(result["scenarios"]["ocvs"]["rankable"])
        self.assertEqual("needs_attention", result["stages"]["scenarios"]["state"])
        self.assertEqual("needs_attention", result["stages"]["results"]["state"])
        self.assertEqual("incomplete", result["overall_state"])
        self.assertFalse(result["customer_ready_export"])
        self.assertEqual(
            ["fit-host-limit"],
            [item["id"] for item in result["stages"]["scenarios"]["blockers"]],
        )
        self.assertIn(
            "fit-host-limit", {item["id"] for item in result["blocking_items"]}
        )

    def test_pure_model_advisory_scenario_issue_remains_nonblocking(self) -> None:
        context = complete_context()
        context["recommendation"] = "ocvs"
        context["scenario_issues"] = [
            {
                "id": "fit-capacity-review",
                "title": "Review spare capacity",
                "detail": "Confirm spare capacity before final approval.",
                "stage": "scenarios",
                "severity": "warning",
                "affected_vm_names": [],
            }
        ]

        result = build_assessment_readiness(context)

        self.assertTrue(result["scenarios"]["ocvs"]["rankable"])
        self.assertEqual("complete", result["stages"]["scenarios"]["state"])
        self.assertTrue(result["customer_ready_export"])
        self.assertIn(
            "fit-capacity-review",
            {item["id"] for item in result["advisory_items"]},
        )

    def test_pure_model_display_advisories_include_acknowledged_inventory_items(self) -> None:
        context = complete_context()
        context["recommendation"] = "ocvs"

        result = build_assessment_readiness(context)

        self.assertNotIn(
            "unsupported-native", {item["id"] for item in result["advisory_items"]}
        )
        unsupported = next(
            item
            for item in result["display_advisory_items"]
            if item["id"] == "unsupported-native"
        )
        self.assertIs(True, unsupported["acknowledged"])
        self.assertTrue(result["customer_ready_export"])

    def test_pure_model_malformed_scenario_issues_fail_closed(self) -> None:
        malformed_values = (
            17,
            True,
            "fit-host-limit",
            [{"id": "fit-capacity-review", "severity": "warning"}, 17],
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed):
                context = complete_context()
                context["recommendation"] = "ocvs"
                context["scenario_issues"] = malformed

                result = build_assessment_readiness(context)

                self.assertEqual(
                    "needs_attention", result["stages"]["scenarios"]["state"]
                )
                self.assertFalse(result["customer_ready_export"])
                self.assertIn(
                    "invalid-scenario-issues",
                    {item["id"] for item in result["blocking_items"]},
                )

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

        self.assertEqual("draft_review_required", unacknowledged["overall_state"])
        self.assertFalse(unacknowledged["customer_ready_export"])

        context["inventory"]["acknowledged_warning_ids"] = ["unsupported-native"]
        context["recommendation_rationale"] = ""
        draft = build_assessment_readiness(context)

        self.assertEqual("draft_review_required", draft["overall_state"])
        self.assertFalse(draft["customer_ready_export"])

    def test_missing_inventory_values_are_nonblocking_advisories(self) -> None:
        context = complete_context()
        context["recommendation"] = "ocvs"
        context["inventory"]["issues"].append(
            {"id": "missing-storage", "severity": "advisory", "vm_names": ["app-01"]}
        )

        result = build_assessment_readiness(context)

        self.assertEqual("complete", result["stages"]["inventory"]["state"])
        self.assertEqual("customer_ready", result["overall_state"])
        self.assertTrue(result["customer_ready_export"])
        self.assertNotIn(
            "missing-storage", {item["id"] for item in result["blocking_items"]}
        )
        self.assertIn(
            "missing-storage", {item["id"] for item in result["advisory_items"]}
        )

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
