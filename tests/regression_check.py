from __future__ import annotations

import math
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from werkzeug.datastructures import MultiDict

import app as app_module


TMP_ROOT = Path("/private/tmp") if Path("/private/tmp").exists() else Path("/tmp")
RUN_ID = uuid4().hex
REGRESSION_ROOT = TMP_ROOT / f"migration_assessment_regression_{RUN_ID}"
app_module.DOWNLOADS_DIR = REGRESSION_ROOT / "downloads"
app_module.RVTOOLS_DIR = REGRESSION_ROOT / "rvtools"
app_module.APP_STATE_DIR = REGRESSION_ROOT / "app_state"
app_module.EXPORTS_DIR = REGRESSION_ROOT / "exports"
CSV_INVENTORY = app_module.RVTOOLS_DIR / "regression_inventory.csv"
XLSX_INVENTORY = app_module.RVTOOLS_DIR / "regression_inventory.xlsx"
MOB_ID_INVENTORY = app_module.RVTOOLS_DIR / "mob_id_inventory.xlsx"
DUPLICATE_INVENTORY = app_module.RVTOOLS_DIR / "duplicate_inventory.csv"
OFFICE_LOCK_INVENTORY = app_module.RVTOOLS_DIR / "~$regression_inventory.xlsx"
REJECTED_INPUT = app_module.RVTOOLS_DIR / "not_vm_inventory.csv"
EXPECTED_VM_COUNT = 4

XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_RELS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} failed. {detail}".strip())
    print(f"PASS {name}{': ' + detail if detail else ''}")


def check_close(name: str, actual: float, expected: float, tolerance: float = 0.01) -> None:
    check(
        name,
        abs(float(actual) - float(expected)) <= tolerance,
        f"actual={actual:.6f}, expected={expected:.6f}",
    )


def sheet_text_and_numbers(zf: zipfile.ZipFile, sheet_path: str) -> tuple[str, list[float], int]:
    xml = ET.fromstring(zf.read(sheet_path))
    text_values = [t.text or "" for t in xml.findall(".//m:t", XLSX_NS)]
    numbers: list[float] = []
    populated_rows = 0

    for row in xml.findall(".//m:row", XLSX_NS):
        row_has_value = False
        for cell in row.findall("m:c", XLSX_NS):
            if cell.find("m:is", XLSX_NS) is not None:
                row_has_value = True
            value = cell.find("m:v", XLSX_NS)
            if value is not None and value.text is not None:
                row_has_value = True
                try:
                    numbers.append(float(value.text))
                except ValueError:
                    pass
        if row_has_value:
            populated_rows += 1

    return " ".join(text_values), numbers, populated_rows


def sheet_text_rows(zf: zipfile.ZipFile, sheet_path: str) -> list[list[str]]:
    xml = ET.fromstring(zf.read(sheet_path))
    text_rows: list[list[str]] = []
    for row in xml.findall(".//m:row", XLSX_NS):
        values = []
        for cell in row.findall("m:c", XLSX_NS):
            text = cell.find(".//m:t", XLSX_NS)
            values.append(text.text if text is not None and text.text is not None else "")
        if any(value.strip() for value in values):
            text_rows.append(values)
    return text_rows


def workbook_sheet_map(zf: zipfile.ZipFile) -> dict[str, str]:
    workbook_xml = ET.fromstring(zf.read("xl/workbook.xml"))
    rels_xml = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_map = {
        rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
        for rel in rels_xml.findall(f"{{{RELS_NS}}}Relationship")
    }
    sheet_map: dict[str, str] = {}
    sheets = workbook_xml.find("m:sheets", XLSX_NS)
    if sheets is None:
        return sheet_map
    for sheet in sheets:
        name = sheet.attrib.get("name", "")
        rel_id = sheet.attrib.get(f"{{{OFFICE_RELS_NS}}}id", "")
        target = rel_map.get(rel_id, "")
        if target and not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        sheet_map[name] = target
    return sheet_map


def price_item(display_name: str, value: float) -> dict[str, object]:
    return {
        "displayName": display_name,
        "currencyCodeLocalizations": [
            {
                "currencyCode": "EUR",
                "prices": [{"model": "PAY_AS_YOU_GO", "value": value}],
            }
        ],
    }


