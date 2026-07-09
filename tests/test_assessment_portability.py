import copy
import json
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import assessment_portability as portability
import app as app_module
from werkzeug.datastructures import MultiDict


def valid_sections() -> tuple[dict, dict, dict]:
    assessment = {
        "name": "Alpha migration",
        "notes": "Portable review notes.",
        "customer_name": "Alpha Customer",
        "saved_at": "2026-07-03T10:00:00",
        "updated_at": "2026-07-04T09:30:00",
        "selected_currency": "EUR",
        "app_state": {
            "selected_vm_names": ["app-01", "db-01"],
            "step4_hybrid_placements": {"app-01": "native", "db-01": "ocvs"},
            "step4_iaas_discount_pct": 12.5,
        },
        "step4_snapshot": {
            "saved_at": "2026-07-04T09:30:00",
            "vm_settings": {
                "app-01": {
                    "selected": True,
                    "oci_shape": "VM.Standard.E5.Flex",
                    "ocpu": 2,
                    "burst": "100%",
                    "vpu": 20,
                    "os_license": "BYOL",
                    "hybrid_placement": "native",
                }
            },
        },
    }
    inventory = {
        "source_file_name": "alpha_inventory.csv",
        "source_label": "Normalized VM inventory",
        "import_summary": {"vm_count": 2, "warning_messages": []},
        "rows": [
            {
                "name": "app-01",
                "source_name": "app-01",
                "duplicate_index": 1,
                "power_state": "On",
                "raw_os": "Oracle Linux 8 (64-bit)",
                "mapped_os": "Oracle Linux 8",
                "cpus": "4",
                "memory_mb": "8192",
                "provisioned_mib": "102400",
            },
            {
                "name": "db-01",
                "source_name": "db-01",
                "duplicate_index": 1,
                "power_state": "Off",
                "raw_os": "Red Hat Enterprise Linux 8 (64-bit)",
                "mapped_os": "Red Hat Enterprise Linux 8",
                "cpus": 8,
                "memory_mb": 16384,
                "provisioned_mib": 512000,
            },
        ],
    }
    pricing = {
        "currency": "EUR",
        "source_file_name": "oci_pricing_EUR.json",
        "document": {
            "items": [
                {
                    "displayName": "Compute - Standard - E5 - OCPU",
                    "currencyCodeLocalizations": [
                        {
                            "currencyCode": "EUR",
                            "prices": [
                                {"model": "PAY_AS_YOU_GO", "value": 0.031}
                            ],
                        }
                    ],
                }
            ]
        },
    }
    return assessment, inventory, pricing


def valid_package() -> dict:
    assessment, inventory, pricing = valid_sections()
    return portability.build_portable_package(
        assessment,
        inventory,
        pricing,
        exported_at="2026-07-04T10:15:00Z",
        source={"assessment_id": "alpha_local_id", "application_schema_version": 1},
    )


def price_document() -> dict:
    return valid_sections()[2]["document"]


@contextmanager
def isolated_portability_client():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        downloads = root / "downloads"
        app_state = root / "app_state"
        rvtools = root / "rvtools"
        downloads.mkdir()
        app_state.mkdir()
        rvtools.mkdir()
        inventory_path = rvtools / "portable_inventory.csv"
        inventory_path.write_text(
            "VM,Powerstate,OS according to the configuration file,CPUs,Memory,Provisioned MiB\n"
            "app-01,poweredOn,Oracle Linux 8 (64-bit),4,8192,102400\n"
            "db-01,poweredOff,Red Hat Enterprise Linux 8 (64-bit),8,16384,512000\n",
            encoding="utf-8",
        )
        pricing_path = downloads / "oci_pricing_EUR_portable.json"
        pricing_path.write_text(
            json.dumps(price_document(), indent=2),
            encoding="utf-8",
        )
        state_id = "portable_test_state"
        state = app_module._default_app_state()
        state.update(
            selected_vm_names=["app-01", "db-01"],
            step4_hybrid_placements={"app-01": "native", "db-01": "ocvs"},
            step4_iaas_discount_pct=12.5,
            step4_ocvs_commitment_term="3_year",
            acknowledged_warning_ids=["unsupported-native"],
            assessor_recommendation="hybrid",
            assessor_recommendation_rationale="Retain the database on OCVS.",
        )
        (app_state / f"{state_id}.json").write_text(
            json.dumps(state, indent=2),
            encoding="utf-8",
        )
        step4_snapshot = {
            "saved_at": "2026-07-04T10:00:00",
            "source_vinfo_csv": str(inventory_path).replace("\\", "/"),
            "vm_settings": {
                "app-01": {
                    "selected": True,
                    "oci_shape": "VM.Standard.E5.Flex",
                    "ocpu": 2,
                    "burst": "100%",
                    "vpu": 20,
                    "os_license": "BYOL",
                    "hybrid_placement": "native",
                }
            },
            "ocvs_commitment_term": "3_year",
        }
        (app_state / f"{state_id}_step4_snapshot.json").write_text(
            json.dumps(step4_snapshot, indent=2),
            encoding="utf-8",
        )

        with (
            patch.object(app_module, "DOWNLOADS_DIR", downloads),
            patch.object(app_module, "APP_STATE_DIR", app_state),
            patch.object(app_module, "RVTOOLS_DIR", rvtools),
        ):
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
                    sess["state_id"] = state_id
                    sess["selected_rvtools_file"] = str(inventory_path).replace(
                        "\\", "/"
                    )
                    sess["rvtools_file_info"] = {
                        "file_path": str(inventory_path).replace("\\", "/"),
                        "file_name": inventory_path.name,
                    }
                    sess["selected_pricelist_file"] = str(pricing_path).replace(
                        "\\", "/"
                    )
                    sess["selected_currency"] = "EUR"
                    sess["customer_name"] = "Alpha Customer"
                    sess["active_assessment_name"] = "Alpha / Migration"
                    sess["active_assessment_notes"] = "Portable route notes."
                yield {
                    "client": client,
                    "downloads": downloads,
                    "app_state": app_state,
                    "inventory_path": inventory_path,
                    "pricing_path": pricing_path,
                    "state_id": state_id,
                }


class PortableAssessmentTests(unittest.TestCase):
    def test_public_constants_and_package_envelope(self) -> None:
        package = valid_package()

        self.assertEqual("vmware_to_oci_assessment", portability.PACKAGE_TYPE)
        self.assertEqual(1, portability.SCHEMA_VERSION)
        self.assertEqual(25 * 1024 * 1024, portability.MAX_PACKAGE_BYTES)
        self.assertEqual(100000, portability.MAX_VM_ROWS)
        self.assertEqual(4000, portability.MAX_TEXT_LENGTH)
        self.assertEqual(portability.PACKAGE_TYPE, package["package_type"])
        self.assertEqual(portability.SCHEMA_VERSION, package["schema_version"])
        self.assertEqual("2026-07-04T10:15:00Z", package["exported_at"])
        self.assertEqual({"assessment", "inventory", "pricing"}, {
            key for key in package if key in {"assessment", "inventory", "pricing"}
        })

    def test_dumps_is_deterministic_utf8_json(self) -> None:
        package = valid_package()
        package["assessment"]["notes"] = "Sizing reviewed in España."

        first = portability.dumps_portable_package(package)
        second = portability.dumps_portable_package(copy.deepcopy(package))

        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))
        self.assertIn("España", first)
        self.assertNotIn("\\u00f1", first)
        self.assertEqual(package, json.loads(first))

    def test_build_normalizes_inventory_numbers(self) -> None:
        package = valid_package()

        row = package["inventory"]["rows"][0]
        self.assertEqual(4, row["cpus"])
        self.assertEqual(8192, row["memory_mb"])
        self.assertEqual(102400, row["provisioned_mib"])

    def test_requires_all_three_sections(self) -> None:
        for section in ("assessment", "inventory", "pricing"):
            with self.subTest(section=section):
                package = valid_package()
                package.pop(section)
                with self.assertRaisesRegex(
                    portability.PortableAssessmentError,
                    rf"{section}.*required",
                ):
                    portability.validate_portable_package(package)

    def test_rejects_wrong_package_type_and_schema_version(self) -> None:
        cases = (
            ("package_type", "different_package", "package type"),
            ("schema_version", 2, "schema version"),
            ("schema_version", True, "schema version"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                package = valid_package()
                package[field] = value
                with self.assertRaisesRegex(
                    portability.PortableAssessmentError,
                    message,
                ):
                    portability.validate_portable_package(package)

    def test_rejects_duplicate_normalized_vm_names(self) -> None:
        package = valid_package()
        duplicate = copy.deepcopy(package["inventory"]["rows"][0])
        duplicate["name"] = " APP-01 "
        package["inventory"]["rows"].append(duplicate)

        with self.assertRaisesRegex(
            portability.PortableAssessmentError,
            "unique",
        ):
            portability.validate_portable_package(package)

    def test_rejects_negative_numbers_in_supported_sections(self) -> None:
        mutations = (
            lambda package: package["inventory"]["rows"][0].update(cpus=-1),
            lambda package: package["assessment"]["app_state"].update(
                step4_iaas_discount_pct=-0.1
            ),
            lambda package: package["pricing"]["document"]["items"][0][
                "currencyCodeLocalizations"
            ][0]["prices"][0].update(value=-0.01),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                package = valid_package()
                mutate(package)
                with self.assertRaisesRegex(
                    portability.PortableAssessmentError,
                    "negative",
                ):
                    portability.validate_portable_package(package)

    def test_formula_marker_text_roundtrips_as_ordinary_portable_text(self) -> None:
        for marker in "=+-@":
            with self.subTest(marker=marker):
                package = valid_package()
                package["assessment"]["name"] = f"{marker}literal name"
                package["assessment"]["notes"] = f"{marker}literal notes"
                package["assessment"]["app_state"][
                    "assessor_recommendation_rationale"
                ] = f"{marker}literal rationale"
                package["inventory"]["rows"][0].update(
                    name=f"{marker}literal VM",
                    source_name=f"{marker}literal source VM",
                    raw_os=f"{marker}literal operating system",
                    cpus="+4",
                )

                validated = portability.validate_portable_package(package)

                self.assertEqual(
                    f"{marker}literal name",
                    validated["assessment"]["name"],
                )
                self.assertEqual(
                    f"{marker}literal notes",
                    validated["assessment"]["notes"],
                )
                self.assertEqual(
                    f"{marker}literal rationale",
                    validated["assessment"]["app_state"][
                        "assessor_recommendation_rationale"
                    ],
                )
                self.assertEqual(
                    f"{marker}literal VM",
                    validated["inventory"]["rows"][0]["name"],
                )
                self.assertEqual(4, validated["inventory"]["rows"][0]["cpus"])

    def test_rejects_noncanonical_or_out_of_domain_assessment_state(self) -> None:
        def all_currencies(package: dict, value: str) -> None:
            package["assessment"]["selected_currency"] = value
            package["pricing"]["currency"] = value
            package["pricing"]["document"]["items"][0][
                "currencyCodeLocalizations"
            ][0]["currencyCode"] = value

        cases = (
            ("unsupported currency", lambda package: all_currencies(package, "CAD")),
            (
                "noncanonical currency",
                lambda package: all_currencies(package, "eur"),
            ),
            (
                "recommendation enum",
                lambda package: package["assessment"]["app_state"].update(
                    assessor_recommendation="maybe"
                ),
            ),
            (
                "lossy rationale whitespace",
                lambda package: package["assessment"]["app_state"].update(
                    assessor_recommendation_rationale="Review this. "
                ),
            ),
            (
                "warning id syntax",
                lambda package: package["assessment"]["app_state"].update(
                    acknowledged_warning_ids=["Not Canonical"]
                ),
            ),
            (
                "duplicate warning ids",
                lambda package: package["assessment"]["app_state"].update(
                    acknowledged_warning_ids=["review", "review"]
                ),
            ),
            (
                "placement enum",
                lambda package: package["assessment"]["app_state"].update(
                    step4_hybrid_placements={"app-01": "Native"}
                ),
            ),
            (
                "burst enum",
                lambda package: package["assessment"]["app_state"].update(
                    step4_vm_bursts={"app-01": "75%"}
                ),
            ),
            (
                "license enum",
                lambda package: package["assessment"]["app_state"].update(
                    step4_vm_os_license={"app-01": "included"}
                ),
            ),
            (
                "profile enum",
                lambda package: package["assessment"]["app_state"].update(
                    step4_ocvs_profile="BM.Unknown"
                ),
            ),
            (
                "commitment canonical enum",
                lambda package: package["assessment"]["app_state"].update(
                    step4_ocvs_commitment_term="1yr"
                ),
            ),
            (
                "discount range",
                lambda package: package["assessment"]["app_state"].update(
                    step4_iaas_discount_pct=100.01
                ),
            ),
            (
                "finite discount",
                lambda package: package["assessment"]["app_state"].update(
                    step4_iaas_discount_pct=float("nan")
                ),
            ),
            (
                "numeric JSON type",
                lambda package: package["assessment"]["app_state"].update(
                    step4_iaas_discount_pct="12.5"
                ),
            ),
            (
                "license price range",
                lambda package: package["assessment"]["app_state"].update(
                    step4_vmware_license_price_per_core_yearly=1_000_000.01
                ),
            ),
            (
                "DR node enum",
                lambda package: package["assessment"]["app_state"].update(
                    step4_ocvs_dr_nodes=3
                ),
            ),
            (
                "whole OCPU",
                lambda package: package["assessment"]["app_state"].update(
                    step4_vm_ocpus={"app-01": 1.5}
                ),
            ),
            (
                "VPU options",
                lambda package: package["assessment"]["app_state"].update(
                    step4_vm_vpus={"app-01": 15}
                ),
            ),
            (
                "complete policy",
                lambda package: package["assessment"]["app_state"].update(
                    step4_ocvs_policy={"vcpu_per_ocpu": 4}
                ),
            ),
            (
                "policy range",
                lambda package: package["assessment"]["app_state"].update(
                    step4_ocvs_policy={
                        **app_module.OCVS_DEFAULT_SIZING_POLICY,
                        "cpu_headroom_pct": 91,
                    }
                ),
            ),
            (
                "policy whole VPU",
                lambda package: package["assessment"]["app_state"].update(
                    step4_ocvs_policy={
                        **app_module.OCVS_DEFAULT_SIZING_POLICY,
                        "standard_storage_vpu": 10.5,
                    }
                ),
            ),
            (
                "snapshot burst enum",
                lambda package: package["assessment"]["step4_snapshot"][
                    "vm_settings"
                ]["app-01"].update(burst="75%"),
            ),
            (
                "snapshot license enum",
                lambda package: package["assessment"]["step4_snapshot"][
                    "vm_settings"
                ]["app-01"].update(os_license="invalid"),
            ),
            (
                "snapshot placement enum",
                lambda package: package["assessment"]["step4_snapshot"][
                    "vm_settings"
                ]["app-01"].update(hybrid_placement="elsewhere"),
            ),
            (
                "snapshot whole OCPU",
                lambda package: package["assessment"]["step4_snapshot"][
                    "vm_settings"
                ]["app-01"].update(ocpu=2.5),
            ),
            (
                "snapshot VPU options",
                lambda package: package["assessment"]["step4_snapshot"][
                    "vm_settings"
                ]["app-01"].update(vpu=15),
            ),
        )

        for label, mutate in cases:
            with self.subTest(label=label):
                package = valid_package()
                mutate(package)
                with self.assertRaises(portability.PortableAssessmentError):
                    portability.validate_portable_package(package)

    def test_valid_full_app_state_is_unchanged_by_application_normalization(self) -> None:
        package = valid_package()
        state = app_module._default_app_state()
        state.update(
            selected_vm_names=["app-01", "db-01"],
            acknowledged_warning_ids=["unsupported-native"],
            assessor_recommendation="hybrid",
            assessor_recommendation_rationale="Retain db-01 on OCVS.",
            step4_os_shapes={"Oracle Linux 8": "VM.Standard.E5.Flex"},
            step4_vm_shapes={"app-01": "VM.Standard.E5.Flex"},
            step4_vm_ocpus={"app-01": 2},
            step4_vm_bursts={"app-01": "100%"},
            step4_vm_vpus={"app-01": 20},
            step4_vm_os_license={"app-01": "BYOL"},
            step4_hybrid_placements={"app-01": "native", "db-01": "ocvs"},
            step4_iaas_discount_pct=12.5,
            step4_ocvs_profile="best_fit",
            step4_ocvs_policy=dict(app_module.OCVS_DEFAULT_SIZING_POLICY),
            step4_ocvs_commitment_term="3_year",
            step4_vmware_license_price_per_core_yearly=3500.0,
            step4_ocvs_dr_nodes=1,
            step4_last_updated_at="2026-07-04T09:30:00",
        )
        package["assessment"]["app_state"] = state

        validated = portability.validate_portable_package(package)
        validated_state = validated["assessment"]["app_state"]

        self.assertEqual(
            validated_state,
            app_module.normalize_app_state(validated_state),
        )

    def test_optimized3_ocvs_profile_roundtrips_in_portable_state(self) -> None:
        package = valid_package()
        package["assessment"]["app_state"].update(
            step4_ocvs_profile="BM.Optimized3.36",
            step4_hybrid_ocvs_profile="BM.Optimized3.36",
        )
        package["assessment"]["step4_snapshot"].update(
            ocvs_profile="BM.Optimized3.36",
            hybrid_ocvs_profile="BM.Optimized3.36",
        )

        validated = portability.validate_portable_package(package)

        self.assertEqual(
            "BM.Optimized3.36",
            validated["assessment"]["app_state"]["step4_ocvs_profile"],
        )
        self.assertEqual(
            "BM.Optimized3.36",
            validated["assessment"]["app_state"]["step4_hybrid_ocvs_profile"],
        )
        self.assertEqual(
            "BM.Optimized3.36",
            validated["assessment"]["step4_snapshot"]["ocvs_profile"],
        )
        self.assertEqual(
            "BM.Optimized3.36",
            validated["assessment"]["step4_snapshot"]["hybrid_ocvs_profile"],
        )

    def test_only_trusted_workbook_formula_wrapper_emits_formula_xml(self) -> None:
        workbook = app_module._build_xlsx_workbook_bytes(
            [
                {
                    "name": "Proof",
                    "rows": [
                        [
                            "=literal equals",
                            "+literal plus",
                            "-literal minus",
                            "@literal at",
                            app_module._xlsx_formula("1+1"),
                        ]
                    ],
                }
            ],
            currency_fmt_code='"USD" #,##0.00',
        )

        with zipfile.ZipFile(BytesIO(workbook)) as archive:
            worksheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

        self.assertEqual(1, worksheet.count("<f>"))
        self.assertIn("<f>1+1</f>", worksheet)
        for literal in (
            "=literal equals",
            "+literal plus",
            "-literal minus",
            "@literal at",
        ):
            self.assertIn(f"<t>{literal}</t>", worksheet)

    def test_rejects_oversized_strings_anywhere_in_supported_sections(self) -> None:
        mutations = (
            lambda package, text: package["assessment"].update(notes=text),
            lambda package, text: package["inventory"]["rows"][0].update(
                raw_os=text
            ),
            lambda package, text: package["pricing"]["document"]["items"][0].update(
                displayName=text
            ),
        )
        oversized = "x" * (portability.MAX_TEXT_LENGTH + 1)
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                package = valid_package()
                mutate(package, oversized)
                with self.assertRaisesRegex(
                    portability.PortableAssessmentError,
                    "maximum length",
                ):
                    portability.validate_portable_package(package)

    def test_rejects_more_than_maximum_vm_rows(self) -> None:
        package = valid_package()
        package["inventory"]["rows"] = package["inventory"]["rows"] * 2

        with patch.object(portability, "MAX_VM_ROWS", 3):
            with self.assertRaisesRegex(
                portability.PortableAssessmentError,
                "too many VM rows",
            ):
                portability.validate_portable_package(package)

    def test_nonempty_pricing_requires_complete_loader_schema(self) -> None:
        def item(package: dict) -> dict:
            return package["pricing"]["document"]["items"][0]

        cases = (
            ("empty item", lambda package: package["pricing"]["document"].update(items=[{}])),
            (
                "non-object item",
                lambda package: package["pricing"]["document"].update(
                    items=["invalid"]
                ),
            ),
            (
                "missing pricing currency",
                lambda package: package["pricing"].update(currency=""),
            ),
            ("missing display name", lambda package: item(package).pop("displayName")),
            ("empty display name", lambda package: item(package).update(displayName="")),
            ("non-text display name", lambda package: item(package).update(displayName=42)),
            (
                "missing localizations",
                lambda package: item(package).pop("currencyCodeLocalizations"),
            ),
            (
                "empty localizations",
                lambda package: item(package).update(currencyCodeLocalizations=[]),
            ),
            (
                "non-array localizations",
                lambda package: item(package).update(
                    currencyCodeLocalizations={}
                ),
            ),
            (
                "non-object localization",
                lambda package: item(package).update(
                    currencyCodeLocalizations=["invalid"]
                ),
            ),
            (
                "malformed localization",
                lambda package: item(package).update(currencyCodeLocalizations=[{}]),
            ),
            (
                "missing currency",
                lambda package: item(package)["currencyCodeLocalizations"][0].pop(
                    "currencyCode"
                ),
            ),
            (
                "invalid currency",
                lambda package: item(package)["currencyCodeLocalizations"][0].update(
                    currencyCode="EU"
                ),
            ),
            (
                "non-text currency",
                lambda package: item(package)["currencyCodeLocalizations"][0].update(
                    currencyCode=42
                ),
            ),
            (
                "mismatched currency",
                lambda package: item(package)["currencyCodeLocalizations"][0].update(
                    currencyCode="USD"
                ),
            ),
            (
                "missing prices",
                lambda package: item(package)["currencyCodeLocalizations"][0].pop(
                    "prices"
                ),
            ),
            (
                "empty prices",
                lambda package: item(package)["currencyCodeLocalizations"][0].update(
                    prices=[]
                ),
            ),
            (
                "non-array prices",
                lambda package: item(package)["currencyCodeLocalizations"][0].update(
                    prices={}
                ),
            ),
            (
                "non-object price",
                lambda package: item(package)["currencyCodeLocalizations"][0].update(
                    prices=["invalid"]
                ),
            ),
            (
                "malformed price",
                lambda package: item(package)["currencyCodeLocalizations"][0].update(
                    prices=[{}]
                ),
            ),
            (
                "missing model",
                lambda package: item(package)["currencyCodeLocalizations"][0][
                    "prices"
                ][0].pop("model"),
            ),
            (
                "empty model",
                lambda package: item(package)["currencyCodeLocalizations"][0][
                    "prices"
                ][0].update(model=""),
            ),
            (
                "non-text model",
                lambda package: item(package)["currencyCodeLocalizations"][0][
                    "prices"
                ][0].update(model=42),
            ),
            (
                "missing value",
                lambda package: item(package)["currencyCodeLocalizations"][0][
                    "prices"
                ][0].pop("value"),
            ),
            (
                "non-number value",
                lambda package: item(package)["currencyCodeLocalizations"][0][
                    "prices"
                ][0].update(value="0.031"),
            ),
            (
                "boolean value",
                lambda package: item(package)["currencyCodeLocalizations"][0][
                    "prices"
                ][0].update(value=True),
            ),
            (
                "NaN value",
                lambda package: item(package)["currencyCodeLocalizations"][0][
                    "prices"
                ][0].update(value=float("nan")),
            ),
            (
                "negative value",
                lambda package: item(package)["currencyCodeLocalizations"][0][
                    "prices"
                ][0].update(value=-0.031),
            ),
        )

        for label, mutate in cases:
            with self.subTest(label=label):
                package = valid_package()
                mutate(package)
                with self.assertRaises(portability.PortableAssessmentError):
                    portability.validate_portable_package(package)

    def test_empty_pricing_items_are_valid_without_currency(self) -> None:
        package = valid_package()
        package["pricing"]["document"] = {"items": []}

        validated = portability.validate_portable_package(package)

        self.assertEqual("", validated["assessment"]["selected_currency"])
        self.assertEqual("", validated["pricing"]["currency"])
        self.assertEqual({"items": []}, validated["pricing"]["document"])

    def test_nonempty_pricing_requires_matching_assessment_currency(self) -> None:
        for selected_currency in ("", "USD"):
            with self.subTest(selected_currency=selected_currency):
                package = valid_package()
                package["assessment"]["selected_currency"] = selected_currency

                with self.assertRaisesRegex(
                    portability.PortableAssessmentError,
                    "assessment.selected_currency",
                ):
                    portability.validate_portable_package(package)

        matched = portability.validate_portable_package(valid_package())
        self.assertEqual("EUR", matched["assessment"]["selected_currency"])
        self.assertEqual("EUR", matched["pricing"]["currency"])
        self.assertEqual(
            "EUR",
            matched["pricing"]["document"]["items"][0][
                "currencyCodeLocalizations"
            ][0]["currencyCode"],
        )

    def test_rejects_serialized_package_over_size_limit(self) -> None:
        package = valid_package()

        with patch.object(portability, "MAX_PACKAGE_BYTES", 100):
            with self.assertRaisesRegex(
                portability.PortableAssessmentError,
                "25 MiB|size limit",
            ):
                portability.dumps_portable_package(package)

    def test_ignores_local_paths_without_filesystem_access(self) -> None:
        assessment, inventory, pricing = valid_sections()
        assessment.update(
            selected_rvtools_file="/private/inventory.csv",
            selected_pricelist_file="/private/pricing.json",
            last_export_file="/private/report.xlsx",
        )
        assessment["step4_snapshot"]["source_vinfo_csv"] = "/private/inventory.csv"
        inventory["source_path"] = "/private/inventory.csv"
        pricing["source_path"] = "/private/pricing.json"

        with patch("builtins.open", side_effect=AssertionError("filesystem accessed")):
            package = portability.build_portable_package(
                assessment,
                inventory,
                pricing,
                exported_at="2026-07-04T10:15:00Z",
            )
            validated = portability.validate_portable_package(package)

        serialized = portability.dumps_portable_package(validated)
        self.assertNotIn("/private/", serialized)
        self.assertNotIn("selected_rvtools_file", serialized)
        self.assertNotIn("selected_pricelist_file", serialized)
        self.assertNotIn("last_export_file", serialized)
        self.assertNotIn("source_vinfo_csv", serialized)

    def test_nested_allowlists_drop_unknown_paths_ids_and_export_fields(self) -> None:
        package = valid_package()
        package["unexpected_root"] = {"inventory_path": "/private/root.csv"}
        package["source"].update(
            assessment_id="sender-local-id",
            local_id="sender-local-id-2",
            generated_export_path="/private/source-export.xlsx",
            arbitrary_nested={"keep_me": False},
        )
        assessment = package["assessment"]
        assessment.update(
            inventory_path="/private/assessment-inventory.csv",
            generated_export_path="/private/assessment-export.xlsx",
            arbitrary_nested={"unknown": "drop"},
        )
        app_state = assessment["app_state"]
        app_state.update(
            step4_vm_shapes={"app-01": "VM.Standard.E5.Flex"},
            step4_ocvs_policy={
                "vcpu_per_ocpu": 4.0,
                "cpu_headroom_pct": 20.0,
                "memory_headroom_pct": 20.0,
                "storage_headroom_pct": 25.0,
                "dense_vsan_usable_pct": 50.0,
                "standard_storage_vpu": 10,
                "inventory_path": "/private/policy-inventory.csv",
                "unknown_policy": 99,
            },
            assessor_recommendation="hybrid",
            assessor_recommendation_rationale="Retain the database on OCVS.",
            inventory_path="/private/app-state-inventory.csv",
            generated_export_path="/private/app-state-export.xlsx",
            arbitrary_nested={"nested": {"unknown": True}},
        )
        snapshot = assessment["step4_snapshot"]
        snapshot.update(
            source_vinfo_csv="/private/source.csv",
            inventory_path="/private/snapshot-inventory.csv",
            generated_export_path="/private/snapshot-export.xlsx",
            arbitrary_nested={"unknown": "drop"},
        )
        snapshot["vm_settings"]["app-01"].update(
            inventory_path="/private/vm-inventory.csv",
            generated_export_path="/private/vm-export.xlsx",
            arbitrary_nested={"unknown": "drop"},
        )
        package["inventory"].update(
            inventory_path="/private/inventory-section.csv",
            generated_export_path="/private/inventory-export.xlsx",
            arbitrary_nested={"unknown": "drop"},
        )
        package["inventory"]["import_summary"].update(
            inventory_path="/private/summary-inventory.csv",
            generated_export_path="/private/summary-export.xlsx",
            arbitrary_nested={"unknown": "drop"},
        )
        package["inventory"]["rows"][0].update(
            inventory_path="/private/row-inventory.csv",
            generated_export_path="/private/row-export.xlsx",
            arbitrary_nested={"unknown": "drop"},
        )
        document = package["pricing"]["document"]
        document.update(
            inventory_path="/private/pricing-inventory.csv",
            generated_export_path="/private/pricing-export.xlsx",
            arbitrary_nested={"unknown": "drop"},
        )
        price_item = document["items"][0]
        price_item["arbitrary_nested"] = {"unknown": "drop"}
        localization = price_item["currencyCodeLocalizations"][0]
        localization["inventory_path"] = "/private/localization.csv"
        localization["prices"][0]["generated_export_path"] = "/private/price.xlsx"

        validated = portability.validate_portable_package(package)
        serialized = portability.dumps_portable_package(validated)

        self.assertEqual(
            {"application_schema_version": 1},
            validated["source"],
        )
        self.assertEqual(
            {
                "selected_vm_names",
                "step4_hybrid_placements",
                "step4_iaas_discount_pct",
                "step4_vm_shapes",
                "step4_ocvs_policy",
                "assessor_recommendation",
                "assessor_recommendation_rationale",
            },
            set(validated["assessment"]["app_state"]),
        )
        self.assertEqual(
            {
                "vcpu_per_ocpu",
                "cpu_headroom_pct",
                "memory_headroom_pct",
                "storage_headroom_pct",
                "dense_vsan_usable_pct",
                "standard_storage_vpu",
            },
            set(
                validated["assessment"]["app_state"]["step4_ocvs_policy"]
            ),
        )
        self.assertEqual(
            {"saved_at", "vm_settings"},
            set(validated["assessment"]["step4_snapshot"]),
        )
        self.assertEqual(
            {
                "selected",
                "oci_shape",
                "ocpu",
                "burst",
                "vpu",
                "os_license",
                "hybrid_placement",
            },
            set(
                validated["assessment"]["step4_snapshot"]["vm_settings"][
                    "app-01"
                ]
            ),
        )
        self.assertEqual(
            {"vm_count", "warning_messages"},
            set(validated["inventory"]["import_summary"]),
        )
        self.assertEqual(
            {"items"},
            set(validated["pricing"]["document"]),
        )
        self.assertEqual(
            {"displayName", "currencyCodeLocalizations"},
            set(validated["pricing"]["document"]["items"][0]),
        )
        for forbidden in (
            "inventory_path",
            "generated_export_path",
            "local_id",
            "assessment_id",
            "arbitrary_nested",
            "/private/",
        ):
            self.assertNotIn(forbidden, serialized)


class PortableAssessmentRouteTests(unittest.TestCase):
    @staticmethod
    def _file_tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_portable_import_forms_use_dedicated_endpoint(self) -> None:
        with isolated_portability_client() as fixture:
            response = fixture["client"].get("/")

        self.assertEqual(200, response.status_code)
        self.assertNotIn(b'action="/assessment/import"', response.data)
        with app_module.app.test_request_context("/step4?tab=price"):
            template = app_module.app.jinja_env.get_template("_export_center.html")
            export_center = template.render(
                results={
                    "customer_ready_export": False,
                    "assessment_name": "Alpha migration",
                    "assessment_notes": "Notes",
                    "excel_export_label": "Export Draft",
                }
            )
        self.assertIn('action="/assessment/import"', export_center)

    def test_portable_import_rejects_missing_or_oversized_length_before_parsing(
        self,
    ) -> None:
        class UnreadableMultipartBody:
            def read(self, *_args: object, **_kwargs: object) -> bytes:
                raise AssertionError("multipart body must not be parsed")

            def readline(self, *_args: object, **_kwargs: object) -> bytes:
                raise AssertionError("multipart body must not be parsed")

        cases = (
            {},
            {"CONTENT_LENGTH": str(app_module.MAX_PORTABLE_REQUEST_BYTES + 1)},
        )
        for environ_overrides in cases:
            overrides = {
                "wsgi.input": UnreadableMultipartBody(),
                **environ_overrides,
            }
            with self.subTest(environ_overrides=environ_overrides):
                with app_module.app.test_request_context(
                    "/assessment/import",
                    method="POST",
                    content_type="multipart/form-data; boundary=portable",
                    environ_overrides=overrides,
                ):
                    if not environ_overrides:
                        app_module.request.environ.pop("CONTENT_LENGTH", None)
                    response = app_module.import_assessment_route()

                self.assertEqual(303, response.status_code)

    def test_large_nonportable_index_post_keeps_global_upload_allowance(self) -> None:
        payload = b"x" * (27 * 1024 * 1024)
        with isolated_portability_client() as fixture:
            response = fixture["client"].post(
                "/",
                data={
                    "action": "upload_rvtools_file",
                    "inventory_mode": "upload",
                    "rvtools_upload": (
                        BytesIO(payload),
                        "large-inventory.xlsx",
                    ),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )

        self.assertEqual(200, response.status_code)
        self.assertIn(b"Input not accepted for sizing", response.data)
        self.assertNotIn(b"Portable assessment upload exceeds", response.data)

    def test_imported_formula_marker_text_stays_literal_in_workbook_xml(self) -> None:
        package = valid_package()
        package["assessment"]["name"] = "=SUM(1,1)"
        package["assessment"]["app_state"][
            "assessor_recommendation_rationale"
        ] = "+CMD(1)"
        package["inventory"]["rows"][0].update(
            name="@SUM(A1:A2)",
            source_name="@SUM(A1:A2)",
        )

        with isolated_portability_client() as fixture:
            response = fixture["client"].post(
                "/assessment/import",
                data={
                    "action": "import_assessment",
                    "assessment_file": (
                        BytesIO(
                            portability.dumps_portable_package(package).encode(
                                "utf-8"
                            )
                        ),
                        "literal-text.json",
                    ),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            with fixture["client"].session_transaction() as sess:
                imported_name = str(sess.get("active_assessment_name", ""))
                imported_inventory_path = str(sess.get("selected_rvtools_file", ""))
            imported_state = app_module.load_app_state()
            imported_rows, _ = app_module.load_vms_from_vinfo(
                imported_inventory_path
            )

        workbook = app_module._build_xlsx_workbook_bytes(
            [
                {
                    "name": "Proof",
                    "rows": [
                        [
                            imported_name,
                            imported_state["assessor_recommendation_rationale"],
                            imported_rows[0]["name"],
                        ]
                    ],
                }
            ],
            currency_fmt_code='"USD" #,##0.00',
        )
        with zipfile.ZipFile(BytesIO(workbook)) as archive:
            worksheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

        self.assertEqual(200, response.status_code)
        self.assertNotIn("<f>", worksheet)
        self.assertIn("<t>=SUM(1,1)</t>", worksheet)
        self.assertIn("<t>+CMD(1)</t>", worksheet)
        self.assertIn("<t>@SUM(A1:A2)</t>", worksheet)

    def test_post_link_temp_cleanup_failure_does_not_hide_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "published.json"
            real_unlink = Path.unlink

            def fail_temp_unlink(path: Path, *args: object, **kwargs: object) -> None:
                if path.name.startswith(".published.json.") and path.name.endswith(".tmp"):
                    raise OSError("injected post-link cleanup failure")
                real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", autospec=True, side_effect=fail_temp_unlink):
                app_module._write_new_json_atomically(destination, {"published": True})

            self.assertEqual({"published": True}, json.loads(destination.read_text()))

    def test_import_rolls_back_published_snapshot_after_post_link_cleanup_failure(self) -> None:
        with isolated_portability_client() as fixture:
            package_bytes = portability.dumps_portable_package(valid_package()).encode(
                "utf-8"
            )
            real_unlink = Path.unlink

            def fail_temp_unlink(path: Path, *args: object, **kwargs: object) -> None:
                if path.name.endswith(".tmp") and "saved_assessments" in path.parts:
                    raise OSError("injected post-link cleanup failure")
                real_unlink(path, *args, **kwargs)

            with (
                patch.object(
                    Path,
                    "unlink",
                    autospec=True,
                    side_effect=fail_temp_unlink,
                ),
                patch.object(
                    app_module,
                    "load_saved_assessment",
                    return_value={"ok": False, "warnings": []},
                ),
            ):
                response = fixture["client"].post(
                    "/assessment/import",
                    data={
                        "action": "import_assessment",
                        "assessment_file": (BytesIO(package_bytes), "portable.json"),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            saved_dir = fixture["app_state"] / "saved_assessments"
            imported_root = fixture["downloads"] / "imported_assessments"
            self.assertEqual(200, response.status_code)
            self.assertIn(b"could not be loaded after reconstruction", response.data)
            self.assertFalse(saved_dir.exists() and list(saved_dir.glob("*.json")))
            self.assertFalse(imported_root.exists() and any(imported_root.iterdir()))

    def test_save_current_assessment_never_truncates_on_atomic_replace_failure(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            client.post(
                "/",
                data={
                    "action": "save_assessment",
                    "assessment_name": "Atomic save",
                    "assessment_notes": "Original bytes",
                },
            )
            with client.session_transaction() as sess:
                assessment_id = str(sess["active_assessment_id"])
                before_session = copy.deepcopy(dict(sess))
            snapshot_path = (
                fixture["app_state"] / "saved_assessments" / f"{assessment_id}.json"
            )
            before_snapshot = snapshot_path.read_bytes()

            with patch.object(
                app_module.os,
                "replace",
                side_effect=OSError("injected atomic replace failure"),
            ):
                response = client.post(
                    "/",
                    data={
                        "action": "save_assessment",
                        "assessment_name": "Atomic save changed",
                        "assessment_notes": "Must not replace",
                        "customer_name": "Must not partially save",
                    },
                )

            with client.session_transaction() as sess:
                after_session = dict(sess)
            self.assertEqual(200, response.status_code)
            self.assertIn(b"could not be saved", response.data)
            self.assertEqual(before_snapshot, snapshot_path.read_bytes())
            self.assertEqual(before_session, after_session)

    def test_save_current_assessment_restores_new_session_on_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_state = Path(temp_dir) / "app_state"
            with (
                patch.object(app_module, "APP_STATE_DIR", app_state),
                app_module.app.test_request_context("/"),
            ):
                app_module.session["marker"] = "preserve"
                before_session = copy.deepcopy(dict(app_module.session))
                with patch.object(
                    app_module,
                    "_write_json_atomically",
                    side_effect=OSError("injected first save failure"),
                ):
                    with self.assertRaisesRegex(OSError, "first save failure"):
                        app_module.save_current_assessment("First save", "No mutation")

                self.assertEqual(before_session, dict(app_module.session))
                saved_dir = app_state / "saved_assessments"
                self.assertFalse(saved_dir.exists() and list(saved_dir.glob("*.json")))

    def test_saved_export_build_failure_preserves_snapshot_and_session_bytes(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            client.post(
                "/",
                data={
                    "action": "save_assessment",
                    "assessment_name": "Transactional export",
                    "assessment_notes": "Original notes",
                },
            )
            with client.session_transaction() as sess:
                assessment_id = str(sess["active_assessment_id"])
                before_session = copy.deepcopy(dict(sess))
            snapshot_path = (
                fixture["app_state"] / "saved_assessments" / f"{assessment_id}.json"
            )
            before_snapshot = snapshot_path.read_bytes()

            with patch.object(
                app_module,
                "build_portable_package",
                side_effect=portability.PortableAssessmentError("injected build failure"),
            ):
                response = client.post(
                    "/",
                    data={
                        "action": "export_assessment",
                        "assessment_name": "Changed by failed export",
                        "assessment_notes": "Must roll back",
                    },
                )

            with client.session_transaction() as sess:
                after_session = dict(sess)
            self.assertEqual(200, response.status_code)
            self.assertIn(b"injected build failure", response.data)
            self.assertEqual(before_snapshot, snapshot_path.read_bytes())
            self.assertEqual(before_session, after_session)

    def test_saved_export_serialization_failure_preserves_snapshot_and_session_bytes(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            client.post(
                "/",
                data={
                    "action": "save_assessment",
                    "assessment_name": "Transactional export",
                    "assessment_notes": "Original notes",
                },
            )
            with client.session_transaction() as sess:
                assessment_id = str(sess["active_assessment_id"])
                before_session = copy.deepcopy(dict(sess))
            snapshot_path = (
                fixture["app_state"] / "saved_assessments" / f"{assessment_id}.json"
            )
            before_snapshot = snapshot_path.read_bytes()

            with patch.object(
                app_module,
                "dumps_portable_package",
                side_effect=OSError("injected serialization failure"),
            ):
                response = client.post(
                    "/",
                    data={
                        "action": "export_assessment",
                        "assessment_name": "Changed by failed export",
                        "assessment_notes": "Must roll back",
                    },
                )

            with client.session_transaction() as sess:
                after_session = dict(sess)
            self.assertEqual(200, response.status_code)
            self.assertIn(b"could not be exported", response.data)
            self.assertEqual(before_snapshot, snapshot_path.read_bytes())
            self.assertEqual(before_session, after_session)

    def test_saved_export_refresh_write_failure_preserves_snapshot_and_session_bytes(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            client.post(
                "/",
                data={
                    "action": "save_assessment",
                    "assessment_name": "Transactional export",
                    "assessment_notes": "Original notes",
                },
            )
            with client.session_transaction() as sess:
                assessment_id = str(sess["active_assessment_id"])
                before_session = copy.deepcopy(dict(sess))
            snapshot_path = (
                fixture["app_state"] / "saved_assessments" / f"{assessment_id}.json"
            )
            before_snapshot = snapshot_path.read_bytes()
            real_atomic_write = app_module._write_json_atomically

            def fail_snapshot_refresh(path: Path, payload: object) -> None:
                if path == snapshot_path:
                    raise OSError("injected snapshot refresh failure")
                real_atomic_write(path, payload)

            with patch.object(
                app_module,
                "_write_json_atomically",
                side_effect=fail_snapshot_refresh,
            ):
                response = client.post(
                    "/",
                    data={
                        "action": "export_assessment",
                        "assessment_name": "Changed by failed export",
                        "assessment_notes": "Must roll back",
                    },
                )

            with client.session_transaction() as sess:
                after_session = dict(sess)
            self.assertEqual(200, response.status_code)
            self.assertIn(b"could not be exported", response.data)
            self.assertEqual(before_snapshot, snapshot_path.read_bytes())
            self.assertEqual(before_session, after_session)

    def test_failed_import_does_not_clobber_concurrent_preference_update(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            package_bytes = portability.dumps_portable_package(valid_package()).encode(
                "utf-8"
            )
            preferences_path = fixture["app_state"] / "preferences.json"
            preferences_path.write_bytes(b'{"before":"import"}\n')
            concurrent_bytes = b'{"concurrent":"must survive"}\n'

            def fail_after_concurrent_update(*_args: object, **_kwargs: object) -> dict:
                preferences_path.write_bytes(concurrent_bytes)
                raise OSError("injected load failure after concurrent update")

            with patch.object(
                app_module,
                "load_saved_assessment",
                side_effect=fail_after_concurrent_update,
            ):
                response = client.post(
                    "/assessment/import",
                    data={
                        "action": "import_assessment",
                        "assessment_file": (BytesIO(package_bytes), "portable.json"),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            self.assertEqual(200, response.status_code)
            self.assertIn(b"current assessment was kept", response.data)
            self.assertEqual(concurrent_bytes, preferences_path.read_bytes())

    def test_successful_import_merges_preferences_changed_before_cas(self) -> None:
        with isolated_portability_client() as fixture:
            preferences_path = fixture["app_state"] / "preferences.json"
            preferences_path.write_text(
                json.dumps(
                    {
                        "last_selected_pricelist_file": "old.json",
                        "last_selected_currency": "USD",
                        "unrelated": "before",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            real_compare = app_module._compare_and_swap_preferences
            compare_calls = 0

            def concurrent_before_compare(
                expected: tuple[bool, bytes],
                desired: tuple[bool, bytes],
            ) -> bool:
                nonlocal compare_calls
                compare_calls += 1
                if compare_calls == 1:
                    preferences_path.write_text(
                        json.dumps(
                            {
                                "last_selected_pricelist_file": "old.json",
                                "last_selected_currency": "USD",
                                "unrelated": "concurrent",
                                "new_preference": "preserve",
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                return real_compare(expected, desired)

            with patch.object(
                app_module,
                "_compare_and_swap_preferences",
                side_effect=concurrent_before_compare,
            ):
                response = fixture["client"].post(
                    "/assessment/import",
                    data={
                        "action": "import_assessment",
                        "assessment_file": (
                            BytesIO(
                                portability.dumps_portable_package(
                                    valid_package()
                                ).encode("utf-8")
                            ),
                            "portable.json",
                        ),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
            self.assertEqual(200, response.status_code)
            self.assertIn(b"Assessment imported", response.data)
            self.assertGreaterEqual(compare_calls, 2)
            self.assertEqual("concurrent", preferences["unrelated"])
            self.assertEqual("preserve", preferences["new_preference"])
            self.assertEqual("EUR", preferences["last_selected_currency"])
            self.assertIn(
                "/imported_assessments/",
                preferences["last_selected_pricelist_file"],
            )

    def test_failed_import_does_not_restore_over_post_write_preference_update(
        self,
    ) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            preferences_path = fixture["app_state"] / "preferences.json"
            preferences_path.write_bytes(b'{"baseline":"preserve"}\n')
            with client.session_transaction() as sess:
                before_session = copy.deepcopy(dict(sess))
            real_compare = app_module._compare_and_swap_preferences
            concurrent_bytes = b""

            def fail_after_concurrent_write(
                expected: tuple[bool, bytes],
                desired: tuple[bool, bytes],
            ) -> bool:
                nonlocal concurrent_bytes
                published = real_compare(expected, desired)
                if published:
                    concurrent = json.loads(desired[1].decode("utf-8"))
                    concurrent["concurrent_after_write"] = "must survive"
                    concurrent_bytes = json.dumps(
                        concurrent,
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                    preferences_path.write_bytes(concurrent_bytes)
                    raise OSError("injected failure after concurrent preference write")
                return published

            with patch.object(
                app_module,
                "_compare_and_swap_preferences",
                side_effect=fail_after_concurrent_write,
            ):
                response = client.post(
                    "/assessment/import",
                    data={
                        "action": "import_assessment",
                        "assessment_file": (
                            BytesIO(
                                portability.dumps_portable_package(
                                    valid_package()
                                ).encode("utf-8")
                            ),
                            "portable.json",
                        ),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            with client.session_transaction() as sess:
                after_session = dict(sess)
            saved_dir = fixture["app_state"] / "saved_assessments"
            imported_root = fixture["downloads"] / "imported_assessments"
            self.assertEqual(200, response.status_code)
            self.assertIn(b"current assessment was kept", response.data)
            self.assertEqual(before_session, after_session)
            self.assertTrue(concurrent_bytes)
            self.assertEqual(concurrent_bytes, preferences_path.read_bytes())
            self.assertFalse(saved_dir.exists() and list(saved_dir.glob("*.json")))
            self.assertFalse(
                imported_root.exists() and any(imported_root.iterdir())
            )

    def test_failed_preference_publication_restores_exact_prior_bytes(self) -> None:
        with isolated_portability_client() as fixture:
            preferences_path = fixture["app_state"] / "preferences.json"
            prior_bytes = b'{"exact" : "spacing"}\n'
            preferences_path.write_bytes(prior_bytes)
            real_compare = app_module._compare_and_swap_preferences

            def fail_after_publication(
                expected: tuple[bool, bytes],
                desired: tuple[bool, bytes],
            ) -> bool:
                published = real_compare(expected, desired)
                if published:
                    raise OSError("injected preference publication failure")
                return published

            with patch.object(
                app_module,
                "_compare_and_swap_preferences",
                side_effect=fail_after_publication,
            ):
                response = fixture["client"].post(
                    "/assessment/import",
                    data={
                        "action": "import_assessment",
                        "assessment_file": (
                            BytesIO(
                                portability.dumps_portable_package(
                                    valid_package()
                                ).encode("utf-8")
                            ),
                            "portable.json",
                        ),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            self.assertEqual(200, response.status_code)
            self.assertIn(b"current assessment was kept", response.data)
            self.assertEqual(prior_bytes, preferences_path.read_bytes())

    def test_import_rejects_extra_duplicate_files_and_ignored_fields(self) -> None:
        package_bytes = portability.dumps_portable_package(valid_package()).encode(
            "utf-8"
        )
        cases = (
            MultiDict(
                [
                    ("action", "import_assessment"),
                    ("assessment_file", (BytesIO(package_bytes), "portable.json")),
                    ("ignored_file", (BytesIO(b"ignored"), "ignored.txt")),
                ]
            ),
            MultiDict(
                [
                    ("action", "import_assessment"),
                    ("assessment_file", (BytesIO(package_bytes), "one.json")),
                    ("assessment_file", (BytesIO(package_bytes), "two.json")),
                ]
            ),
            MultiDict(
                [
                    ("action", "import_assessment"),
                    ("assessment_file", (BytesIO(package_bytes), "portable.json")),
                    ("ignored_field", "x" * 5000),
                ]
            ),
        )

        for index, data in enumerate(cases):
            with self.subTest(case=index), isolated_portability_client() as fixture:
                response = fixture["client"].post(
                    "/assessment/import",
                    data=data,
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

                imported_root = fixture["downloads"] / "imported_assessments"
                self.assertEqual(200, response.status_code)
                self.assertIn(b"exactly one portable assessment", response.data)
                self.assertFalse(
                    imported_root.exists() and any(imported_root.iterdir())
                )

    def test_symlinked_import_root_is_rejected_without_touching_target(self) -> None:
        with isolated_portability_client() as fixture:
            outside_root = fixture["downloads"].parent / "outside-import-root"
            outside_root.mkdir()
            imported_root = fixture["downloads"] / "imported_assessments"
            imported_root.symlink_to(outside_root, target_is_directory=True)
            before_files = self._file_tree_bytes(fixture["app_state"].parent)

            response = fixture["client"].post(
                "/assessment/import",
                data={
                    "action": "import_assessment",
                    "assessment_file": (
                        BytesIO(
                            portability.dumps_portable_package(valid_package()).encode(
                                "utf-8"
                            )
                        ),
                        "portable.json",
                    ),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )

            self.assertEqual(200, response.status_code)
            self.assertIn(b"cannot be a symlink", response.data)
            self.assertFalse(any(outside_root.iterdir()))
            self.assertEqual(before_files, self._file_tree_bytes(fixture["app_state"].parent))

    def test_empty_pricing_import_clears_prior_selection_and_preferences(self) -> None:
        with isolated_portability_client() as fixture:
            package = valid_package()
            package["pricing"]["document"] = {"items": []}
            preferences_path = fixture["app_state"] / "preferences.json"
            preferences_path.write_text(
                json.dumps(
                    {
                        "last_selected_pricelist_file": str(
                            fixture["pricing_path"]
                        ).replace("\\", "/"),
                        "last_selected_currency": "EUR",
                        "unrelated_preference": "preserve",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            response = fixture["client"].post(
                "/assessment/import",
                data={
                    "action": "import_assessment",
                    "assessment_file": (
                        BytesIO(
                            portability.dumps_portable_package(package).encode(
                                "utf-8"
                            )
                        ),
                        "no-pricing.json",
                    ),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )

            with fixture["client"].session_transaction() as sess:
                imported_session = dict(sess)
            imported_id = str(imported_session.get("active_assessment_id", ""))
            imported_snapshot = json.loads(
                (
                    fixture["app_state"]
                    / "saved_assessments"
                    / f"{imported_id}.json"
                ).read_text(encoding="utf-8")
            )
            preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
            import_dir = fixture["downloads"] / "imported_assessments" / imported_id

            self.assertEqual(200, response.status_code)
            self.assertIn(b"Assessment imported", response.data)
            self.assertNotIn("selected_pricelist_file", imported_session)
            self.assertNotIn("selected_currency", imported_session)
            self.assertEqual("", imported_snapshot["selected_pricelist_file"])
            self.assertEqual("", imported_snapshot["selected_currency"])
            self.assertNotIn("last_selected_pricelist_file", preferences)
            self.assertNotIn("last_selected_currency", preferences)
            self.assertEqual("preserve", preferences["unrelated_preference"])
            self.assertEqual([], list(import_dir.glob("oci_pricing_*.json")))

    def test_malformed_pricing_rejection_preserves_every_local_byte(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            client.post(
                "/",
                data={
                    "action": "save_assessment",
                    "assessment_name": "Preserve pricing baseline",
                    "assessment_notes": "Malformed pricing must not change this.",
                },
            )

            def partial_item(package: dict) -> None:
                package["pricing"]["document"]["items"] = [
                    {"displayName": "Incomplete"}
                ]

            def empty_item(package: dict) -> None:
                package["pricing"]["document"]["items"] = [{}]

            def nan_price(package: dict) -> None:
                package["pricing"]["document"]["items"][0][
                    "currencyCodeLocalizations"
                ][0]["prices"][0]["value"] = float("nan")

            def negative_price(package: dict) -> None:
                package["pricing"]["document"]["items"][0][
                    "currencyCodeLocalizations"
                ][0]["prices"][0]["value"] = -1

            for label, mutate in (
                ("empty item", empty_item),
                ("partial item", partial_item),
                ("NaN price", nan_price),
                ("negative price", negative_price),
            ):
                with self.subTest(label=label):
                    package = valid_package()
                    mutate(package)
                    with client.session_transaction() as sess:
                        before_session = copy.deepcopy(dict(sess))
                    before_files = self._file_tree_bytes(fixture["app_state"].parent)

                    response = client.post(
                        "/assessment/import",
                        data={
                            "action": "import_assessment",
                            "assessment_file": (
                                BytesIO(json.dumps(package).encode("utf-8")),
                                "malformed-pricing.json",
                            ),
                        },
                        content_type="multipart/form-data",
                        follow_redirects=True,
                    )

                    with client.session_transaction() as sess:
                        after_session = dict(sess)
                    self.assertEqual(200, response.status_code)
                    self.assertEqual(before_session, after_session)
                    self.assertEqual(
                        before_files,
                        self._file_tree_bytes(fixture["app_state"].parent),
                    )
                    imported_root = (
                        fixture["downloads"] / "imported_assessments"
                    )
                    self.assertFalse(
                        imported_root.exists() and any(imported_root.iterdir())
                    )

    def test_valid_pricing_materialization_roundtrips_loader_lookup(self) -> None:
        with isolated_portability_client() as fixture:
            package = valid_package()

            response = fixture["client"].post(
                "/assessment/import",
                data={
                    "action": "import_assessment",
                    "assessment_file": (
                        BytesIO(
                            portability.dumps_portable_package(package).encode(
                                "utf-8"
                            )
                        ),
                        "valid-pricing.json",
                    ),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )

            with fixture["client"].session_transaction() as sess:
                imported_path = str(sess.get("selected_pricelist_file", ""))
                imported_currency = str(sess.get("selected_currency", ""))
            lookup, loaded_currency, loaded_source = app_module.load_price_lookup(
                imported_path
            )
            materialized = json.loads(Path(imported_path).read_text(encoding="utf-8"))

            self.assertEqual(200, response.status_code)
            self.assertEqual("EUR", imported_currency)
            self.assertEqual(
                {"Compute - Standard - E5 - OCPU": 0.031},
                lookup,
            )
            self.assertEqual("EUR", loaded_currency)
            self.assertEqual(imported_path, loaded_source)
            self.assertEqual(package["pricing"]["document"], materialized)

    def test_currency_mismatch_rejection_preserves_every_local_byte(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            client.post(
                "/",
                data={
                    "action": "save_assessment",
                    "assessment_name": "Preserve currency baseline",
                    "assessment_notes": "USD and EUR must not be mixed.",
                },
            )
            package = valid_package()
            package["assessment"]["selected_currency"] = "USD"
            with client.session_transaction() as sess:
                before_session = copy.deepcopy(dict(sess))
            before_files = self._file_tree_bytes(fixture["app_state"].parent)

            response = client.post(
                "/assessment/import",
                data={
                    "action": "import_assessment",
                    "assessment_file": (
                        BytesIO(json.dumps(package).encode("utf-8")),
                        "currency-mismatch.json",
                    ),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )

            with client.session_transaction() as sess:
                after_session = dict(sess)
            self.assertEqual(200, response.status_code)
            self.assertIn(b"assessment.selected_currency", response.data)
            self.assertEqual(before_session, after_session)
            self.assertEqual(
                before_files,
                self._file_tree_bytes(fixture["app_state"].parent),
            )
            imported_root = fixture["downloads"] / "imported_assessments"
            self.assertFalse(
                imported_root.exists() and any(imported_root.iterdir())
            )

    def test_materialized_pricing_lookup_mismatch_rolls_back(self) -> None:
        with isolated_portability_client() as fixture:
            package = valid_package()
            with fixture["client"].session_transaction() as sess:
                before_session = copy.deepcopy(dict(sess))
            before_files = self._file_tree_bytes(fixture["app_state"].parent)

            with patch.object(
                app_module,
                "load_price_lookup",
                return_value=({}, "", ""),
            ):
                response = fixture["client"].post(
                    "/assessment/import",
                    data={
                        "action": "import_assessment",
                        "assessment_file": (
                            BytesIO(
                                portability.dumps_portable_package(package).encode(
                                    "utf-8"
                                )
                            ),
                            "lookup-mismatch.json",
                        ),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            with fixture["client"].session_transaction() as sess:
                after_session = dict(sess)
            self.assertEqual(200, response.status_code)
            self.assertIn(b"could not be reconstructed exactly", response.data)
            self.assertEqual(before_session, after_session)
            self.assertEqual(
                before_files,
                self._file_tree_bytes(fixture["app_state"].parent),
            )

    def test_imported_inventory_reload_preserves_every_normalized_field(self) -> None:
        with isolated_portability_client() as fixture:
            assessment, inventory, pricing = valid_sections()
            inventory["rows"] = [
                {
                    "name": "duplicate-app [2]",
                    "source_name": "duplicate-app",
                    "duplicate_index": 2,
                    "power_state": "Unknown",
                    "raw_os": "Vendor Guest OS 9",
                    "mapped_os": "Preserved Mapped OS",
                    "cpus": 3.5,
                    "memory_mb": 6144,
                    "provisioned_mib": 77777,
                }
            ]
            inventory["import_summary"] = {
                "vm_count": 1,
                "duplicate_name_count": 1,
                "duplicate_row_count": 1,
                "warning_messages": ["Duplicate provenance retained."],
            }
            package = portability.build_portable_package(
                assessment,
                inventory,
                pricing,
                exported_at="2026-07-04T10:15:00Z",
            )

            response = fixture["client"].post(
                "/assessment/import",
                data={
                    "action": "import_assessment",
                    "assessment_file": (
                        BytesIO(portability.dumps_portable_package(package).encode("utf-8")),
                        "normalized.json",
                    ),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            with fixture["client"].session_transaction() as sess:
                imported_inventory = str(sess.get("selected_rvtools_file", ""))

            reloaded_rows, reloaded_source = app_module.load_vms_from_vinfo(
                imported_inventory
            )
            generated_payload = json.loads(
                Path(imported_inventory).read_text(encoding="utf-8")
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual(package["inventory"]["rows"], reloaded_rows)
            self.assertEqual(package["inventory"], generated_payload["inventory"])
            self.assertEqual(
                imported_inventory,
                reloaded_source.rsplit("::", 1)[0],
            )

    def test_import_id_allocation_skips_directory_and_snapshot_collisions(self) -> None:
        with isolated_portability_client() as fixture:
            package = valid_package()
            imported_root = fixture["downloads"] / "imported_assessments"
            directory_collision_id = "existing_import_directory"
            directory_collision = imported_root / directory_collision_id
            directory_collision.mkdir(parents=True)
            directory_marker = directory_collision / "preserved.txt"
            directory_marker.write_bytes(b"preserve existing import artifacts")
            snapshot_collision_id = "existing_saved_snapshot"
            saved_dir = fixture["app_state"] / "saved_assessments"
            saved_dir.mkdir()
            snapshot_collision = saved_dir / f"{snapshot_collision_id}.json"
            snapshot_collision.write_bytes(b'{"preserve":"existing snapshot"}\n')
            allocated_id = "collision_free_import"

            with patch.object(
                app_module,
                "_new_assessment_id",
                side_effect=[
                    directory_collision_id,
                    snapshot_collision_id,
                    allocated_id,
                ],
            ) as id_generator:
                response = fixture["client"].post(
                    "/assessment/import",
                    data={
                        "action": "import_assessment",
                        "assessment_file": (
                            BytesIO(
                                portability.dumps_portable_package(package).encode(
                                    "utf-8"
                                )
                            ),
                            "portable.json",
                        ),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            with fixture["client"].session_transaction() as sess:
                active_id = str(sess.get("active_assessment_id", ""))
            self.assertEqual(200, response.status_code)
            self.assertEqual(3, id_generator.call_count)
            self.assertEqual(allocated_id, active_id)
            self.assertEqual(
                b"preserve existing import artifacts",
                directory_marker.read_bytes(),
            )
            self.assertEqual(
                b'{"preserve":"existing snapshot"}\n',
                snapshot_collision.read_bytes(),
            )
            self.assertTrue((imported_root / allocated_id).is_dir())
            self.assertTrue((saved_dir / f"{allocated_id}.json").is_file())

    def test_late_snapshot_collision_is_preserved_and_owned_artifacts_roll_back(self) -> None:
        with isolated_portability_client() as fixture:
            package = valid_package()
            collision_id = "late_snapshot_collision"
            saved_dir = fixture["app_state"] / "saved_assessments"
            saved_dir.mkdir()
            collision_snapshot = saved_dir / f"{collision_id}.json"
            collision_bytes = b'{"preserve":"late collision"}\n'
            imported_root = fixture["downloads"] / "imported_assessments"
            real_writer = app_module._write_imported_inventory

            def create_late_collision(
                file_path: Path,
                inventory: dict,
            ) -> None:
                real_writer(file_path, inventory)
                collision_snapshot.write_bytes(collision_bytes)

            with fixture["client"].session_transaction() as sess:
                before_session = copy.deepcopy(dict(sess))
            with (
                patch.object(
                    app_module,
                    "_new_assessment_id",
                    return_value=collision_id,
                ),
                patch.object(
                    app_module,
                    "_write_imported_inventory",
                    side_effect=create_late_collision,
                ),
            ):
                response = fixture["client"].post(
                    "/assessment/import",
                    data={
                        "action": "import_assessment",
                        "assessment_file": (
                            BytesIO(
                                portability.dumps_portable_package(package).encode(
                                    "utf-8"
                                )
                            ),
                            "portable.json",
                        ),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            with fixture["client"].session_transaction() as sess:
                after_session = dict(sess)
            self.assertEqual(200, response.status_code)
            self.assertIn(b"current assessment was kept", response.data)
            self.assertEqual(before_session, after_session)
            self.assertEqual(collision_bytes, collision_snapshot.read_bytes())
            self.assertFalse((imported_root / collision_id).exists())

    def test_unsaved_export_is_self_contained_without_creating_local_snapshot(self) -> None:
        with isolated_portability_client() as fixture:
            response = fixture["client"].post(
                "/",
                data={
                    "action": "export_assessment",
                    "assessment_name": "Alpha / Migration",
                    "assessment_notes": "Portable route notes.",
                },
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual("application/json", response.mimetype)
            self.assertEqual(
                "application/json; charset=utf-8",
                response.content_type,
            )
            self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
            self.assertRegex(
                response.headers.get("Content-Disposition", ""),
                r"alpha_migration_portable_assessment_\d{8}_\d{6}\.json",
            )
            package = json.loads(response.data.decode("utf-8"))
            self.assertEqual(portability.PACKAGE_TYPE, package["package_type"])
            self.assertEqual(["app-01", "db-01"], [
                row["name"] for row in package["inventory"]["rows"]
            ])
            self.assertEqual("EUR", package["pricing"]["currency"])
            self.assertTrue(package["pricing"]["document"]["items"])
            self.assertEqual(
                "hybrid",
                package["assessment"]["app_state"]["assessor_recommendation"],
            )
            self.assertNotIn("source_vinfo_csv", response.data.decode("utf-8"))
            saved_dir = fixture["app_state"] / "saved_assessments"
            self.assertFalse(saved_dir.exists() and list(saved_dir.glob("*.json")))

    def test_saved_export_refreshes_existing_snapshot_first(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            client.post(
                "/",
                data={
                    "action": "save_assessment",
                    "assessment_name": "Refresh me",
                    "assessment_notes": "Original notes",
                },
            )
            with client.session_transaction() as sess:
                assessment_id = str(sess["active_assessment_id"])
            state = json.loads(
                (fixture["app_state"] / f"{fixture['state_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            state["step4_iaas_discount_pct"] = 18.75
            (fixture["app_state"] / f"{fixture['state_id']}.json").write_text(
                json.dumps(state, indent=2),
                encoding="utf-8",
            )

            response = client.post(
                "/",
                data={
                    "action": "export_assessment",
                    "assessment_name": "Refresh me",
                    "assessment_notes": "Updated before export",
                },
            )

            snapshot_path = (
                fixture["app_state"] / "saved_assessments" / f"{assessment_id}.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(200, response.status_code)
            self.assertEqual("application/json", response.mimetype)
            self.assertEqual("Updated before export", snapshot["notes"])
            self.assertEqual(
                18.75,
                snapshot["app_state"]["step4_iaas_discount_pct"],
            )

    def test_import_reconstructs_dependencies_loads_state_and_suffixes_name(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            export_response = client.post(
                "/",
                data={"action": "export_assessment"},
            )
            portable_bytes = bytes(export_response.data)
            fixture["inventory_path"].unlink()
            fixture["pricing_path"].unlink()

            first_response = client.post(
                "/assessment/import",
                data={
                    "action": "import_assessment",
                    "assessment_file": (BytesIO(portable_bytes), "alpha.json"),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            with client.session_transaction() as sess:
                first_id = str(sess.get("active_assessment_id", ""))
                first_session = dict(sess)
            first_state = json.loads(
                (fixture["app_state"] / f"{fixture['state_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            first_snapshot = json.loads(
                (
                    fixture["app_state"]
                    / "saved_assessments"
                    / f"{first_id}.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(200, first_response.status_code)
            self.assertIn(b"Assessment imported", first_response.data)
            self.assertTrue(first_id)
            self.assertEqual("Alpha / Migration", first_session["active_assessment_name"])
            self.assertEqual("Alpha Customer", first_session["customer_name"])
            self.assertEqual("Portable route notes.", first_session["active_assessment_notes"])
            self.assertEqual("EUR", first_session["selected_currency"])
            self.assertEqual(["app-01", "db-01"], first_state["selected_vm_names"])
            self.assertEqual(
                {"app-01": "native", "db-01": "ocvs"},
                first_state["step4_hybrid_placements"],
            )
            self.assertEqual("3_year", first_state["step4_ocvs_commitment_term"])
            self.assertEqual(
                ["unsupported-native"],
                first_state["acknowledged_warning_ids"],
            )
            self.assertEqual("hybrid", first_state["assessor_recommendation"])
            self.assertEqual(
                "Retain the database on OCVS.",
                first_state["assessor_recommendation_rationale"],
            )
            self.assertTrue(Path(first_snapshot["selected_rvtools_file"]).is_file())
            self.assertTrue(Path(first_snapshot["selected_pricelist_file"]).is_file())
            self.assertIn(
                f"imported_assessments/{first_id}",
                first_snapshot["selected_rvtools_file"].replace("\\", "/"),
            )

            second_response = client.post(
                "/assessment/import",
                data={
                    "action": "import_assessment",
                    "assessment_file": (BytesIO(portable_bytes), "alpha.json"),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            with client.session_transaction() as sess:
                second_id = str(sess.get("active_assessment_id", ""))
                second_name = str(sess.get("active_assessment_name", ""))

            self.assertEqual(200, second_response.status_code)
            self.assertNotEqual(first_id, second_id)
            self.assertEqual("Alpha / Migration (Imported 2)", second_name)

    def test_invalid_import_preserves_session_state_and_saved_library_bytes(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            client.post(
                "/",
                data={
                    "action": "save_assessment",
                    "assessment_name": "Preserved assessment",
                    "assessment_notes": "Keep every byte.",
                },
            )
            with client.session_transaction() as sess:
                before_session = copy.deepcopy(dict(sess))
            state_path = fixture["app_state"] / f"{fixture['state_id']}.json"
            snapshot_path = fixture["app_state"] / f"{fixture['state_id']}_step4_snapshot.json"
            saved_dir = fixture["app_state"] / "saved_assessments"
            before_state = state_path.read_bytes()
            before_step4 = snapshot_path.read_bytes()
            before_library = {
                path.name: path.read_bytes() for path in saved_dir.glob("*.json")
            }
            invalid = valid_package()
            invalid["package_type"] = "wrong"

            response = client.post(
                "/assessment/import",
                data={
                    "action": "import_assessment",
                    "assessment_file": (
                        BytesIO(json.dumps(invalid).encode("utf-8")),
                        "invalid.json",
                    ),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )

            with client.session_transaction() as sess:
                after_session = dict(sess)
            after_library = {
                path.name: path.read_bytes() for path in saved_dir.glob("*.json")
            }
            self.assertEqual(200, response.status_code)
            self.assertIn(b"Unsupported package type", response.data)
            self.assertEqual(before_session, after_session)
            self.assertEqual(before_state, state_path.read_bytes())
            self.assertEqual(before_step4, snapshot_path.read_bytes())
            self.assertEqual(before_library, after_library)
            imported_root = fixture["downloads"] / "imported_assessments"
            self.assertFalse(imported_root.exists() and any(imported_root.iterdir()))

    def test_final_load_failure_restores_exact_bytes_and_cleans_new_artifacts(self) -> None:
        with isolated_portability_client() as fixture:
            client = fixture["client"]
            export_response = client.post(
                "/",
                data={"action": "export_assessment"},
            )
            state_path = fixture["app_state"] / f"{fixture['state_id']}.json"
            step4_path = (
                fixture["app_state"] / f"{fixture['state_id']}_step4_snapshot.json"
            )
            preferences_path = fixture["app_state"] / "preferences.json"
            preferences_path.write_bytes(b'{"preserved":"exact-spacing"}\n')
            with client.session_transaction() as sess:
                before_session = copy.deepcopy(dict(sess))
            before_state = state_path.read_bytes()
            before_step4 = step4_path.read_bytes()
            before_preferences = preferences_path.read_bytes()

            def fail_after_mutation(*_args: object, **_kwargs: object) -> dict:
                app_module.session.clear()
                app_module.session["mutated"] = True
                state_path.write_bytes(b'{"mutated":true}')
                step4_path.write_bytes(b'{"mutated":true}')
                raise OSError("injected final load failure")

            with patch.object(
                app_module,
                "load_saved_assessment",
                side_effect=fail_after_mutation,
            ):
                response = client.post(
                    "/assessment/import",
                    data={
                        "action": "import_assessment",
                        "assessment_file": (
                            BytesIO(export_response.data),
                            "portable.json",
                        ),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            with client.session_transaction() as sess:
                after_session = dict(sess)
            self.assertEqual(200, response.status_code)
            self.assertIn(b"current assessment was kept", response.data)
            self.assertEqual(before_session, after_session)
            self.assertEqual(before_state, state_path.read_bytes())
            self.assertEqual(before_step4, step4_path.read_bytes())
            self.assertEqual(before_preferences, preferences_path.read_bytes())
            saved_dir = fixture["app_state"] / "saved_assessments"
            self.assertFalse(saved_dir.exists() and list(saved_dir.glob("*.json")))
            imported_root = fixture["downloads"] / "imported_assessments"
            self.assertFalse(imported_root.exists() and any(imported_root.iterdir()))

    def test_results_portability_controls_are_enabled_without_header_menu(self) -> None:
        with isolated_portability_client() as fixture:
            response = fixture["client"].get("/")
            html = response.data.decode("utf-8")
            self.assertEqual(200, response.status_code)
            self.assertNotIn("data-assessment-menu", html)
            self.assertNotIn('value="export_assessment"', html)
            self.assertNotIn('name="assessment_file"', html)
            self.assertNotIn("Export assessment JSON", html)
            self.assertNotIn("Import assessment JSON", html)
            self.assertNotIn(">Save</button>", html)
            self.assertNotIn(">Open</button>", html)
            self.assertNotIn("Portable assessment import is not available yet", html)
            self.assertNotRegex(
                html,
                r"data-assessment-(?:import|export)[^>]+aria-disabled=\"true\"",
            )

        with app_module.app.test_request_context("/step4?tab=price"):
            template = app_module.app.jinja_env.get_template("_export_center.html")
            html = template.render(
                results={
                    "customer_ready_export": False,
                    "assessment_name": "Alpha migration",
                    "assessment_notes": "Notes",
                    "excel_export_label": "Export Draft",
                }
            )
        self.assertIn('value="export_assessment"', html)
        self.assertIn('name="assessment_file"', html)
        self.assertIn("Export assessment JSON", html)
        self.assertIn("Import assessment JSON", html)
        self.assertIn("data-assessment-save-form", html)
        self.assertIn("data-assessment-open-form", html)


if __name__ == "__main__":
    unittest.main()