def create_regression_fixtures() -> None:
    app_module.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    app_module.RVTOOLS_DIR.mkdir(parents=True, exist_ok=True)

    inventory_rows = [
        ["VM", "Powerstate", "Template", "OS according to the configuration file", "CPUs", "Memory", "Provisioned MiB"],
        ["vm-app-01", "poweredOn", "False", "Microsoft Windows Server 2019 (64-bit)", "4", "8192", "102400"],
        ["vm-db-01", "poweredOn", "False", "Red Hat Enterprise Linux 8 (64-bit)", "8", "16384", "512000"],
        ["vm-web-01", "poweredOff", "False", "Ubuntu Linux (64-bit)", "2", "4096", "51200"],
        ["vm-legacy-01", "poweredOn", "False", "Microsoft Windows Server 2008 (64-bit)", "2", "4096", "20480"],
    ]
    CSV_INVENTORY.write_text(
        "\n".join(",".join(value for value in row) for row in inventory_rows) + "\n",
        encoding="utf-8",
    )
    duplicate_rows = [
        ["VM", "Powerstate", "Template", "OS according to the configuration file", "CPUs", "Memory", "Provisioned MiB"],
        ["vm-duplicate", "poweredOn", "False", "Microsoft Windows Server 2019 (64-bit)", "4", "8192", "102400"],
        ["vm-unique", "poweredOn", "False", "Red Hat Enterprise Linux 8 (64-bit)", "2", "4096", "51200"],
        ["vm-duplicate", "poweredOn", "False", "Microsoft Windows Server 2019 (64-bit)", "4", "8192", "10240"],
    ]
    DUPLICATE_INVENTORY.write_text(
        "\n".join(",".join(value for value in row) for row in duplicate_rows) + "\n",
        encoding="utf-8",
    )
    xlsx_bytes = app_module._build_xlsx_workbook_bytes(
        [{"name": "vInfo", "rows": inventory_rows}],
        currency_fmt_code='€#,##0.00',
    )
    XLSX_INVENTORY.write_bytes(xlsx_bytes)
    mob_id_rows = [
        [
            "MOB ID",
            "IsRunning",
            "Power State",
            "VM OS",
            "Virtual CPU",
            "Provisioned Memory (MiB)",
            "Guest VM Disk Capacity (MiB)",
        ],
        ["vm-1001", "TRUE", "poweredOn", "Microsoft Windows Server 2019 (64-bit)", "4", "8192", "102400"],
        ["vm-1002", "FALSE", "poweredOff", "SUSE Linux Enterprise 12 (64-bit)", "8", "16384", "512000"],
    ]
    mob_id_xlsx_bytes = app_module._build_xlsx_workbook_bytes(
        [{"name": "vInfo", "rows": mob_id_rows}],
        currency_fmt_code='€#,##0.00',
    )
    MOB_ID_INVENTORY.write_bytes(mob_id_xlsx_bytes)
    OFFICE_LOCK_INVENTORY.write_bytes(b"temporary-office-lock-file")
    REJECTED_INPUT.write_text(
        "Part,Description,Unit Price\nA1,Oracle Investment Proposal,100\n",
        encoding="utf-8",
    )

    price_payload = {
        "items": [
            price_item("Compute - Standard - X9 - OCPU", 0.0372),
            price_item("Compute - Standard - X9 - Memory", 0.001395),
            price_item("Compute - Standard - E4 - OCPU", 0.02325),
            price_item("Compute - Standard - E4  - Memory", 0.001395),
            price_item("Compute - Standard - E5 - OCPU", 0.03),
            price_item("Compute - Standard - E5 - Memory", 0.0018),
            price_item("OCI - Compute - Standard - E6 - OCPU", 0.035),
            price_item("OCI - Compute - Standard - E6 - Memory", 0.002),
            price_item("Storage - Block Volume - Storage", 0.023715),
            price_item("Storage - Block Volume - Performance Units", 0.001581),
            price_item("Compute - Windows OS", 0.092),
        ]
    }
    price_file = app_module.DOWNLOADS_DIR / "oci_pricing_EUR_regression.json"
    price_file.write_text(json.dumps(price_payload, indent=2), encoding="utf-8")


def find_price_file() -> str:
    price_lists = app_module.list_downloaded_price_lists()
    check("local price lists available", bool(price_lists), str(price_lists[:2]))
    return next((path for path in price_lists if "EUR" in path), price_lists[0])


def validate_price_list_dropdown_policy() -> None:
    with app_module.app.test_client() as client:
        response = client.get("/")
        check(
            "last selected price list restored",
            response.status_code == 200 and b"Active Price List" in response.data and b"oci_pricing_EUR_regression.json" in response.data,
        )

    payload = {"items": [], "lastUpdated": "Regression"}
    for idx in range(12):
        path = app_module.DOWNLOADS_DIR / f"oci_pricing_EUR_dropdown_{idx:02d}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

    with app_module.app.test_client() as client:
        response = client.get("/")
        html = response.data.decode("utf-8")
        price_select = re.search(r'<select id="price_list_file".*?</select>', html, re.S)
        price_option_count = len(re.findall(r'<option value="[^"]*oci_pricing_', price_select.group(0))) if price_select else 0
        check("price list dropdown capped at 10", price_option_count == 10, str(price_option_count))
        check(
            "currency list EMEA plus USD",
            all(f'value="{currency}"' in html for currency in ["USD", "EUR", "GBP", "CHF", "SEK", "NOK", "DKK"])
            and all(f'value="{currency}"' not in html for currency in ["AUD", "CAD", "JPY", "SGD"]),
        )


def validate_inventory_imports() -> None:
    discovered_files = app_module.list_rvtools_export_files()
    check(
        "temporary inventory files hidden",
        str(OFFICE_LOCK_INVENTORY).replace("\\", "/") not in discovered_files,
        str(discovered_files),
    )

    accepted_files = [CSV_INVENTORY, XLSX_INVENTORY, MOB_ID_INVENTORY]
    for inventory_path in accepted_files:
        check("inventory fixture exists", inventory_path.exists(), str(inventory_path))
        rows, source = app_module.load_vms_from_vinfo(str(inventory_path))
        total_vcpu = int(sum(app_module._to_number(row.get("cpus")) for row in rows))
        total_ram_gb = int(math.ceil(sum(app_module._to_number(row.get("memory_mb")) for row in rows) / 1024.0))
        total_storage_gb = int(
            math.ceil(sum(app_module._to_number(row.get("provisioned_mib")) for row in rows) / 1024.0)
        )
        check(
            "inventory totals valid",
            bool(rows) and total_vcpu > 0 and total_ram_gb > 0 and total_storage_gb > 0,
            f"{inventory_path.name}: {len(rows)} VMs from {source}",
        )

    matching_before = sorted(app_module.RVTOOLS_DIR.glob(f"{CSV_INVENTORY.stem}*{CSV_INVENTORY.suffix}"))
    with app_module.app.test_client() as client:
        response = client.get("/")
        check("inventory upload reuse session initialized", response.status_code == 200)
        with CSV_INVENTORY.open("rb") as handle:
            response = client.post(
                "/",
                data={"action": "upload_rvtools_file", "rvtools_upload": (handle, CSV_INVENTORY.name)},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
    matching_after = sorted(app_module.RVTOOLS_DIR.glob(f"{CSV_INVENTORY.stem}*{CSV_INVENTORY.suffix}"))
    check(
        "inventory upload reuses identical catalog file",
        response.status_code == 200
        and b"already exists in the rvtools catalog" in response.data
        and matching_after == matching_before,
        f"before={matching_before}, after={matching_after}",
    )

    try:
        app_module.load_vms_from_vinfo(str(REJECTED_INPUT))
    except Exception as exc:
        info = app_module.build_rejected_inventory_info(
            {"file_path": str(REJECTED_INPUT), "file_name": REJECTED_INPUT.name},
            str(exc),
        )
        check("non-inventory input rejected", info["category"] == "Unsupported inventory format", info["category"])
    else:
        raise AssertionError("Expected non-inventory fixture to be rejected.")


def validate_step3_duplicate_removal() -> None:
    rows, _ = app_module.load_vms_from_vinfo(str(DUPLICATE_INVENTORY))
    vm_names = [row["name"] for row in rows]
    check("duplicate fixture loads suffixed VM", "vm-duplicate [2]" in vm_names, str(vm_names))

    with app_module.app.test_client() as client:
        response = client.get("/")
        check("duplicate removal session initialized", response.status_code == 200)
        with client.session_transaction() as sess:
            sess["selected_rvtools_file"] = str(DUPLICATE_INVENTORY)

        response = client.post(
            "/step3",
            data=MultiDict([("action", "add")] + [("vm_names", name) for name in vm_names]),
            follow_redirects=True,
        )
        check("duplicate fixture selected", response.status_code == 200 and b"Duplicate VM rows:" in response.data)

        response = client.post("/step3", data={"action": "remove_duplicates"}, follow_redirects=True)
        state = app_module.load_app_state()
        selected_names = state.get("selected_vm_names", [])
        check(
            "step3 remove duplicate names",
            response.status_code == 200
            and b"Removed 1 duplicate VM name row" in response.data
            and selected_names == ["vm-duplicate", "vm-unique"],
            str(selected_names),
        )


def validate_manual_sizing_input() -> None:
    with app_module.app.test_client() as client:
        response = client.get("/")
        check(
            "manual sizing form renders",
            response.status_code == 200
            and b"Manual Workload Summary" in response.data
            and b"manual_windows_vm_count" not in response.data,
        )

        response = client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "manual_vm_count": "6",
                "manual_total_vcpus": "25",
                "manual_total_memory_gb": "96",
                "manual_total_storage_gb": "1200",
                "manual_supported_vm_count": "5",
                "manual_unsupported_vm_count": "1",
            },
            follow_redirects=True,
        )
        check(
            "manual sizing creates inventory",
            response.status_code == 200
            and b"Manual workload summary created" in response.data
            and b"Selected VM Inventory File" in response.data,
        )
        check(
            "manual sizing form prefilled after create",
            b"Update Manual Inventory" in response.data
            and b'name="manual_vm_count" type="number" min="1" step="1" value="6"' in response.data
            and b'name="manual_total_vcpus" type="number" min="1" step="1" value="25"' in response.data
            and b'name="manual_supported_vm_count" type="number" min="0" step="1" value="5"' in response.data,
        )

        with client.session_transaction() as sess:
            selected_file = str(sess.get("selected_rvtools_file", ""))
        manual_rows, source = app_module.load_vms_from_vinfo(selected_file)
        state = app_module.load_app_state()
        selected_names = state.get("selected_vm_names", [])
        check("manual source path selected", "/manual/" in selected_file.replace("\\", "/"), selected_file)
        check("manual source loads", len(manual_rows) == 6 and "manual" in source.lower(), source)
        check("manual rows auto-selected", len(selected_names) == 6, str(selected_names))
        check(
            "manual totals preserved",
            sum(int(row["cpus"]) for row in manual_rows) == 25
            and sum(int(math.ceil(int(row["memory_mb"]) / 1024.0)) for row in manual_rows) == 96
            and sum(int(math.ceil(int(row["provisioned_mib"]) / 1024.0)) for row in manual_rows) == 1200,
            str(manual_rows),
        )

        response = client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "manual_vm_count": "4",
                "manual_total_vcpus": "18",
                "manual_total_memory_gb": "80",
                "manual_total_storage_gb": "900",
                "manual_supported_vm_count": "3",
                "manual_unsupported_vm_count": "1",
            },
            follow_redirects=True,
        )
        with client.session_transaction() as sess:
            updated_selected_file = str(sess.get("selected_rvtools_file", ""))
        updated_rows, _updated_source = app_module.load_vms_from_vinfo(updated_selected_file)
        updated_state = app_module.load_app_state()
        updated_names = updated_state.get("selected_vm_names", [])
        check(
            "manual sizing updates existing summary",
            response.status_code == 200
            and b"Manual workload summary updated" in response.data
            and updated_selected_file != selected_file
            and len(updated_rows) == 4
            and len(updated_names) == 4,
            updated_selected_file,
        )
        check(
            "manual updated totals preserved",
            sum(int(row["cpus"]) for row in updated_rows) == 18
            and sum(int(math.ceil(int(row["memory_mb"]) / 1024.0)) for row in updated_rows) == 80
            and sum(int(math.ceil(int(row["provisioned_mib"]) / 1024.0)) for row in updated_rows) == 900,
            str(updated_rows),
        )
        selected_file = updated_selected_file

        response = client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "manual_vm_count": "5",
                "manual_total_vcpus": "20",
                "manual_total_memory_gb": "64",
                "manual_total_storage_gb": "500",
                "manual_supported_vm_count": "2",
                "manual_unsupported_vm_count": "2",
            },
            follow_redirects=True,
        )
        with client.session_transaction() as sess:
            selected_file_after_invalid = str(sess.get("selected_rvtools_file", ""))
        check(
            "manual invalid counts rejected",
            response.status_code == 200
            and b"Manual sizing counts must add up to the VM count" in response.data
            and selected_file_after_invalid == selected_file,
            selected_file_after_invalid,
        )


def run_workflow_and_export() -> tuple[Path, dict[str, object]]:
    inventory = CSV_INVENTORY
    price_file = find_price_file()

    with app_module.app.test_client() as client:
        response = client.get("/")
        check("home route renders", response.status_code == 200 and b"Step 1 - Setup & Inventory" in response.data)
        check(
            "no default price list before selection",
            b"Active Price List" not in response.data and b"-- Select OCI price list --" in response.data,
        )

        response = client.post(
            "/",
            data={"action": "save_customer_name", "customer_name": "Regression Customer"},
            follow_redirects=True,
        )
        check("customer name save", response.status_code == 200 and b"Regression Customer" in response.data)

        response = client.post(
            "/",
            data={"action": "select_pricelist", "price_list_file": price_file},
            follow_redirects=True,
        )
        check("price list selection", response.status_code == 200 and b"Active Price List" in response.data)

        response = client.post(
            "/",
            data={"action": "select_rvtools_file", "rvtools_file": str(inventory)},
            follow_redirects=True,
        )
        check(
            "inventory selection",
            response.status_code == 200
            and b"Import Quality Check" in response.data
            and b"Selected VM Inventory" in response.data,
        )

        rows, _ = app_module.load_vms_from_vinfo(str(inventory))
        vm_names = [row["name"] for row in rows]
        response = client.post(
            "/step3",
            data=MultiDict([("action", "add"), ("redirect_to", "step4")] + [("vm_names", name) for name in vm_names]),
            follow_redirects=False,
        )
        check("step3 continue redirect", response.status_code in {302, 303} and "/step4" in response.headers.get("Location", ""))

        for route, panel_id in [
            ("/step4", b'id="scenario-panel-paths"'),
            ("/scenario/native", b'id="scenario-panel-native"'),
            ("/scenario/ocvs", b'id="scenario-panel-ocvs"'),
            ("/scenario/hybrid", b'id="scenario-panel-hybrid"'),
            ("/step5", b'id="scenario-panel-price"'),
        ]:
            response = client.get(route, follow_redirects=True)
            check(f"{route} panel renders", response.status_code == 200 and panel_id in response.data)
            check(f"{route} controls render", b"Save Settings" in response.data and b"Export to Excel" in response.data)
            check(
                f"{route} export status UI renders",
                b'id="export-status"' in response.data
                and b"Excel export created:" in response.data
                and b"Open file" in response.data,
            )
            check(f"{route} has no JSON export", b"Export to JSON" not in response.data and b"Export to Json" not in response.data)
            response_text = response.data.decode("utf-8", errors="ignore")
            scenario_cost_per_vm_values = [
                float(re.sub(r"[^0-9.]", "", value) or 0)
                for value in re.findall(
                    r"<span>Cost / VM / month</span>\s*<strong[^>]*>\s*([^<]+?)\s*</strong>",
                    response_text,
                )
            ]
            check(
                f"{route} scenario cost per VM populated",
                bool(scenario_cost_per_vm_values) and all(value > 0 for value in scenario_cost_per_vm_values),
                str(scenario_cost_per_vm_values),
            )
            if route == "/scenario/native":
                check(
                    "native tab naming convention",
                    b"OCI Native sizing decision" in response.data
                    and b"Infrastructure and Licensing" in response.data
                    and b"VM Sizing Inputs" in response.data,
                )
            if route == "/scenario/ocvs":
                check(
                    "ocvs sizing layout labels",
                    b"OCVS sizing decision" in response.data
                    and b"Sizing basis" in response.data
                    and b"Workload Capacity Requirements" in response.data
                    and b"Infrastructure and Licensing" in response.data
                    and b"Sizing Assumptions" in response.data
                    and b"Cost optimized" in response.data
                    and b"Selected capacity" not in response.data
                    and b"Workload capacity to size" not in response.data,
                )
            if route == "/scenario/hybrid":
                check(
                    "hybrid tab naming convention",
                    b"Hybrid placement decision" in response.data
                    and b"Placement basis" in response.data
                    and b"Infrastructure and Licensing" in response.data
                    and b"OCVS Subset Sizing" in response.data
                    and b"Placement cost split" not in response.data,
                )

        first_vm, second_vm = vm_names[0], vm_names[1]
        save_data = MultiDict(
            [
                ("action", "save"),
                ("active_scenario", "hybrid"),
                ("ocvs_profile", "BM.Standard.E4.128"),
                ("ocvs_vcpu_per_ocpu", "4"),
                ("ocvs_cpu_headroom_pct", "20"),
                ("ocvs_memory_headroom_pct", "20"),
                ("ocvs_storage_headroom_pct", "25"),
                ("ocvs_dense_vsan_usable_pct", "50"),
                ("ocvs_standard_storage_vpu", "10"),
                ("ocvs_dr_nodes", "1"),
                ("ocvs_commitment_term", "3_year"),
                ("vmware_license_price_per_core_yearly", "400"),
                ("hybrid_vm_name", first_vm),
                ("hybrid_placement", "ocvs"),
                ("hybrid_vm_name", second_vm),
                ("hybrid_placement", "native"),
            ]
        )
        response = client.post("/step4", data=save_data, follow_redirects=True)
        check("path settings save", response.status_code == 200 and b"Migration path settings saved." in response.data)

        state = app_module.load_app_state()
        placements = state.get("step4_hybrid_placements", {})
        check("ocvs commitment term persists", state.get("step4_ocvs_commitment_term") == "3_year", str(state))
        check(
            "hybrid placement persists",
            placements.get(first_vm) == "ocvs" and placements.get(second_vm) == "native",
            f"{first_vm}={placements.get(first_vm)}, {second_vm}={placements.get(second_vm)}",
        )

        bulk_response = client.post(
            "/step4",
            data={
                "action": "save",
                "active_scenario": "native",
                "bulk_apply_oci_shape": "E5",
                "bulk_apply_burst": "50%",
                "bulk_apply_vpu": "20",
                "bulk_apply_os_license": "Lic Include",
            },
            follow_redirects=True,
        )
        check("native bulk settings save", bulk_response.status_code == 200 and b"Migration path settings saved." in bulk_response.data)
        state = app_module.load_app_state()
        shapes = state.get("step4_vm_shapes", {})
        bursts = state.get("step4_vm_bursts", {})
        vpus = state.get("step4_vm_vpus", {})
        os_licenses = state.get("step4_vm_os_license", {})
        check("bulk target shape persisted", all(shapes.get(name) == "E5" for name in vm_names), str(shapes))
        check("bulk burst persisted", all(bursts.get(name) == "50%" for name in vm_names), str(bursts))
        check("bulk vpu persisted", all(vpus.get(name) == 20 for name in vm_names), str(vpus))
        check(
            "bulk Windows license persisted",
            os_licenses.get("vm-app-01") == "Lic Include" and os_licenses.get("vm-legacy-01") == "Lic Include",
            str(os_licenses),
        )

        strategy_response = client.post(
            "/step4",
            data=MultiDict(
                [
                    ("action", "save"),
                    ("active_scenario", "native"),
                    ("native_shape_strategy_enabled", "1"),
                    ("native_strategy_os", "Microsoft Windows Server 2019 (64-bit)"),
                    ("native_strategy_shape", "E4"),
                    ("native_strategy_burst", "12.5%"),
                    ("native_strategy_os", "Red Hat Enterprise Linux 8 (64-bit)"),
                    ("native_strategy_shape", "E6"),
                    ("native_strategy_burst", "100%"),
                ]
            ),
            follow_redirects=True,
        )
        check(
            "native default shape strategy save",
            strategy_response.status_code == 200 and b"Migration path settings saved." in strategy_response.data,
        )
        state = app_module.load_app_state()
        shapes = state.get("step4_vm_shapes", {})
        bursts = state.get("step4_vm_bursts", {})
        check(
            "strategy target shape persisted",
            shapes.get("vm-app-01") == "E4" and shapes.get("vm-db-01") == "E6",
            str(shapes),
        )
        check(
            "strategy burst persisted",
            bursts.get("vm-app-01") == "12.5%" and bursts.get("vm-db-01") == "100%",
            str(bursts),
        )

        response = client.post(
            "/step4",
            data={"action": "export_excel", "active_scenario": "price"},
            follow_redirects=False,
        )
        content_disposition = response.headers.get("Content-Disposition", "")
        check(
            "excel export route",
            response.status_code == 200
            and response.data.startswith(b"PK")
            and "attachment" in content_disposition
            and ".xlsx" in content_disposition,
        )
        with client.session_transaction() as saved_session:
            state_id = str(saved_session.get("state_id", "") or "")

    with app_module.app.test_request_context("/"):
        if state_id:
            app_module.session["state_id"] = state_id
        workflow_state = app_module.load_app_state()

    exports = sorted(app_module.EXPORTS_DIR.glob("*.xlsx"))
    check("single workbook export", len(exports) == 1, str(exports))
    return exports[0], workflow_state


def validate_workbook(workbook_path: Path) -> None:
    expected_sheets = [
        "Executive Summary",
        "Price Comparison",
        "OCI Native Analysis",
        "OCVS Analysis",
        "Hybrid Analysis",
        "Hybrid Placement",
        "Selected VMs",
        "Non-Selected VMs",
        "Price List",
        "Technical Details",
    ]
    with zipfile.ZipFile(workbook_path) as zf:
        check("xlsx integrity", zf.testzip() is None, workbook_path.name)
        styles_xml = zf.read("xl/styles.xml").decode("utf-8")
        check("left-aligned workbook styles", 'numFmtId="164"' in styles_xml and 'horizontal="left" vertical="center"' in styles_xml)
        check("wrapped left-aligned styles", 'horizontal="left" vertical="center" wrapText="1"' in styles_xml)
        check("no centered workbook styles", 'horizontal="center"' not in styles_xml)
        check("no right-aligned workbook styles", 'horizontal="right"' not in styles_xml)

        raw_xml = "\n".join(
            zf.read(name).decode("utf-8", errors="ignore")
            for name in zf.namelist()
            if name.endswith(".xml")
        )
        check("no Excel error markers", not re.search(r"#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", raw_xml))

        sheet_map = workbook_sheet_map(zf)
        check("workbook sheet order", list(sheet_map.keys()) == expected_sheets, ", ".join(sheet_map.keys()))
        check("recommendation tab removed", "Recommendation" not in sheet_map)
        check("migration paths tab consolidated", "Migration Paths" not in sheet_map)

        sheet_data = {
            sheet_name: sheet_text_and_numbers(zf, sheet_path)
            for sheet_name, sheet_path in sheet_map.items()
        }
        check(
            "executive summary sections",
            all(
                token in sheet_data["Executive Summary"][0]
                for token in [
                    "Assessment Context",
                    "Decision Readout",
                    "Migration Path Options",
                    "Modernize and Optimize",
                    "Lift & Shift",
                    "Balance Modernization and Risk",
                    "Report Scope",
                ]
            ),
        )
        executive_rows = sheet_text_rows(zf, sheet_map["Executive Summary"])
        check(
            "executive migration path cards exported horizontally",
            any(
                len(row) >= 3
                and row[0] == "Modernize and Optimize"
                and row[1] == "Lift & Shift"
                and row[2] == "Balance Modernization and Risk"
                for row in executive_rows
            )
            and any(
                len(row) >= 3
                and row[0] == "OCI Native"
                and row[1] == "Oracle Cloud VMware Solution (OCVS)"
                and row[2] == "Hybrid"
                for row in executive_rows
            ),
        )
        check("price comparison sections", all(token in sheet_data["Price Comparison"][0] for token in ["Price Signal", "Ranked Migration Path Price Comparison", "3-Year Cost"]))
        check("ocvs sections", all(token in sheet_data["OCVS Analysis"][0] for token in ["Workload Capacity Requirements", "OCVS Sizing Decision", "Capacity Drivers"]))
        check(
            "ocvs commitment exported",
            "OCVS Commitment Term" in sheet_data["OCVS Analysis"][0]
            and "OCVS Commitment Term" in sheet_data["Technical Details"][0]
            and "3-Year" in sheet_data["Technical Details"][0],
        )
        check("technical sizing notes label", "Sizing Summary Notes" in sheet_data["Technical Details"][0])
        check("old warning labels removed", "Fit Warnings" not in sheet_data["Technical Details"][0] and "Severity" not in sheet_data["Technical Details"][0])
        check(
            "hybrid placement populated",
            sheet_data["Hybrid Placement"][2] >= EXPECTED_VM_COUNT + 1,
            f"{sheet_data['Hybrid Placement'][2]} populated rows",
        )
        check(
            "selected VM detail populated",
            sheet_data["Selected VMs"][2] >= EXPECTED_VM_COUNT + 1,
            f"{sheet_data['Selected VMs'][2]} populated rows",
        )
        check("price list populated", "Block Storage Unit Price" in sheet_data["Price List"][0] and any(value > 0 for value in sheet_data["Price List"][1]))


def validate_pricing_invariants(state: dict[str, object]) -> None:
    price_file = find_price_file()
    price_lookup, _, source_pricelist_file = app_module.load_price_lookup(price_file)
    shape_options = app_module.load_oci_target_shapes()
    shape_pricing_map = app_module.load_oci_price_mapping_details()
    if shape_pricing_map:
        shape_options = [shape for shape in shape_options if shape in shape_pricing_map] or list(shape_pricing_map.keys())
    unit_prices = app_module.resolve_pricing_unit_prices(price_lookup)
    inventory_rows, _ = app_module.load_vms_from_vinfo(str(CSV_INVENTORY))
    selected_names = state.get("selected_vm_names") or [row["name"] for row in inventory_rows]
    selected_set = {str(name) for name in selected_names}
    selected_vms = [row for row in inventory_rows if str(row.get("name")) in selected_set]

    vm_rows = app_module.build_vm_cost_rows(
        selected_vms,
        shape_options=shape_options,
        shape_pricing_map=shape_pricing_map,
        price_lookup=price_lookup,
        block_storage_unit_price=unit_prices["block_storage_unit_price"],
        block_perf_unit_price=unit_prices["block_perf_unit_price"],
        windows_os_unit_price=unit_prices["windows_os_unit_price"],
        iaas_discount_pct=float(state.get("step4_iaas_discount_pct", 0.0) or 0.0),
        vm_shape_selection=state.get("step4_vm_shapes", {}),
        vm_ocpu_selection=state.get("step4_vm_ocpus", {}),
        vm_burst_selection=state.get("step4_vm_bursts", {}),
        vm_vpu_selection=state.get("step4_vm_vpus", {}),
        vm_os_license_selection=state.get("step4_vm_os_license", {}),
        valid_shape_values=set(shape_options),
        valid_vpu_values=set(app_module.VPU_OPTIONS),
    )
    analysis = app_module.build_price_analysis_from_rows(
        vm_rows=vm_rows,
        price_lookup=price_lookup,
        block_storage_unit_price=unit_prices["block_storage_unit_price"],
        block_perf_unit_price=unit_prices["block_perf_unit_price"],
        windows_os_unit_price=unit_prices["windows_os_unit_price"],
        iaas_discount_pct=float(state.get("step4_iaas_discount_pct", 0.0) or 0.0),
        ocvs_policy=app_module.normalize_ocvs_policy(state.get("step4_ocvs_policy", {})),
        ocvs_profile_choice=app_module.normalize_ocvs_profile(state.get("step4_ocvs_profile", "best_fit")),
        source_pricelist_file=source_pricelist_file,
        vmware_license_price_per_core_yearly=app_module._bounded_float(
            state.get("step4_vmware_license_price_per_core_yearly"),
            0.0,
            0.0,
            1_000_000.0,
        ),
        ocvs_dr_nodes=app_module.normalize_ocvs_dr_nodes(state.get("step4_ocvs_dr_nodes", 0)),
        ocvs_commitment_term=app_module.normalize_ocvs_commitment_term(state.get("step4_ocvs_commitment_term", "payg")),
        hybrid_placement_selection=state.get("step4_hybrid_placements", {}),
    )

    overall = analysis["overall"]
    native_monthly = float(overall["total_monthly_cost"])
    check_close(
        "native component total invariant",
        native_monthly,
        float(overall["total_cpu_ram_monthly_cost"])
        + float(overall["total_storage_monthly_cost"])
        + float(overall["total_os_license_monthly_cost"]),
    )

    ocvs_selected = analysis["ocvs_price"]["selected"]
    vmware_summary = analysis["vmware_license_summary"]
    check_close(
        "ocvs component total invariant",
        analysis["price_comparison"]["ocvs_monthly_cost"],
        float(ocvs_selected["total_monthly_cost"]) + float(vmware_summary["ocvs"]["monthly_cost"]),
    )

    hybrid_selected = analysis["hybrid_ocvs_price"]["selected"]
    check_close(
        "hybrid component total invariant",
        analysis["price_comparison"]["hybrid_monthly_cost"],
        float(analysis["supported_native_summary"]["total_monthly_cost"])
        + float(hybrid_selected["total_monthly_cost"])
        + float(vmware_summary["hybrid"]["monthly_cost"]),
    )

    scenario_rows = analysis["scenario_comparison"]["rows"]
    native_row = next(row for row in scenario_rows if row["id"] == "native")
    for row in scenario_rows:
        check_close(f"{row['id']} annual price invariant", row["yearly_cost"], row["monthly_cost"] * 12.0)
        expected_delta = 0.0 if row["id"] == "native" else float(row["monthly_cost"]) - float(native_row["monthly_cost"])
        check_close(f"{row['id']} delta invariant", row["monthly_delta"], expected_delta)

    viable_rows = [row for row in scenario_rows if row.get("is_viable")]
    comparison_pool = viable_rows or scenario_rows
    expected_best = min(comparison_pool, key=lambda item: float(item["monthly_cost"]))
    check("best scenario invariant", analysis["scenario_comparison"]["best"]["id"] == expected_best["id"], expected_best["id"])
    ranked_chart_rows = sorted(analysis["scenario_chart_rows"], key=lambda item: float(item["monthly_cost"]))
    check(
        "scenario rank sorting invariant",
        [row["monthly_cost"] for row in ranked_chart_rows] == sorted(row["monthly_cost"] for row in ranked_chart_rows),
    )
    check_close(
        "monthly spread invariant",
        analysis["scenario_comparison"]["monthly_spread"],
        max(float(row["monthly_cost"]) for row in comparison_pool) - min(float(row["monthly_cost"]) for row in comparison_pool),
    )
    check_close(
        "3-year spread invariant",
        analysis["scenario_comparison"]["three_year_spread"],
        analysis["scenario_comparison"]["monthly_spread"] * 36.0,
    )

    payg_analysis = app_module.build_price_analysis_from_rows(
        vm_rows=vm_rows,
        price_lookup=price_lookup,
        block_storage_unit_price=unit_prices["block_storage_unit_price"],
        block_perf_unit_price=unit_prices["block_perf_unit_price"],
        windows_os_unit_price=unit_prices["windows_os_unit_price"],
        iaas_discount_pct=0.0,
        ocvs_policy=app_module.normalize_ocvs_policy({}),
        ocvs_profile_choice="BM.Standard.E4.128",
        source_pricelist_file=source_pricelist_file,
        vmware_license_price_per_core_yearly=0.0,
        ocvs_dr_nodes=0,
        ocvs_commitment_term="payg",
        hybrid_placement_selection={},
    )
    one_year_analysis = app_module.build_price_analysis_from_rows(
        vm_rows=vm_rows,
        price_lookup=price_lookup,
        block_storage_unit_price=unit_prices["block_storage_unit_price"],
        block_perf_unit_price=unit_prices["block_perf_unit_price"],
        windows_os_unit_price=unit_prices["windows_os_unit_price"],
        iaas_discount_pct=0.0,
        ocvs_policy=app_module.normalize_ocvs_policy({}),
        ocvs_profile_choice="BM.Standard.E4.128",
        source_pricelist_file=source_pricelist_file,
        vmware_license_price_per_core_yearly=0.0,
        ocvs_dr_nodes=0,
        ocvs_commitment_term="1_year",
        hybrid_placement_selection={},
    )
    three_year_analysis = app_module.build_price_analysis_from_rows(
        vm_rows=vm_rows,
        price_lookup=price_lookup,
        block_storage_unit_price=unit_prices["block_storage_unit_price"],
        block_perf_unit_price=unit_prices["block_perf_unit_price"],
        windows_os_unit_price=unit_prices["windows_os_unit_price"],
        iaas_discount_pct=0.0,
        ocvs_policy=app_module.normalize_ocvs_policy({}),
        ocvs_profile_choice="BM.Standard.E4.128",
        source_pricelist_file=source_pricelist_file,
        vmware_license_price_per_core_yearly=0.0,
        ocvs_dr_nodes=0,
        ocvs_commitment_term="3_year",
        hybrid_placement_selection={},
    )
    payg_host = float(payg_analysis["ocvs_price"]["selected"]["host_monthly_cost"])
    one_year_host = float(one_year_analysis["ocvs_price"]["selected"]["host_monthly_cost"])
    three_year_host = float(three_year_analysis["ocvs_price"]["selected"]["host_monthly_cost"])
    check_close("ocvs one-year term discount", one_year_host, payg_host * 0.65)
    check_close("ocvs three-year term discount", three_year_host, payg_host * 0.55)
    check(
        "hybrid ocvs term metadata",
        three_year_analysis["hybrid_ocvs_price"]["selected"]["commitment_term"] == "3_year"
        and three_year_analysis["hybrid_ocvs_price"]["selected"]["commitment_discount_pct"] == 45.0,
    )


def main() -> None:
    create_regression_fixtures()
    app_module.app.jinja_env.get_template("index.html")
    app_module.app.jinja_env.get_template("step3.html")
    app_module.app.jinja_env.get_template("step4.html")
    check("templates load", True)
    check(
        "large step4 form memory limit",
        int(app_module.app.config.get("MAX_FORM_MEMORY_SIZE", 0)) >= 128 * 1024 * 1024,
    )
    check(
        "large step4 form field limit",
        int(app_module.app.config.get("MAX_FORM_PARTS", 0)) >= 50000,
    )

    validate_inventory_imports()
    validate_manual_sizing_input()
    validate_step3_duplicate_removal()
    workbook_path, workflow_state = run_workflow_and_export()
    validate_pricing_invariants(workflow_state)
    validate_workbook(workbook_path)
    validate_price_list_dropdown_policy()
    print(f"REGRESSION_OK workbook={workbook_path}")


if __name__ == "__main__":
    main()
