from __future__ import annotations

import hashlib
import math
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from io import BytesIO
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
XLSM_INVENTORY = app_module.RVTOOLS_DIR / "regression_inventory.xlsm"
MOB_ID_INVENTORY = app_module.RVTOOLS_DIR / "mob_id_inventory.xlsx"
DUPLICATE_INVENTORY = app_module.RVTOOLS_DIR / "duplicate_inventory.csv"
INVENTORY_REVIEW_INVENTORY = app_module.RVTOOLS_DIR / "inventory_review.csv"
UNKNOWN_ONLY_INVENTORY = app_module.RVTOOLS_DIR / "unknown_only_inventory.csv"
LARGE_INVENTORY = app_module.RVTOOLS_DIR / "large_inventory_950.csv"
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


class WorkspaceMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.document_counts = {"html": 0, "head": 0, "body": 0}
        self.stage_items: list[dict[str, object]] = []
        self.mobile_options: list[dict[str, str | None]] = []
        self.footer_controls: list[dict[str, object]] = []
        self.roles: list[str] = []
        self.assessment_trigger: dict[str, str | None] | None = None
        self.assessment_panel: dict[str, str | None] | None = None
        self.assessment_import: dict[str, object] | None = None
        self.assessment_export: dict[str, object] | None = None
        self._in_stage_select = False
        self._in_stage_footer = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set(str(attributes.get("class", "")).split())
        if tag in self.document_counts:
            self.document_counts[tag] += 1
        if attributes.get("role"):
            self.roles.append(str(attributes["role"]))
        if "stage-nav__link" in classes:
            self.stage_items.append({"tag": tag, "attrs": attributes})
        if tag == "select" and attributes.get("id") == "workspace-stage-select":
            self._in_stage_select = True
        elif tag == "option" and self._in_stage_select:
            self.mobile_options.append(attributes)
        if tag == "footer" and "workspace-stage-actions" in classes:
            self._in_stage_footer = True
        elif self._in_stage_footer and "workspace-action" in classes:
            self.footer_controls.append({"tag": tag, "attrs": attributes})
        if "data-assessment-menu-trigger" in attributes:
            self.assessment_trigger = attributes
        if "data-assessment-menu-panel" in attributes:
            self.assessment_panel = attributes
        if "data-assessment-import" in attributes:
            self.assessment_import = {"tag": tag, "attrs": attributes}
        if "data-assessment-export" in attributes:
            self.assessment_export = {"tag": tag, "attrs": attributes}

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._in_stage_select:
            self._in_stage_select = False
        if tag == "footer" and self._in_stage_footer:
            self._in_stage_footer = False


def parse_workspace_markup(response_data: bytes) -> WorkspaceMarkupParser:
    parser = WorkspaceMarkupParser()
    parser.feed(response_data.decode("utf-8", errors="replace"))
    parser.close()
    return parser


class VisibleTextOutsideDetailsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self._details_depth = 0
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "details":
            self._details_depth += 1
        if tag in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "details" and self._details_depth:
            self._details_depth -= 1
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._details_depth and not self._ignored_depth:
            clean_text = " ".join(data.split())
            if clean_text:
                self.text_parts.append(clean_text)


def visible_text_outside_details(response_data: bytes) -> str:
    parser = VisibleTextOutsideDetailsParser()
    parser.feed(response_data.decode("utf-8", errors="replace"))
    parser.close()
    return " ".join(parser.text_parts)


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
    inventory_review_rows = [
        ["VM", "Powerstate", "Template", "OS according to the configuration file", "CPUs", "Memory", "Provisioned MiB"],
        ["review-supported", "poweredOn", "False", "Microsoft Windows Server 2019 (64-bit)", "4", "8192", "102400"],
        ["review-unsupported", "poweredOff", "False", "Microsoft Windows Server 2008 (64-bit)", "2", "4096", "51200"],
        ["review-unknown", "poweredOn", "False", "Unknown", "2", "4096", "20480"],
        ["review-critical", "poweredOn", "False", "Ubuntu Linux (64-bit)", "2", "4096", "0"],
    ]
    INVENTORY_REVIEW_INVENTORY.write_text(
        "\n".join(",".join(value for value in row) for row in inventory_review_rows) + "\n",
        encoding="utf-8",
    )
    unknown_only_rows = [
        ["VM", "Powerstate", "Template", "OS according to the configuration file", "CPUs", "Memory", "Provisioned MiB"],
        ["unknown-only-vm", "poweredOn", "False", "Unknown", "2", "4096", "51200"],
    ]
    UNKNOWN_ONLY_INVENTORY.write_text(
        "\n".join(",".join(value for value in row) for row in unknown_only_rows) + "\n",
        encoding="utf-8",
    )
    large_inventory_rows = [
        ["VM", "Powerstate", "Template", "OS according to the configuration file", "CPUs", "Memory", "Provisioned MiB"]
    ]
    large_inventory_rows.extend(
        [
            f"large-vm-{index + 1:04d}",
            "poweredOn" if index % 2 == 0 else "poweredOff",
            "False",
            "Oracle Linux 8 (64-bit)",
            "4",
            "8192",
            "102400",
        ]
        for index in range(950)
    )
    LARGE_INVENTORY.write_text(
        "\n".join(",".join(value for value in row) for row in large_inventory_rows) + "\n",
        encoding="utf-8",
    )
    xlsx_bytes = app_module._build_xlsx_workbook_bytes(
        [{"name": "vInfo", "rows": inventory_rows}],
        currency_fmt_code='€#,##0.00',
    )
    XLSX_INVENTORY.write_bytes(xlsx_bytes)
    XLSM_INVENTORY.write_bytes(xlsx_bytes)
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


def validate_shared_workspace_shell() -> None:
    price_file = find_price_file()
    inventory_rows, _ = app_module.load_vms_from_vinfo(str(CSV_INVENTORY))
    state_id = f"workspace_shell_{uuid4().hex}"
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        state = app_module.load_app_state()
        state["selected_vm_names"] = [str(row["name"]) for row in inventory_rows]
        state["step4_hybrid_placements"] = {
            "vm-app-01": "native",
            "vm-db-01": "native",
            "vm-web-01": "native",
            "vm-legacy-01": "ocvs",
        }
        state["acknowledged_warning_ids"] = ["unsupported-native"]
        app_module.save_app_state(state)

    shell_fragments = [
        b'<header class="workspace-header">',
        b'<nav class="stage-nav" aria-label="Assessment stages">',
        b'<main id="main-workspace">',
        b'<div id="workspace-status" role="status" aria-live="polite">',
    ]
    old_color_explanation = (
        b"Green/teal marks ready and recommended actions. Amber marks review items. "
        b"Oracle red stays as a restrained brand accent."
    )

    with app_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["state_id"] = state_id
            sess["selected_rvtools_file"] = str(CSV_INVENTORY)
            sess["selected_pricelist_file"] = price_file
            sess["selected_currency"] = "EUR"
            sess["customer_name"] = "Workspace Shell Customer"

        for route, progress_text in [
            ("/", b"Step 1 of 4"),
            ("/step3", b"Step 2 of 4"),
            ("/step4?tab=native", b"Step 3 of 4"),
            ("/step4?tab=price", b"Step 4 of 4"),
        ]:
            response = client.get(route)
            check(
                f"{route} shared workspace shell",
                response.status_code == 200 and all(fragment in response.data for fragment in shell_fragments),
                f"status={response.status_code}",
            )
            check(f"{route} workspace progress", progress_text in response.data)
            check(f"{route} old color explanation removed", old_color_explanation not in response.data)


def validate_unsupported_currency_workspace_shell() -> None:
    with app_module.app.test_client() as client:
        response = client.post(
            "/",
            data={"action": "download_pricing", "currency_code": "ZZZ"},
        )

    check(
        "unsupported currency keeps Stage 1 workspace shell",
        response.status_code == 200
        and b'<header class="workspace-header">' in response.data
        and b'<nav class="stage-nav" aria-label="Assessment stages">' in response.data
        and b'<main id="main-workspace">' in response.data
        and b'<div id="workspace-status" role="status" aria-live="polite">' in response.data
        and b"Step 1 of 4" in response.data
        and b"Please select a supported currency." in response.data,
        f"status={response.status_code}",
    )


def validate_pricing_fallback_filename_concealment() -> None:
    price_file = find_price_file()
    original_fetch_oci_price_list = app_module.fetch_oci_price_list

    def reject_live_pricing(_currency_code: str) -> dict[str, object]:
        raise ValueError("Regression fallback trigger")

    app_module.fetch_oci_price_list = reject_live_pricing
    try:
        with app_module.app.test_client() as client:
            response = client.post(
                "/",
                data={"action": "download_pricing", "currency_code": "EUR"},
            )
    finally:
        app_module.fetch_oci_price_list = original_fetch_oci_price_list

    html = response.data.decode("utf-8")
    fallback_flash = re.search(
        r"Live EUR price-list download did not complete\.\s*Using existing local EUR price list[^<]*",
        html,
    )
    fallback_text = fallback_flash.group(0) if fallback_flash else ""
    check(
        "local pricing fallback flash hides the source filename",
        response.status_code == 200
        and fallback_flash is not None
        and Path(price_file).name not in fallback_text
        and fallback_text.endswith("price list."),
        fallback_text,
    )


def validate_catalog_choice_tokens() -> None:
    duplicate_name = "shared_catalog_inventory.csv"
    duplicate_files = [
        app_module.RVTOOLS_DIR / "duplicate-source-a" / duplicate_name,
        app_module.RVTOOLS_DIR / "duplicate-source-b" / duplicate_name,
    ]
    inserted_file = app_module.RVTOOLS_DIR / "000-inserted-source" / "inserted_inventory.csv"
    for duplicate_file in duplicate_files:
        duplicate_file.parent.mkdir(parents=True, exist_ok=True)
        duplicate_file.write_bytes(CSV_INVENTORY.read_bytes())

    def expected_token(path_text: str) -> str:
        normalized = str(path_text).strip().replace("\\", "/")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        return f"catalog-{digest}"

    try:
        initial_inventory_paths = app_module.list_rvtools_export_files()
        duplicate_paths = [
            path_text for path_text in initial_inventory_paths if Path(path_text).name == duplicate_name
        ]
        initial_inventory_choices = app_module.build_catalog_choices(initial_inventory_paths, "inventory")
        initial_token_by_path = {
            str(choice.get("file_path", "")): str(choice.get("token", ""))
            for choice in initial_inventory_choices
        }
        expected_duplicate_tokens = {
            path_text: expected_token(path_text)
            for path_text in duplicate_paths
        }

        inserted_file.parent.mkdir(parents=True, exist_ok=True)
        inserted_file.write_bytes(CSV_INVENTORY.read_bytes())
        inventory_paths = app_module.list_rvtools_export_files()
        reordered_inventory_choices = app_module.build_catalog_choices(inventory_paths, "inventory")
        reordered_token_by_path = {
            str(choice.get("file_path", "")): str(choice.get("token", ""))
            for choice in reordered_inventory_choices
        }
        expected_inventory_tokens = [expected_token(path_text) for path_text in inventory_paths]
        inventory_tokens = [choice.get("token", "") for choice in reordered_inventory_choices]
        duplicate_token_resolutions = {
            path_text: app_module.resolve_catalog_selection(token, inventory_paths)
            for path_text, token in expected_duplicate_tokens.items()
        }
        removed_path = duplicate_paths[0] if duplicate_paths else ""
        removed_path_result = app_module.resolve_catalog_selection(
            expected_duplicate_tokens.get(removed_path, ""),
            [path_text for path_text in inventory_paths if path_text != removed_path],
        )
        tampered_token = ""
        tampered_result = ""
        if duplicate_paths:
            original_token = expected_duplicate_tokens[duplicate_paths[0]]
            replacement = "0" if original_token[-1] != "0" else "1"
            tampered_token = f"{original_token[:-1]}{replacement}"
            tampered_result = app_module.resolve_catalog_selection(tampered_token, inventory_paths)

        price_paths = app_module.list_downloaded_price_lists()[: app_module.MAX_VISIBLE_PRICE_LISTS]
        price_choices = app_module.build_catalog_choices(price_paths, "pricing")
        expected_price_tokens = [expected_token(path_text) for path_text in price_paths]
        price_tokens = [choice.get("token", "") for choice in price_choices]

        malformed_tokens = [
            "catalog-",
            "catalog-x",
            "catalog--1",
            "catalog-01",
            "catalog-0123456789abcdef0123456",
            "catalog-0123456789abcdef012345678",
            "catalog-0123456789ABCDEF01234567",
        ]
        malformed_results = {
            token: app_module.resolve_catalog_selection(token, inventory_paths)
            for token in malformed_tokens
        }
        exact_path_results = {
            path_text: app_module.resolve_catalog_selection(path_text, inventory_paths)
            for path_text in duplicate_paths
        }
        unique_basename_result = app_module.resolve_catalog_selection(
            CSV_INVENTORY.name,
            inventory_paths,
        )
        duplicate_basename_result = app_module.resolve_catalog_selection(
            duplicate_name,
            inventory_paths,
        )
        outside_path = "/private/tmp/outside-allowlist/shared_catalog_inventory.csv"
        outside_exact_result = app_module.resolve_catalog_selection(outside_path, inventory_paths)
        outside_basename_result = app_module.resolve_catalog_selection(
            "outside_inventory.csv",
            inventory_paths,
        )

        with app_module.app.test_client() as client:
            response = client.get("/")
            html = response.data.decode("utf-8")
            inventory_select = re.search(r'<select[^>]*id="rvtools_file".*?</select>', html, re.S)
            inventory_option_values = (
                re.findall(r'<option[^>]*value="([^"]*)"', inventory_select.group(0))
                if inventory_select
                else []
            )
            price_select = re.search(r'<select[^>]*id="price_list_file".*?</select>', html, re.S)
            price_option_values = (
                re.findall(r'<option[^>]*value="([^"]*)"', price_select.group(0))
                if price_select
                else []
            )

            duplicate_route_results: dict[str, str] = {}
            for path_text, token in expected_duplicate_tokens.items():
                client.post(
                    "/",
                    data={
                        "action": "select_rvtools_file",
                        "inventory_mode": "upload",
                        "rvtools_file": token,
                    },
                )
                with client.session_transaction() as sess:
                    duplicate_route_results[path_text] = str(sess.get("selected_rvtools_file", ""))

            selected_price_path = price_paths[0] if price_paths else ""
            selected_price_token = expected_price_tokens[0] if expected_price_tokens else ""
            if selected_price_token:
                client.post(
                    "/",
                    data={
                        "action": "select_pricelist",
                        "price_list_file": selected_price_token,
                    },
                )
            with client.session_transaction() as sess:
                selected_price_result = str(sess.get("selected_pricelist_file", ""))

        token_contract = (
            len(duplicate_paths) == 2
            and inventory_tokens == expected_inventory_tokens
            and all(initial_token_by_path.get(path_text) == token for path_text, token in expected_duplicate_tokens.items())
            and all(reordered_token_by_path.get(path_text) == token for path_text, token in expected_duplicate_tokens.items())
            and duplicate_token_resolutions == {path_text: path_text for path_text in duplicate_paths}
            and removed_path_result == ""
            and tampered_result == ""
            and price_tokens == expected_price_tokens
            and all(result == "" for result in malformed_results.values())
            and exact_path_results == {path_text: path_text for path_text in duplicate_paths}
            and unique_basename_result == str(CSV_INVENTORY).replace("\\", "/")
            and duplicate_basename_result == ""
            and outside_exact_result == ""
            and outside_basename_result == ""
            and all(token in inventory_option_values for token in expected_duplicate_tokens.values())
            and duplicate_name not in inventory_option_values
            and all(path_text not in inventory_option_values for path_text in duplicate_paths)
            and all(token in price_option_values for token in expected_price_tokens)
            and all(Path(path_text).name not in price_option_values for path_text in price_paths)
            and duplicate_route_results == {path_text: path_text for path_text in duplicate_paths}
            and selected_price_result == selected_price_path
        )
        check(
            "opaque catalog tokens select duplicate basenames independently",
            token_contract,
            json.dumps(
                {
                    "inventory_tokens": inventory_tokens,
                    "expected_inventory_tokens": expected_inventory_tokens,
                    "initial_token_by_path": initial_token_by_path,
                    "reordered_token_by_path": reordered_token_by_path,
                    "duplicate_token_resolutions": duplicate_token_resolutions,
                    "removed_path_result": removed_path_result,
                    "tampered_token": tampered_token,
                    "tampered_result": tampered_result,
                    "malformed_results": malformed_results,
                    "outside_exact_result": outside_exact_result,
                    "outside_basename_result": outside_basename_result,
                    "inventory_option_values": inventory_option_values,
                    "duplicate_route_results": duplicate_route_results,
                    "price_tokens": price_tokens,
                    "price_option_values": price_option_values,
                    "selected_price_result": selected_price_result,
                    "selected_price_path": selected_price_path,
                },
                sort_keys=True,
            ),
        )
    finally:
        for temporary_file in [*duplicate_files, inserted_file]:
            temporary_file.unlink(missing_ok=True)
            try:
                temporary_file.parent.rmdir()
            except OSError:
                pass


def validate_atomic_app_state_write() -> None:
    state_id = f"atomic_state_{uuid4().hex}"
    secret_path = "/private/tmp/private-state/atomic-state.json"
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        original_state = app_module._default_app_state()
        original_state["selected_vm_names"] = ["original-vm"]
        app_module.save_app_state(original_state)
        state_file = app_module._state_file_path()
        original_bytes = state_file.read_bytes()

        original_replace = app_module.os.replace
        replace_sources: list[str] = []

        def reject_atomic_replace(source: object, destination: object) -> None:
            replace_sources.append(str(source))
            raise OSError(secret_path)

        app_module.os.replace = reject_atomic_replace
        raised = ""
        try:
            replacement_state = app_module._default_app_state()
            replacement_state["selected_vm_names"] = ["replacement-vm"]
            try:
                app_module.save_app_state(replacement_state)
            except OSError as exc:
                raised = str(exc)
        finally:
            app_module.os.replace = original_replace

        temporary_files = list(state_file.parent.glob(f".{state_file.name}.*.tmp"))
        check(
            "app state writes are atomic and clean failed temporary files",
            bool(raised)
            and secret_path in raised
            and bool(replace_sources)
            and state_file.read_bytes() == original_bytes
            and not temporary_files,
            f"raised={raised!r}, replace_sources={replace_sources}, temporary_files={temporary_files}",
        )


def validate_transactional_inventory_activation() -> None:
    secret_path = "/private/tmp/private-state/activation-state.json"
    candidate_name = f"transaction_candidate_{uuid4().hex}.csv"
    candidate_path = app_module.RVTOOLS_DIR / candidate_name
    preserved_keys = [
        "active_assessment_id",
        "active_assessment_name",
        "active_assessment_notes",
        "selected_pricelist_file",
        "selected_currency",
        "selected_rvtools_file",
        "rvtools_file_info",
        "rvtools_import_summary",
    ]

    with app_module.app.test_client() as client:
        client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "inventory_mode": "manual",
                "manual_vm_count": "3",
                "manual_total_vcpus": "12",
                "manual_total_memory_gb": "48",
                "manual_total_storage_gb": "600",
                "manual_supported_vm_count": "2",
                "manual_unsupported_vm_count": "1",
            },
        )
        with client.session_transaction() as sess:
            sess["active_assessment_id"] = "transaction-preserved"
            sess["active_assessment_name"] = "Transaction preserved"
            sess["active_assessment_notes"] = "Keep this identity."
            sess["selected_pricelist_file"] = find_price_file()
            sess["selected_currency"] = "EUR"
            state_id = str(sess.get("state_id", ""))
            prior_source = str(sess.get("selected_rvtools_file", ""))
            prior_session = json.loads(json.dumps({key: sess.get(key) for key in preserved_keys}))

        prior_state = app_module.load_app_state()
        prior_state["selected_vm_names"] = ["manual-vm-001", "manual-vm-003"]
        prior_state["step4_hybrid_placements"] = {
            "manual-vm-001": "native",
            "manual-vm-003": "ocvs",
        }
        app_module.save_app_state(prior_state)
        prior_state = app_module.load_app_state()
        state_file = app_module.APP_STATE_DIR / f"{state_id}.json"
        prior_state_bytes = state_file.read_bytes()
        prior_source_bytes = Path(prior_source).read_bytes()

        original_save_app_state = app_module.save_app_state

        def reject_replacement_state(_state: dict[str, object]) -> None:
            raise OSError(secret_path)

        app_module.save_app_state = reject_replacement_state
        response = None
        raised = ""
        try:
            try:
                response = client.post(
                    "/",
                    data={
                        "action": "upload_rvtools_file",
                        "inventory_mode": "upload",
                        "rvtools_upload": (BytesIO(CSV_INVENTORY.read_bytes()), candidate_name),
                    },
                    content_type="multipart/form-data",
                )
            except OSError as exc:
                raised = str(exc)
        finally:
            app_module.save_app_state = original_save_app_state

        with client.session_transaction() as sess:
            session_after = json.loads(json.dumps({key: sess.get(key) for key in preserved_keys}))
        state_after = app_module.load_app_state()
        visible_text = visible_text_outside_details(response.data) if response is not None else ""
        check(
            "inventory activation rolls back when app state persistence fails",
            response is not None
            and response.status_code == 200
            and not raised
            and session_after == prior_session
            and state_after == prior_state
            and state_file.read_bytes() == prior_state_bytes
            and Path(prior_source).read_bytes() == prior_source_bytes
            and not candidate_path.exists()
            and secret_path not in visible_text
            and Path(secret_path).name not in visible_text
            and "Inventory source could not be activated" in visible_text,
            json.dumps(
                {
                    "response_status": response.status_code if response is not None else None,
                    "raised": raised,
                    "session_after": session_after,
                    "candidate_exists": candidate_path.exists(),
                    "visible_text": visible_text,
                },
                sort_keys=True,
            ),
        )


def validate_owned_candidate_cleanup_protection() -> None:
    with app_module.app.test_client() as client:
        client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "inventory_mode": "manual",
                "manual_vm_count": "3",
                "manual_total_vcpus": "12",
                "manual_total_memory_gb": "48",
                "manual_total_storage_gb": "600",
                "manual_supported_vm_count": "2",
                "manual_unsupported_vm_count": "1",
            },
        )
        with client.session_transaction() as sess:
            active_manual_path = str(sess.get("selected_rvtools_file", ""))
        active_manual_file = Path(active_manual_path)
        active_manual_bytes = active_manual_file.read_bytes()

        original_manual_generator = app_module.create_manual_inventory_csv_from_form
        original_summary_builder = app_module.build_inventory_import_summary

        def return_active_manual_source() -> tuple[Path, list[str]]:
            return active_manual_file, []

        def reject_active_manual_source(_rows: list[dict[str, object]], _source: str) -> dict[str, object]:
            raise ValueError("Injected active-source validation failure")

        app_module.create_manual_inventory_csv_from_form = return_active_manual_source
        app_module.build_inventory_import_summary = reject_active_manual_source
        try:
            response = client.post(
                "/",
                data={
                    "action": "create_manual_inventory",
                    "inventory_mode": "manual",
                    "manual_vm_count": "3",
                    "manual_total_vcpus": "12",
                    "manual_total_memory_gb": "48",
                    "manual_total_storage_gb": "600",
                    "manual_supported_vm_count": "2",
                    "manual_unsupported_vm_count": "1",
                },
            )
        finally:
            app_module.create_manual_inventory_csv_from_form = original_manual_generator
            app_module.build_inventory_import_summary = original_summary_builder

        active_exists_after_failure = active_manual_file.exists()
        active_bytes_after_failure = active_manual_file.read_bytes() if active_exists_after_failure else b""
        with client.session_transaction() as sess:
            selected_after_failure = str(sess.get("selected_rvtools_file", ""))

        if not active_manual_file.exists():
            active_manual_file.parent.mkdir(parents=True, exist_ok=True)
            active_manual_file.write_bytes(active_manual_bytes)

        catalog_bytes = CSV_INVENTORY.read_bytes()
        app_module.build_inventory_import_summary = reject_active_manual_source
        try:
            client.post(
                "/",
                data={
                    "action": "select_rvtools_file",
                    "inventory_mode": "upload",
                    "rvtools_file": str(CSV_INVENTORY),
                },
            )
            catalog_preserved = CSV_INVENTORY.exists() and CSV_INVENTORY.read_bytes() == catalog_bytes
            client.post(
                "/",
                data={
                    "action": "upload_rvtools_file",
                    "inventory_mode": "upload",
                    "rvtools_upload": (BytesIO(catalog_bytes), CSV_INVENTORY.name),
                },
                content_type="multipart/form-data",
            )
            reused_upload_preserved = CSV_INVENTORY.exists() and CSV_INVENTORY.read_bytes() == catalog_bytes
        finally:
            app_module.build_inventory_import_summary = original_summary_builder

        check(
            "owned candidate cleanup never deletes the active or reused source",
            response.status_code == 200
            and active_exists_after_failure
            and active_bytes_after_failure == active_manual_bytes
            and selected_after_failure == active_manual_path
            and catalog_preserved
            and reused_upload_preserved,
            (
                f"active_exists={active_exists_after_failure}, selected={selected_after_failure}, "
                f"catalog_preserved={catalog_preserved}, reused_preserved={reused_upload_preserved}"
            ),
        )


def validate_stage1_safe_exception_messages() -> None:
    secret_paths = {
        "save": "/private/tmp/private-assessments/customer-alpha.json",
        "load": "/private/tmp/private-assessments/customer-load.json",
        "delete": "/private/tmp/private-assessments/customer-delete.json",
        "pricing": "/private/tmp/private-pricing/oci_pricing_EUR_private.json",
    }
    cases = [
        (
            "save",
            "save_current_assessment",
            {"action": "save_assessment", "assessment_name": "Safe failure", "assessment_notes": ""},
            "Assessment could not be saved. Try again.",
            "Stage 1 assessment save failed",
        ),
        (
            "load",
            "load_saved_assessment",
            {"action": "load_assessment", "assessment_id": "safe_failure"},
            "Saved assessment could not be loaded. Try again.",
            "Stage 1 assessment load failed",
        ),
        (
            "delete",
            "delete_saved_assessment",
            {"action": "delete_assessment", "assessment_id": "safe_failure"},
            "Saved assessment could not be deleted. Try again.",
            "Stage 1 assessment delete failed",
        ),
        (
            "pricing",
            "fetch_oci_price_list",
            {"action": "download_pricing", "currency_code": "EUR"},
            "The latest OCI price list could not be downloaded. Try again or use an existing local price list.",
            "Stage 1 pricing download failed",
        ),
    ]
    outcomes: dict[str, dict[str, object]] = {}
    logged_messages: list[str] = []
    original_logger_exception = app_module.app.logger.exception
    app_module.app.logger.exception = lambda message, *args, **kwargs: logged_messages.append(str(message))
    try:
        for case_name, attribute_name, form_data, expected_message, expected_log in cases:
            original = getattr(app_module, attribute_name)

            def raise_private_path(*_args: object, _path: str = secret_paths[case_name], **_kwargs: object) -> None:
                raise OSError(_path)

            setattr(app_module, attribute_name, raise_private_path)
            response = None
            raised = ""
            try:
                with app_module.app.test_client() as client:
                    try:
                        response = client.post("/", data=form_data)
                    except OSError as exc:
                        raised = str(exc)
            finally:
                setattr(app_module, attribute_name, original)

            visible_text = visible_text_outside_details(response.data) if response is not None else ""
            outcomes[case_name] = {
                "status": response.status_code if response is not None else None,
                "raised": raised,
                "safe": expected_message in visible_text,
                "path_hidden": secret_paths[case_name] not in visible_text,
                "basename_hidden": Path(secret_paths[case_name]).name not in visible_text,
                "logged": expected_log in logged_messages,
            }
    finally:
        app_module.app.logger.exception = original_logger_exception

    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    check(
        "Stage 1 exception flashes hide filesystem details and log failures",
        all(
            outcome.get("status") == 200
            and not outcome.get("raised")
            and outcome.get("safe")
            and outcome.get("path_hidden")
            and outcome.get("basename_hidden")
            and outcome.get("logged")
            for outcome in outcomes.values()
        )
        and 'elif action == "save_identity":' not in app_source,
        json.dumps({"outcomes": outcomes, "logged_messages": logged_messages}, sort_keys=True),
    )


def prepare_saved_assessment_load_fault_fixture(
    client: object,
    label: str,
) -> dict[str, object]:
    price_file = find_price_file()
    prior_price_file = app_module.DOWNLOADS_DIR / f"oci_pricing_USD_prior_{label}.json"
    prior_price_file.write_bytes(Path(price_file).read_bytes())

    client.post(
        "/",
        data={"action": "save_customer_name", "customer_name": f"Target Customer {label}"},
    )
    client.post(
        "/",
        data={"action": "select_pricelist", "price_list_file": price_file},
    )
    client.post(
        "/",
        data={
            "action": "create_manual_inventory",
            "inventory_mode": "manual",
            "manual_vm_count": "3",
            "manual_total_vcpus": "12",
            "manual_total_memory_gb": "48",
            "manual_total_storage_gb": "600",
            "manual_supported_vm_count": "2",
            "manual_unsupported_vm_count": "1",
        },
    )
    target_state = app_module.load_app_state()
    target_state["selected_vm_names"] = ["manual-vm-001", "manual-vm-002", "manual-vm-003"]
    target_state["step4_hybrid_placements"] = {"manual-vm-001": "ocvs"}
    target_state["assessor_recommendation"] = "hybrid"
    app_module.save_app_state(target_state)
    target_step4_snapshot = {
        "marker": f"target-step4-{label}",
        "selected_scenario": "hybrid",
    }
    app_module.save_step4_snapshot(target_step4_snapshot)

    client.post(
        "/",
        data={
            "action": "save_assessment",
            "assessment_name": f"Target Manual Assessment {label}",
            "customer_name": f"Target Customer {label}",
            "assessment_notes": f"Target notes {label}",
        },
    )
    with client.session_transaction() as sess:
        target_assessment_id = str(sess.get("active_assessment_id", ""))

    client.post(
        "/",
        data={
            "action": "select_rvtools_file",
            "inventory_mode": "upload",
            "rvtools_file": str(CSV_INVENTORY),
        },
    )
    client.post(
        "/",
        data={"action": "select_pricelist", "price_list_file": str(prior_price_file)},
    )

    prior_app_state = app_module.load_app_state()
    prior_app_state["selected_vm_names"] = ["vm-app-01", "vm-db-01"]
    prior_app_state["step4_hybrid_placements"] = {
        "vm-app-01": "native",
        "vm-db-01": "ocvs",
    }
    prior_app_state["assessor_recommendation"] = "native"
    app_module.save_app_state(prior_app_state)
    prior_app_state = app_module.load_app_state()

    prior_step4_snapshot = {
        "marker": f"prior-step4-{label}",
        "selected_scenario": "native",
    }
    app_module.save_step4_snapshot(prior_step4_snapshot)
    prior_preferences = {
        "last_selected_pricelist_file": str(prior_price_file).replace("\\", "/"),
        "last_selected_currency": "USD",
        "preserved_marker": f"preferences-{label}",
    }
    app_module.save_preferences(prior_preferences)

    with client.session_transaction() as sess:
        sess["active_assessment_id"] = f"prior-active-{label}"
        sess["active_assessment_name"] = f"Prior Upload Assessment {label}"
        sess["active_assessment_notes"] = f"Prior notes {label}"
        sess["customer_name"] = f"Prior Customer {label}"
        sess["last_export_file"] = f"/private/tmp/prior-export-{label}.xlsx"

    prior_response = client.get("/")
    with client.session_transaction() as sess:
        prior_session = json.loads(json.dumps(dict(sess)))
    target_snapshot_path = (
        app_module.APP_STATE_DIR / "saved_assessments" / f"{target_assessment_id}.json"
    )
    target_snapshot = json.loads(target_snapshot_path.read_text(encoding="utf-8"))
    return {
        "target_assessment_id": target_assessment_id,
        "target_snapshot_path": target_snapshot_path,
        "target_snapshot": target_snapshot,
        "target_step4_snapshot": target_step4_snapshot,
        "prior_session": prior_session,
        "prior_app_state": prior_app_state,
        "prior_step4_snapshot": prior_step4_snapshot,
        "prior_preferences": prior_preferences,
        "prior_mode_is_upload": (
            re.search(
                r'<input(?=[^>]*id="inventory-mode-upload")(?=[^>]*checked)[^>]*>',
                prior_response.data.decode("utf-8"),
            )
            is not None
        ),
    }


def assert_saved_load_fault_preserves_prior(
    client: object,
    fixture: dict[str, object],
    response: object,
    fault_name: str,
) -> None:
    with client.session_transaction() as sess:
        session_after = json.loads(json.dumps(dict(sess)))
    app_state_after = app_module.load_app_state()
    step4_after = app_module.load_step4_snapshot()
    preferences_after = app_module.load_preferences()
    response_html = response.data.decode("utf-8")
    visible_text = visible_text_outside_details(response.data)
    upload_checked = (
        re.search(
            r'<input(?=[^>]*id="inventory-mode-upload")(?=[^>]*checked)[^>]*>',
            response_html,
        )
        is not None
    )
    manual_panel = re.search(
        r'<div(?=[^>]*data-inventory-mode-panel="manual")[^>]*>',
        response_html,
        re.S,
    )
    manual_panel_tag = manual_panel.group(0) if manual_panel else ""
    check(
        fault_name,
        response.status_code == 200
        and "Saved assessment could not be loaded" in visible_text
        and session_after == fixture["prior_session"]
        and app_state_after == fixture["prior_app_state"]
        and step4_after == fixture["prior_step4_snapshot"]
        and preferences_after == fixture["prior_preferences"]
        and fixture["prior_mode_is_upload"] is True
        and upload_checked
        and 'aria-hidden="true"' in manual_panel_tag
        and re.search(r"\shidden(?:\s|>)", manual_panel_tag) is not None,
        json.dumps(
            {
                "status": response.status_code,
                "session_after": session_after,
                "prior_session": fixture["prior_session"],
                "app_state_after": app_state_after,
                "step4_after": step4_after,
                "preferences_after": preferences_after,
                "upload_checked": upload_checked,
                "manual_panel": manual_panel_tag,
                "visible_text": visible_text,
            },
            sort_keys=True,
            default=str,
        ),
    )


def validate_saved_assessment_load_save_state_failure() -> None:
    with app_module.app.test_client() as client:
        fixture = prepare_saved_assessment_load_fault_fixture(client, f"state-{uuid4().hex[:8]}")
        original_save_app_state = app_module.save_app_state

        def reject_staged_app_state(_state: dict[str, object]) -> None:
            raise OSError("/private/tmp/private-load-state/staged-app-state.json")

        app_module.save_app_state = reject_staged_app_state
        try:
            response = client.post(
                "/",
                data={
                    "action": "load_assessment",
                    "assessment_id": fixture["target_assessment_id"],
                },
            )
        finally:
            app_module.save_app_state = original_save_app_state

        assert_saved_load_fault_preserves_prior(
            client,
            fixture,
            response,
            "saved assessment load preserves everything when app state persistence fails",
        )
        Path(fixture["target_snapshot_path"]).unlink(missing_ok=True)


def validate_saved_assessment_load_step4_failure() -> None:
    with app_module.app.test_client() as client:
        fixture = prepare_saved_assessment_load_fault_fixture(client, f"step4-{uuid4().hex[:8]}")
        check(
            "saved load Step 4 fault fixture is nonempty",
            bool(fixture["target_snapshot"].get("step4_snapshot")),
            str(fixture["target_snapshot"].get("step4_snapshot")),
        )
        original_save_step4_snapshot = app_module.save_step4_snapshot

        def reject_staged_step4(_snapshot: dict[str, object]) -> None:
            raise OSError("/private/tmp/private-load-state/staged-step4.json")

        app_module.save_step4_snapshot = reject_staged_step4
        try:
            response = client.post(
                "/",
                data={
                    "action": "load_assessment",
                    "assessment_id": fixture["target_assessment_id"],
                },
            )
        finally:
            app_module.save_step4_snapshot = original_save_step4_snapshot

        assert_saved_load_fault_preserves_prior(
            client,
            fixture,
            response,
            "saved assessment load rolls back app state when Step 4 persistence fails",
        )
        Path(fixture["target_snapshot_path"]).unlink(missing_ok=True)


def validate_atomic_step4_snapshot_write() -> None:
    state_id = f"atomic_step4_{uuid4().hex}"
    secret_path = "/private/tmp/private-load-state/atomic-step4.json"
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        original_snapshot = {"marker": "original-step4"}
        app_module.save_step4_snapshot(original_snapshot)
        snapshot_file = app_module._step4_snapshot_file_path()
        original_bytes = snapshot_file.read_bytes()
        original_replace = app_module.os.replace
        replace_sources: list[str] = []

        def reject_step4_replace(source: object, destination: object) -> None:
            replace_sources.append(str(source))
            raise OSError(secret_path)

        app_module.os.replace = reject_step4_replace
        raised = ""
        try:
            try:
                app_module.save_step4_snapshot({"marker": "replacement-step4"})
            except OSError as exc:
                raised = str(exc)
        finally:
            app_module.os.replace = original_replace

        temporary_files = list(snapshot_file.parent.glob(f".{snapshot_file.name}.*.tmp"))
        check(
            "Step 4 snapshot writes are atomic and clean failed temporary files",
            bool(raised)
            and secret_path in raised
            and bool(replace_sources)
            and snapshot_file.read_bytes() == original_bytes
            and not temporary_files,
            f"raised={raised!r}, replace_sources={replace_sources}, temporary_files={temporary_files}",
        )


def validate_workspace_context_contracts() -> None:
    state_id = f"workspace_contract_{uuid4().hex}"
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        app_module.save_app_state(app_module._default_app_state())

        empty_setup = app_module.build_workspace_context("setup")
        check(
            "empty workspace prerequisite availability",
            [stage.get("available") for stage in empty_setup["workspace_stages"]]
            == [True, False, False, False]
            and [stage.get("is_disabled") for stage in empty_setup["workspace_stages"]]
            == [False, True, True, True]
            and empty_setup.get("workspace_continue_presentation") == "link"
            and empty_setup.get("workspace_continue_is_safe_link") is False
            and bool(empty_setup.get("workspace_continue_unavailable_message")),
            str(empty_setup),
        )

        app_module.session["selected_rvtools_file"] = str(CSV_INVENTORY)
        inventory_only_scenarios = app_module.build_workspace_context("scenarios")
        check(
            "current stage stays available without full readiness",
            [stage.get("available") for stage in inventory_only_scenarios["workspace_stages"]]
            == [True, True, True, False]
            and inventory_only_scenarios.get("workspace_continue_presentation") == "form"
            and inventory_only_scenarios.get("workspace_continue_is_safe_link") is False
            and bool(inventory_only_scenarios.get("workspace_continue_url")),
            str(inventory_only_scenarios),
        )

        state = app_module.load_app_state()
        state["selected_vm_names"] = ["vm-app-01"]
        app_module.save_app_state(state)
        configured_setup = app_module.build_workspace_context("setup")
        configured_inventory = app_module.build_workspace_context("inventory")
        check(
            "configured workspace prerequisite availability",
            [stage.get("available") for stage in configured_setup["workspace_stages"]]
            == [True, True, True, True]
            and configured_setup.get("workspace_continue_is_safe_link") is True
            and configured_inventory.get("workspace_continue_presentation") == "form"
            and configured_inventory.get("workspace_continue_is_safe_link") is False
            and bool(configured_inventory.get("workspace_continue_url"))
            and configured_setup.get("workspace_can_export") is True,
            f"setup={configured_setup}, inventory={configured_inventory}",
        )


def validate_workspace_shell_behavior() -> None:
    expected_urls = ["/", "/step3", "/step4?tab=native", "/step4?tab=price"]

    with app_module.app.test_client() as client:
        empty_response = client.get("/")
    empty_shell = parse_workspace_markup(empty_response.data)
    empty_stage_signature = [
        (
            item["tag"],
            item["attrs"].get("href"),
            item["attrs"].get("aria-current"),
            item["attrs"].get("aria-disabled"),
        )
        for item in empty_shell.stage_items
    ]
    empty_primary_controls = [
        item
        for item in empty_shell.footer_controls
        if "workspace-action--primary" in str(item["attrs"].get("class", "")).split()
    ]
    check(
        "empty Setup renders disabled prerequisite navigation",
        empty_response.status_code == 200
        and empty_shell.document_counts == {"html": 1, "head": 1, "body": 1}
        and empty_stage_signature
        == [
            ("a", "/", "step", None),
            ("span", None, None, "true"),
            ("span", None, None, "true"),
            ("span", None, None, "true"),
        ]
        and [option.get("value") for option in empty_shell.mobile_options] == expected_urls
        and ["disabled" in option for option in empty_shell.mobile_options] == [False, True, True, True]
        and len(empty_primary_controls) == 1
        and empty_primary_controls[0]["tag"] != "a"
        and empty_primary_controls[0]["attrs"].get("aria-disabled") == "true"
        and empty_shell.assessment_export is not None
        and empty_shell.assessment_export["tag"] == "span"
        and empty_shell.assessment_export["attrs"].get("aria-disabled") == "true",
        f"stages={empty_stage_signature}, options={empty_shell.mobile_options}, footer={empty_shell.footer_controls}",
    )
    check(
        "assessment actions use disclosure semantics",
        empty_shell.assessment_trigger is not None
        and empty_shell.assessment_trigger.get("aria-expanded") == "false"
        and empty_shell.assessment_trigger.get("aria-controls") == "assessment-menu-panel"
        and "aria-haspopup" not in empty_shell.assessment_trigger
        and empty_shell.assessment_panel is not None
        and empty_shell.assessment_panel.get("role") == "region"
        and "menu" not in empty_shell.roles
        and "menuitem" not in empty_shell.roles
        and empty_shell.assessment_import is not None
        and empty_shell.assessment_import["tag"] == "span"
        and empty_shell.assessment_import["attrs"].get("aria-disabled") == "true"
        and bool(empty_shell.assessment_import["attrs"].get("title")),
        f"trigger={empty_shell.assessment_trigger}, panel={empty_shell.assessment_panel}, roles={empty_shell.roles}",
    )

    inventory_rows, _ = app_module.load_vms_from_vinfo(str(CSV_INVENTORY))
    state_id = f"workspace_markup_{uuid4().hex}"
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        state = app_module.load_app_state()
        state["selected_vm_names"] = [str(row["name"]) for row in inventory_rows]
        state["step4_hybrid_placements"] = {
            "vm-app-01": "native",
            "vm-db-01": "native",
            "vm-web-01": "native",
            "vm-legacy-01": "ocvs",
        }
        state["acknowledged_warning_ids"] = ["unsupported-native"]
        app_module.save_app_state(state)

    with app_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["state_id"] = state_id
            sess["selected_rvtools_file"] = str(CSV_INVENTORY)
            sess["selected_currency"] = "EUR"
            sess["customer_name"] = "Workspace Contract Customer"

        configured_responses = [
            client.get("/"),
            client.get("/step3"),
            client.get("/step4?tab=native"),
        ]

    configured_shells = [parse_workspace_markup(response.data) for response in configured_responses]
    for index, (response, shell) in enumerate(zip(configured_responses, configured_shells)):
        check(
            f"configured workspace shell {index + 1} has one document and exact stage links",
            response.status_code == 200
            and shell.document_counts == {"html": 1, "head": 1, "body": 1}
            and [item["tag"] for item in shell.stage_items] == ["a", "a", "a", "a"]
            and [item["attrs"].get("href") for item in shell.stage_items] == expected_urls
            and [item["attrs"].get("aria-current") for item in shell.stage_items]
            == ["step" if stage_index == index else None for stage_index in range(4)]
            and ["disabled" in option for option in shell.mobile_options] == [False, False, False, False],
            f"status={response.status_code}, stages={shell.stage_items}, options={shell.mobile_options}",
        )

    setup_primary_links = [
        item
        for item in configured_shells[0].footer_controls
        if item["tag"] == "a"
        and "workspace-action--primary" in str(item["attrs"].get("class", "")).split()
    ]
    check(
        "configured Setup safely links to Inventory Review",
        len(setup_primary_links) == 1
        and setup_primary_links[0]["attrs"].get("href") == "/step3"
        and configured_shells[0].assessment_export is not None
        and configured_shells[0].assessment_export["tag"] == "a",
        str(configured_shells[0].footer_controls),
    )
    for stage_name, shell in zip(["Inventory Review", "Scenario Configuration"], configured_shells[1:]):
        check(
            f"{stage_name} has no footer anchor bypass",
            not any(
                item["tag"] == "a"
                and "workspace-action--primary" in str(item["attrs"].get("class", "")).split()
                for item in shell.footer_controls
            ),
            str(shell.footer_controls),
        )
    check(
        "form-driven stages retain their inner save controls",
        b'id="continue_step4_form"' in configured_responses[1].data
        and b"Save & Continue" in configured_responses[1].data
        and b'id="step4-form"' in configured_responses[2].data
        and b"Save Settings" in configured_responses[2].data,
    )


def validate_workspace_source_contracts() -> None:
    workspace_css = (ROOT / "static" / "css" / "workspace.css").read_text(encoding="utf-8")
    workspace_js = (ROOT / "static" / "js" / "workspace.js").read_text(encoding="utf-8")

    scenario_rule_patterns = [
        r"\.workspace-body button\.scenario-tab\s*\{[^}]*background:\s*var\(--tab-soft\);[^}]*border-color:\s*var\(--tab-accent\);[^}]*color:\s*var\(--tab-strong\);",
        r"\.workspace-body button\.scenario-tab:hover,\s*\.workspace-body button\.scenario-tab:focus-visible\s*\{[^}]*background:\s*var\(--tab-accent\);[^}]*border-color:\s*var\(--tab-accent\);[^}]*color:\s*#fff;",
        r"\.workspace-body button\.scenario-tab\.is-active\s*\{[^}]*background:\s*var\(--tab-strong\);[^}]*border-color:\s*var\(--tab-strong\);[^}]*color:\s*#fff;",
        r"\.workspace-body button\.scenario-tab\.is-active:hover,\s*\.workspace-body button\.scenario-tab\.is-active:focus-visible\s*\{[^}]*background:\s*var\(--tab-accent\);[^}]*border-color:\s*var\(--tab-accent\);[^}]*color:\s*#fff;",
    ]
    check(
        "late-loaded workspace scenario tab contrast overrides",
        all(re.search(pattern, workspace_css, re.S) for pattern in scenario_rule_patterns),
    )

    mobile_contract_patterns = [
        r"@media\s*\(max-width:\s*600px\)",
        r"\.workspace-body #main-workspace form[^\{]*\{[^}]*min-width:\s*0;[^}]*max-width:\s*100%;",
        r"\.workspace-body #main-workspace (?:input|select|textarea)[^\{]*\{[^}]*min-width:\s*0;[^}]*max-width:\s*100%;",
        r"\.workspace-body #main-workspace code\s*\{[^}]*overflow-wrap:\s*anywhere;[^}]*max-width:\s*100%;",
        r"\.workspace-body #main-workspace \.card[^\{]*\{[^}]*overflow-x:\s*auto;",
        r"\.workspace-body #main-workspace \.warning-review-card[^\{]*\{[^}]*overflow-x:\s*auto;",
    ]
    check(
        "390px workspace containment source contract",
        all(re.search(pattern, workspace_css, re.S) for pattern in mobile_contract_patterns),
    )
    check(
        "assessment disclosure JavaScript contract",
        '[role="menuitem"]' not in workspace_js
        and 'a[href]:not([aria-disabled="true"])' in workspace_js
        and 'event.key === "Escape"' in workspace_js
        and "closeMenu(true)" in workspace_js
        and "!menu.contains(event.target)" in workspace_js,
    )


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
        price_select = re.search(r'<select[^>]*id="price_list_file".*?</select>', html, re.S)
        price_option_count = (
            len(re.findall(r'<option value="catalog-[0-9a-f]{24}"', price_select.group(0)))
            if price_select
            else 0
        )
        check("price list dropdown capped at 10", price_option_count == 10, str(price_option_count))
        check(
            "currency list EMEA plus USD",
            all(f'value="{currency}"' in html for currency in ["USD", "EUR", "GBP", "CHF", "SEK", "NOK", "DKK"])
            and all(f'value="{currency}"' not in html for currency in ["AUD", "CAD", "JPY", "SGD"]),
        )


def validate_stage1_setup_redesign() -> None:
    price_file = find_price_file()

    with app_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["selected_pricelist_file"] = price_file
            sess["selected_currency"] = "EUR"

        response = client.get("/")
        html = response.data.decode("utf-8")
        assessment_section = re.search(
            r'<section[^>]+id="assessment-identity".*?</section>',
            html,
            re.S,
        )
        pricing_section = re.search(
            r'<section[^>]+id="oci-pricing".*?</section>',
            html,
            re.S,
        )
        inventory_section = re.search(
            r'<section[^>]+id="inventory-source".*?</section>',
            html,
            re.S,
        )

        assessment_html = assessment_section.group(0) if assessment_section else ""
        check(
            "Stage 1 Assessment Identity controls are separate",
            response.status_code == 200
            and "Assessment Identity" in assessment_html
            and 'id="assessment_name"' in assessment_html
            and 'id="customer_name"' in assessment_html
            and 'id="assessment_notes"' in assessment_html
            and "Assessment name" in assessment_html
            and "Customer / project name" in assessment_html
            and ">Notes<" in assessment_html,
        )
        identity_form = re.search(
            r'<form[^>]*>(?:(?!</form>).)*id="assessment_name".*?</form>',
            assessment_html,
            re.S,
        )
        identity_form_html = identity_form.group(0) if identity_form else ""
        check(
            "Assessment Identity form is the direct save authority",
            'name="assessment_name"' in identity_form_html
            and 'name="customer_name"' in identity_form_html
            and 'name="assessment_notes"' in identity_form_html
            and re.search(
                r'<button[^>]+name="action"[^>]+value="save_assessment"',
                identity_form_html,
            )
            and 'value="save_identity"' not in identity_form_html,
        )
        setup_section_ids = re.findall(
            r'<section[^>]+id="(assessment-identity|oci-pricing|inventory-source|saved-assessments)"',
            html,
        )
        check(
            "Stage 1 has exactly three top-level setup sections",
            setup_section_ids == ["assessment-identity", "oci-pricing", "inventory-source"]
            and 'id="saved-assessments"' in assessment_html
            and '<section id="saved-assessments"' not in html
            and assessment_html.count('value="save_assessment"') == 1
            and "Current assessment" not in assessment_html,
            str(setup_section_ids),
        )

        pricing_html = pricing_section.group(0) if pricing_section else ""
        source_details = re.findall(
            r'<details(?=[^>]*data-source-details)[^>]*>.*?</details>',
            html,
            re.S,
        )
        check(
            "Stage 1 OCI Pricing summary and collapsed source details render",
            "OCI Pricing" in pricing_html
            and "Active" in pricing_html
            and "EUR" in pricing_html
            and "Pricing entries" in pricing_html
            and "Source Details" in pricing_html
            and source_details
            and all(not re.match(r"<details[^>]*\sopen(?:\s|=|>)", details) for details in source_details),
        )

        inventory_html = inventory_section.group(0) if inventory_section else ""
        check(
            "Stage 1 inventory mode is a two-option radio control",
            "Inventory Source" in inventory_html
            and "<fieldset" in inventory_html
            and "<legend" in inventory_html
            and len(re.findall(r'name="inventory_mode"', inventory_html)) == 2
            and 'value="upload"' in inventory_html
            and 'value="manual"' in inventory_html,
        )

        details_pattern = r'<details(?=[^>]*data-source-details)[^>]*>.*?</details>'
        html_outside_source_details = re.sub(details_pattern, "", html, flags=re.S)
        check(
            "Stage 1 hides absolute local paths outside Source Details",
            str(app_module.DOWNLOADS_DIR) not in html_outside_source_details
            and str(app_module.RVTOOLS_DIR) not in html_outside_source_details,
        )
        initial_visible_text = visible_text_outside_details(response.data)
        known_inventory_filenames = [Path(path_text).name for path_text in app_module.list_rvtools_export_files()]
        check(
            "Stage 1 hides local filenames outside Source Details",
            Path(price_file).name not in initial_visible_text
            and all(file_name not in initial_visible_text for file_name in known_inventory_filenames),
            initial_visible_text,
        )
        check(
            "Stage 1 catalog options use friendly source labels",
            re.search(r"Saved price list 1 - \d{4}-\d{2}-\d{2}", initial_visible_text) is not None
            and "Saved inventory 1 - " in initial_visible_text,
            initial_visible_text,
        )

        response = client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "inventory_mode": "manual",
                "manual_vm_count": "6",
                "manual_total_vcpus": "25",
                "manual_total_memory_gb": "96",
                "manual_total_storage_gb": "1200",
                "manual_supported_vm_count": "5",
                "manual_unsupported_vm_count": "1",
            },
            follow_redirects=True,
        )
        manual_html = response.data.decode("utf-8")
        check(
            "existing manual summary stays editable with update action",
            response.status_code == 200
            and 'value="6"' in manual_html
            and 'value="25"' in manual_html
            and 'value="96"' in manual_html
            and 'value="1200"' in manual_html
            and 'value="5"' in manual_html
            and "Update Summary" in manual_html,
        )

        with client.session_transaction() as sess:
            sess["active_assessment_id"] = "preserved-assessment"
            sess["active_assessment_name"] = "Preserved assessment"
            sess["active_assessment_notes"] = "Keep these notes after a failed replacement."
            prior_selected_file = str(sess.get("selected_rvtools_file", ""))
            prior_file_info = dict(sess.get("rvtools_file_info", {}))
            prior_import_summary = dict(sess.get("rvtools_import_summary", {}))
            preserved_session_keys = [
                "active_assessment_id",
                "active_assessment_name",
                "active_assessment_notes",
                "selected_pricelist_file",
                "selected_currency",
                "selected_rvtools_file",
                "rvtools_file_info",
                "rvtools_import_summary",
            ]
            prior_session_state = json.loads(
                json.dumps({key: sess.get(key) for key in preserved_session_keys})
            )
        manual_visible_text = visible_text_outside_details(response.data)
        check(
            "active inventory filename stays inside Source Details",
            Path(prior_selected_file).name not in manual_visible_text,
            manual_visible_text,
        )
        prior_inventory_bytes = Path(prior_selected_file).read_bytes()
        prior_state = app_module.load_app_state()
        prior_state["selected_vm_names"] = ["manual-vm-001", "manual-vm-003", "manual-vm-006"]
        prior_state["step4_hybrid_placements"] = {
            "manual-vm-001": "native",
            "manual-vm-003": "ocvs",
            "manual-vm-006": "native",
        }
        app_module.save_app_state(prior_state)
        prior_state = app_module.load_app_state()

        invalid_name = f"invalid_replacement_{uuid4().hex}.csv"
        invalid_candidate = app_module.RVTOOLS_DIR / invalid_name
        response = client.post(
            "/",
            data={
                "action": "upload_rvtools_file",
                "inventory_mode": "upload",
                "rvtools_upload": (
                    BytesIO(b"Part,Description,Unit Price\nA1,Not VM inventory,100\n"),
                    invalid_name,
                ),
            },
            content_type="multipart/form-data",
        )
        error_html = response.data.decode("utf-8")
        with client.session_transaction() as sess:
            selected_file_after_error = str(sess.get("selected_rvtools_file", ""))
            file_info_after_error = dict(sess.get("rvtools_file_info", {}))
            import_summary_after_error = dict(sess.get("rvtools_import_summary", {}))
            session_state_after_error = json.loads(
                json.dumps({key: sess.get(key) for key in preserved_session_keys})
            )
        state_after_error = app_module.load_app_state()

        check(
            "invalid replacement preserves selected source and inventory state",
            response.status_code == 200
            and selected_file_after_error == prior_selected_file
            and file_info_after_error == prior_file_info
            and import_summary_after_error == prior_import_summary
            and session_state_after_error == prior_session_state
            and state_after_error.get("selected_vm_names") == prior_state.get("selected_vm_names")
            and state_after_error.get("step4_hybrid_placements") == prior_state.get("step4_hybrid_placements"),
            f"selected={selected_file_after_error}, state={state_after_error}",
        )
        check(
            "failed replacement keeps prior inventory and deletes candidate",
            Path(prior_selected_file).exists()
            and Path(prior_selected_file).read_bytes() == prior_inventory_bytes
            and not invalid_candidate.exists(),
            f"prior_exists={Path(prior_selected_file).exists()}, candidate_exists={invalid_candidate.exists()}",
        )
        check(
            "Stage 1 field errors are described and linked",
            'id="setup-error-summary"' in error_html
            and 'role="alert"' in error_html
            and 'tabindex="-1"' in error_html
            and 'href="#rvtools_upload"' in error_html
            and re.search(r'id="rvtools_upload"[^>]+aria-describedby="[^"]*rvtools_upload-error', error_html)
            and 'id="rvtools_upload-error"' in error_html,
        )

        manual_candidates_before = set((app_module.RVTOOLS_DIR / "manual").glob("manual_inventory_*.csv"))
        original_summary_builder = app_module.build_inventory_import_summary

        def reject_generated_manual_summary(vm_rows: list[dict[str, object]], source: str) -> dict[str, object]:
            if str(source).replace("\\", "/") != prior_selected_file:
                raise ValueError("Regression rejection after manual candidate generation.")
            return original_summary_builder(vm_rows, source)

        app_module.build_inventory_import_summary = reject_generated_manual_summary
        try:
            response = client.post(
                "/",
                data={
                    "action": "create_manual_inventory",
                    "inventory_mode": "manual",
                    "manual_vm_count": "7",
                    "manual_total_vcpus": "28",
                    "manual_total_memory_gb": "112",
                    "manual_total_storage_gb": "1400",
                    "manual_supported_vm_count": "6",
                    "manual_unsupported_vm_count": "1",
                },
            )
        finally:
            app_module.build_inventory_import_summary = original_summary_builder

        with client.session_transaction() as sess:
            session_state_after_manual_error = json.loads(
                json.dumps({key: sess.get(key) for key in preserved_session_keys})
            )
        state_after_manual_error = app_module.load_app_state()
        manual_candidates_after = set((app_module.RVTOOLS_DIR / "manual").glob("manual_inventory_*.csv"))
        check(
            "invalid manual update preserves complete active state",
            response.status_code == 200
            and session_state_after_manual_error == prior_session_state
            and state_after_manual_error == prior_state
            and Path(prior_selected_file).read_bytes() == prior_inventory_bytes,
            f"session={session_state_after_manual_error}, state={state_after_manual_error}",
        )
        check(
            "invalid manual update deletes its generated candidate",
            manual_candidates_after == manual_candidates_before,
            f"before={manual_candidates_before}, after={manual_candidates_after}",
        )

        response = client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "inventory_mode": "manual",
                "manual_vm_count": "5",
                "manual_total_vcpus": "20",
                "manual_total_memory_gb": "64",
                "manual_total_storage_gb": "500",
                "manual_supported_vm_count": "2",
                "manual_unsupported_vm_count": "2",
            },
        )
        manual_error_html = response.data.decode("utf-8")
        check(
            "manual field errors retain submitted values and link to summary",
            'href="#manual_supported_vm_count"' in manual_error_html
            and re.search(
                r'id="manual_supported_vm_count"[^>]+aria-describedby="[^"]*manual_supported_vm_count-error',
                manual_error_html,
            )
            and 'id="manual_supported_vm_count-error"' in manual_error_html
            and 'name="manual_vm_count"' in manual_error_html
            and 'value="5"' in manual_error_html,
        )

    setup_js = ROOT / "static" / "js" / "setup.js"
    source_details_template = ROOT / "templates" / "_source_details.html"
    check("Stage 1 setup assets exist", setup_js.is_file() and source_details_template.is_file())
    setup_js_text = setup_js.read_text(encoding="utf-8")
    check(
        "Stage 1 mode script preserves inactive values and manages panel state",
        'input[name="inventory_mode"]' in setup_js_text
        and ".hidden =" in setup_js_text
        and 'setAttribute("aria-hidden"' in setup_js_text
        and "errorSummary.focus(" in setup_js_text
        and ".value =" not in setup_js_text,
    )


def validate_stage1_identity_save_and_loaded_manual_mode() -> None:
    price_file = find_price_file()
    assessment_name = "Direct Identity Assessment"
    customer_name = "Direct Identity Customer"
    assessment_notes = "Saved from the visible identity form in one request."

    with app_module.app.test_client() as client:
        response = client.get("/")
        html = response.data.decode("utf-8")
        assessment_section = re.search(
            r'<section[^>]+id="assessment-identity".*?</section>',
            html,
            re.S,
        )
        assessment_html = assessment_section.group(0) if assessment_section else ""
        identity_form = re.search(
            r'<form[^>]*>(?:(?!</form>).)*id="assessment_name".*?</form>',
            assessment_html,
            re.S,
        )
        identity_form_html = identity_form.group(0) if identity_form else ""
        save_button = re.search(
            r'<button[^>]+name="action"[^>]+value="([^"]+)"[^>]*>\s*Save Assessment\s*</button>',
            identity_form_html,
            re.S,
        )
        submitted_action = save_button.group(1) if save_button else ""

        response = client.post(
            "/",
            data={
                "action": submitted_action,
                "assessment_name": assessment_name,
                "customer_name": customer_name,
                "assessment_notes": assessment_notes,
            },
        )
        with client.session_transaction() as sess:
            saved_assessment_id = str(sess.get("active_assessment_id", ""))
            saved_session_identity = {
                "name": str(sess.get("active_assessment_name", "")),
                "customer": str(sess.get("customer_name", "")),
                "notes": str(sess.get("active_assessment_notes", "")),
            }
        saved_snapshot_path = app_module.APP_STATE_DIR / "saved_assessments" / f"{saved_assessment_id}.json"
        saved_snapshot = (
            json.loads(saved_snapshot_path.read_text(encoding="utf-8"))
            if saved_snapshot_path.is_file()
            else {}
        )
        check(
            "identity Save Assessment button persists all visible values in one POST",
            response.status_code == 200
            and submitted_action == "save_assessment"
            and saved_session_identity
            == {"name": assessment_name, "customer": customer_name, "notes": assessment_notes}
            and saved_snapshot.get("name") == assessment_name
            and saved_snapshot.get("customer_name") == customer_name
            and saved_snapshot.get("notes") == assessment_notes,
            f"action={submitted_action}, session={saved_session_identity}, snapshot={saved_snapshot}",
        )

        client.post(
            "/",
            data={
                "action": "select_pricelist",
                "price_list_file": price_file,
            },
        )
        client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "inventory_mode": "manual",
                "manual_vm_count": "3",
                "manual_total_vcpus": "12",
                "manual_total_memory_gb": "48",
                "manual_total_storage_gb": "600",
                "manual_supported_vm_count": "2",
                "manual_unsupported_vm_count": "1",
            },
        )
        client.post(
            "/",
            data={
                "action": "save_assessment",
                "assessment_name": assessment_name,
                "customer_name": customer_name,
                "assessment_notes": assessment_notes,
            },
        )
        with client.session_transaction() as sess:
            saved_manual_path = str(sess.get("selected_rvtools_file", ""))

        response = client.post(
            "/",
            data={"action": "select_rvtools_file", "inventory_mode": "upload", "rvtools_file": str(CSV_INVENTORY)},
        )
        check(
            "upload mode active before loading saved manual assessment",
            response.status_code == 200
            and re.search(
                r'<input(?=[^>]*id="inventory-mode-upload")(?=[^>]*checked)[^>]*>',
                response.data.decode("utf-8"),
            )
            is not None,
        )

        response = client.post(
            "/",
            data={"action": "load_assessment", "assessment_id": saved_assessment_id},
        )
        loaded_html = response.data.decode("utf-8")
        manual_radio = re.search(
            r'<input(?=[^>]*id="inventory-mode-manual")(?=[^>]*checked)[^>]*>',
            loaded_html,
        )
        manual_panel = re.search(
            r'<div(?=[^>]*data-inventory-mode-panel="manual")[^>]*>',
            loaded_html,
            re.S,
        )
        manual_panel_tag = manual_panel.group(0) if manual_panel else ""
        with client.session_transaction() as sess:
            loaded_manual_path = str(sess.get("selected_rvtools_file", ""))
        check(
            "loading saved manual assessment recomputes visible inventory mode",
            response.status_code == 200
            and loaded_manual_path == saved_manual_path
            and manual_radio is not None
            and 'aria-hidden="false"' in manual_panel_tag
            and re.search(r"\shidden(?:\s|>)", manual_panel_tag) is None,
            f"loaded={loaded_manual_path}, panel={manual_panel_tag}",
        )

        client.post(
            "/",
            data={"action": "delete_assessment", "assessment_id": saved_assessment_id},
        )


def validate_inventory_imports() -> None:
    discovered_files = app_module.list_rvtools_export_files()
    check(
        "temporary inventory files hidden",
        str(OFFICE_LOCK_INVENTORY).replace("\\", "/") not in discovered_files,
        str(discovered_files),
    )

    accepted_files = [CSV_INVENTORY, XLSX_INVENTORY, XLSM_INVENTORY, MOB_ID_INVENTORY]
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


def validate_guided_inventory_review() -> None:
    rows, _ = app_module.load_vms_from_vinfo(str(INVENTORY_REVIEW_INVENTORY))
    vm_names = [str(row["name"]) for row in rows]
    issues = app_module.build_inventory_review_issues(rows)
    issues_by_id = {str(issue.get("id")): issue for issue in issues}
    expected_issue_fields = {
        "id",
        "title",
        "detail",
        "severity",
        "count",
        "default_action",
        "vm_names",
        "vm_rows",
        "hidden_count",
    }
    check(
        "inventory review issue contract and severities",
        all(expected_issue_fields.issubset(issue) for issue in issues)
        and issues_by_id.get("unsupported-native", {}).get("severity") == "advisory"
        and issues_by_id.get("unknown-os", {}).get("severity") == "advisory"
        and issues_by_id.get("missing-storage", {}).get("severity") == "critical",
        str(issues),
    )
    unknown_only_rows, _ = app_module.load_vms_from_vinfo(str(UNKNOWN_ONLY_INVENTORY))
    unknown_only_issues = app_module.build_inventory_review_issues(unknown_only_rows)
    check(
        "unknown OS belongs only to unknown advisory",
        [issue.get("id") for issue in unknown_only_issues] == ["unknown-os"]
        and unknown_only_issues[0].get("vm_names") == ["unknown-only-vm"]
        and unknown_only_issues[0].get("severity") == "advisory",
        str(unknown_only_issues),
    )

    state_id = f"guided_inventory_{uuid4().hex}"
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        state = app_module.load_app_state()
        state["selected_vm_names"] = ["review-critical"]
        state["step4_hybrid_placements"] = {
            "review-critical": "native",
            "removed-stale-vm": "ocvs",
        }
        state["acknowledged_warning_ids"] = ["stale-warning"]
        app_module.save_app_state(state)

    with app_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["state_id"] = state_id
            sess["selected_rvtools_file"] = str(INVENTORY_REVIEW_INVENTORY)

        response = client.post(
            "/step3",
            data=MultiDict(
                [
                    ("action", "save_inventory_review"),
                    ("included_vm_names", "review-unknown"),
                    ("included_vm_names", "review-unsupported"),
                    ("included_vm_names", "review-supported"),
                    ("acknowledged_warning_ids", "unknown-os"),
                    ("acknowledged_warning_ids", "unsupported-native"),
                    ("acknowledged_warning_ids", "missing-storage"),
                    ("acknowledged_warning_ids", "stale-warning"),
                ]
            ),
        )
        state = app_module.load_app_state()

        check(
            "inventory review replaces selected names in source order",
            response.status_code == 200
            and state.get("selected_vm_names")
            == ["review-supported", "review-unsupported", "review-unknown"],
            str(state.get("selected_vm_names")),
        )
        check(
            "inventory review placements persist for included names only",
            set(state.get("step4_hybrid_placements", {}))
            == {"review-supported", "review-unsupported", "review-unknown"},
            str(state.get("step4_hybrid_placements")),
        )
        check(
            "inventory review placement defaults follow support state",
            state.get("step4_hybrid_placements")
            == {
                "review-supported": "native",
                "review-unsupported": "ocvs",
                "review-unknown": "review",
            },
            str(state.get("step4_hybrid_placements")),
        )
        check(
            "inventory review keeps current advisory acknowledgments only",
            state.get("acknowledged_warning_ids") == ["unsupported-native", "unknown-os"],
            str(state.get("acknowledged_warning_ids")),
        )
        check(
            "inventory review never acknowledges critical warnings",
            "missing-storage" not in state.get("acknowledged_warning_ids", []),
            str(state.get("acknowledged_warning_ids")),
        )

        html = response.data.decode("utf-8", errors="replace")
        check(
            "Stage 2 renders one guided inventory control tree",
            html.count("<table") == 1
            and html.count('name="included_vm_names"') == len(rows)
            and html.count('class="inventory-row-details"') == len(rows)
            and 'id="inventory-include-0"' in html
            and 'id="inventory-placement-0"' in html
            and 'id="inventory-search"' in html
            and 'id="inventory-support-filter"' in html
            and 'id="inventory-power-filter"' in html
            and 'id="inventory-placement-filter"' in html
            and 'id="inventory-bulk-placement"' in html
            and 'data-select-all' in html
            and 'name="included_vm_names" type="checkbox"' in html
            and re.search(r'<th[^>]+aria-sort="none"[^>]*>\s*<button[^>]+data-sort=', html)
            and not re.search(r'<button[^>]+data-sort=[^>]+aria-sort=', html)
            and 'data-warning-filter="unsupported-native"' in html
            and 'id="inventory-undo"' in html
            and re.search(r'data-selection-status[^>]+role="status"[^>]+aria-live="polite"', html),
        )
        check(
            "Stage 2 removes legacy transfer and unsupported-image controls",
            "Available VMs (Left)" not in html
            and "Selected VMs (Right)" not in html
            and "remove_unsupported" not in html
            and "Remove all non OS supported images" not in html
            and "removable images" not in html.lower(),
        )
        check(
            "Stage 2 mobile details do not duplicate form controls",
            html.count('name="included_vm_names"') == len(rows)
            and len(re.findall(r'name="placement:[^"]+"', html)) == len(rows)
            and len(re.findall(r'id="inventory-include-[0-9]+"', html)) == len(rows)
            and len(re.findall(r'id="inventory-placement-[0-9]+"', html)) == len(rows)
            and all(f'id="{vm_name}"' not in html for vm_name in vm_names),
        )
        check(
            "warning filtering exposes affected VM treatment details",
            "review-unsupported" in html
            and "Detected value" in html
            and "Reason" in html
            and "Recommended treatment" in html
            and "Action" in html
            and "Affected VMs" in html,
        )

        preserved_state = json.loads(
            json.dumps(
                {
                    "selected_vm_names": state.get("selected_vm_names"),
                    "step4_hybrid_placements": state.get("step4_hybrid_placements"),
                    "acknowledged_warning_ids": state.get("acknowledged_warning_ids"),
                }
            )
        )
        response = client.post("/step3", data={"action": "remove_unsupported"})
        state_after_retired_action = app_module.load_app_state()
        check(
            "retired remove unsupported action cannot mutate scope",
            response.status_code == 200
            and b"no longer supported" in response.data
            and state_after_retired_action.get("selected_vm_names") == preserved_state["selected_vm_names"],
            str(state_after_retired_action.get("selected_vm_names")),
        )

        response = client.post(
            "/step3",
            data={"action": "save_inventory_review", "continue_to_scenarios": "1"},
        )
        state_after_empty_scope = app_module.load_app_state()
        check(
            "invalid empty inventory scope preserves prior saved state",
            response.status_code == 200
            and b"Include at least one VM" in response.data
            and {
                "selected_vm_names": state_after_empty_scope.get("selected_vm_names"),
                "step4_hybrid_placements": state_after_empty_scope.get("step4_hybrid_placements"),
                "acknowledged_warning_ids": state_after_empty_scope.get("acknowledged_warning_ids"),
            }
            == preserved_state,
            str(state_after_empty_scope),
        )

    unknown_state_id = f"guided_unknown_{uuid4().hex}"
    with app_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["state_id"] = unknown_state_id
            sess["selected_rvtools_file"] = str(UNKNOWN_ONLY_INVENTORY)
        response = client.post(
            "/step3",
            data=MultiDict(
                [
                    ("action", "save_inventory_review"),
                    ("included_vm_names", "unknown-only-vm"),
                    ("acknowledged_warning_ids", "unknown-os"),
                ]
            ),
        )
        unknown_state = app_module.load_app_state()
        check(
            "unknown-only inventory saves Review placement",
            response.status_code == 200
            and unknown_state.get("selected_vm_names") == ["unknown-only-vm"]
            and unknown_state.get("step4_hybrid_placements") == {"unknown-only-vm": "review"}
            and unknown_state.get("acknowledged_warning_ids") == ["unknown-os"],
            str(unknown_state),
        )

    def inventory_client(inventory_path: Path) -> tuple[object, str]:
        local_client = app_module.app.test_client()
        local_state_id = f"guided_continue_{uuid4().hex}"
        with local_client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["state_id"] = local_state_id
            sess["selected_rvtools_file"] = str(inventory_path)
        return local_client, local_state_id

    client, _ = inventory_client(CSV_INVENTORY)
    response = client.post(
        "/step3",
        data={"action": "save_inventory_review", "continue_to_scenarios": "1"},
    )
    check(
        "inventory review continue requires an included VM",
        response.status_code == 200 and b"Include at least one VM" in response.data,
        f"status={response.status_code}",
    )

    client, critical_state_id = inventory_client(INVENTORY_REVIEW_INVENTORY)
    response = client.post(
        "/step3",
        data=MultiDict(
            [
                ("action", "save_inventory_review"),
                ("continue_to_scenarios", "1"),
                ("included_vm_names", "review-supported"),
                ("placement:review-supported", "native"),
                ("acknowledged_warning_ids", "unsupported-native"),
                ("acknowledged_warning_ids", "unknown-os"),
            ]
        ),
    )
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = critical_state_id
        critical_blocked_state = app_module.load_app_state()
    check(
        "inventory review persists valid state while critical issues block Continue",
        response.status_code == 200
        and b"Resolve critical inventory issues" in response.data
        and critical_blocked_state.get("selected_vm_names") == ["review-supported"]
        and critical_blocked_state.get("step4_hybrid_placements") == {"review-supported": "native"}
        and critical_blocked_state.get("acknowledged_warning_ids") == ["unsupported-native", "unknown-os"],
        f"status={response.status_code}, state={critical_blocked_state}",
    )

    client, _ = inventory_client(CSV_INVENTORY)
    response = client.post(
        "/step3",
        data=MultiDict(
            [
                ("action", "save_inventory_review"),
                ("continue_to_scenarios", "1"),
                ("included_vm_names", "vm-app-01"),
                ("placement:vm-app-01", "native"),
            ]
        ),
    )
    check(
        "inventory review continue requires advisory acknowledgments",
        response.status_code == 200 and b"Acknowledge advisory warnings" in response.data,
        f"status={response.status_code}",
    )

    client, _ = inventory_client(CSV_INVENTORY)
    response = client.post(
        "/step3",
        data=MultiDict(
            [
                ("action", "save_inventory_review"),
                ("continue_to_scenarios", "1"),
                ("included_vm_names", "vm-app-01"),
                ("acknowledged_warning_ids", "unsupported-native"),
                ("placement:vm-app-01", "elsewhere"),
            ]
        ),
    )
    check(
        "inventory review continue requires valid included placements",
        response.status_code == 200 and b"Choose a valid placement" in response.data,
        f"status={response.status_code}",
    )

    client, _ = inventory_client(CSV_INVENTORY)
    response = client.post(
        "/step3",
        data=MultiDict(
            [
                ("action", "save_inventory_review"),
                ("continue_to_scenarios", "1"),
                ("included_vm_names", "vm-app-01"),
                ("acknowledged_warning_ids", "unsupported-native"),
                ("placement:vm-app-01", "native"),
            ]
        ),
    )
    check(
        "inventory review continue redirects only when ready",
        response.status_code in {302, 303}
        and response.headers.get("Location", "").endswith("/step4?tab=native"),
        f"status={response.status_code}, location={response.headers.get('Location')}",
    )

    inventory_js = (ROOT / "static" / "js" / "inventory-review.js").read_text(encoding="utf-8")
    check(
        "inventory controller uses bounded cached interactions",
        "const rowRecords" in inventory_js
        and "clearTimeout(searchTimer)" in inventory_js
        and "setTimeout" in inventory_js
        and "changedRecords" in inventory_js
        and "placementsByIndex" in inventory_js
        and "Object.prototype.hasOwnProperty.call(saved" in inventory_js
        and "snapshotState" not in inventory_js
        and '.closest("th")' in inventory_js,
    )


def validate_inventory_review_transactions_and_step4_boundary() -> None:
    def new_client(inventory_path: Path = CSV_INVENTORY) -> tuple[object, str]:
        local_client = app_module.app.test_client()
        local_state_id = f"guided_adversarial_{uuid4().hex}"
        with local_client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["state_id"] = local_state_id
            sess["selected_rvtools_file"] = str(inventory_path)
        return local_client, local_state_id

    def write_state(state_id: str, selected: list[str], placements: dict[str, str], acknowledgments: list[str]) -> dict[str, object]:
        with app_module.app.test_request_context("/"):
            app_module.session["state_id"] = state_id
            state = app_module.load_app_state()
            state["selected_vm_names"] = selected
            state["step4_hybrid_placements"] = placements
            state["acknowledged_warning_ids"] = acknowledgments
            app_module.save_app_state(state)
            return app_module.load_app_state()

    def read_state(state_id: str) -> dict[str, object]:
        with app_module.app.test_request_context("/"):
            app_module.session["state_id"] = state_id
            return app_module.load_app_state()

    client, state_id = new_client()
    prior_state = write_state(
        state_id,
        ["vm-app-01"],
        {"vm-app-01": "native"},
        ["unsupported-native"],
    )
    invalid_forms = {
        "unknown included VM": [
            ("included_vm_names", "vm-app-01"),
            ("included_vm_names", "missing-vm"),
            ("placement:vm-app-01", "native"),
        ],
        "duplicate included VM": [
            ("included_vm_names", "vm-app-01"),
            ("included_vm_names", "vm-app-01"),
            ("placement:vm-app-01", "native"),
        ],
        "duplicate placement field": [
            ("included_vm_names", "vm-app-01"),
            ("placement:vm-app-01", "native"),
            ("placement:vm-app-01", "ocvs"),
        ],
        "unknown placement field": [
            ("included_vm_names", "vm-app-01"),
            ("placement:vm-app-01", "native"),
            ("placement:not-in-inventory", "ocvs"),
        ],
        "invalid placement value": [
            ("included_vm_names", "vm-app-01"),
            ("placement:vm-app-01", "elsewhere"),
        ],
        "placement outside included scope": [
            ("included_vm_names", "vm-app-01"),
            ("placement:vm-app-01", "native"),
            ("placement:vm-db-01", "ocvs"),
        ],
    }
    for label, fields in invalid_forms.items():
        response = client.post(
            "/step3",
            data=MultiDict(
                [("action", "save_inventory_review"), *fields, ("acknowledged_warning_ids", "unsupported-native")]
            ),
        )
        state_after = read_state(state_id)
        check(
            f"inventory review rejects {label} transactionally",
            response.status_code == 200
            and b'id="inventory-errors"' in response.data
            and state_after == prior_state,
            str(state_after),
        )

    response = client.post(
        "/step3",
        data=MultiDict(
            [
                ("action", "save_inventory_review"),
                ("continue_to_scenarios", "1"),
                ("included_vm_names", "vm-db-01"),
                ("placement:vm-db-01", "native"),
            ]
        ),
    )
    state_after_not_ready = read_state(state_id)
    check(
        "inventory review persists valid state while advisories block Continue",
        response.status_code == 200
        and b"Acknowledge advisory warnings" in response.data
        and state_after_not_ready.get("selected_vm_names") == ["vm-db-01"]
        and state_after_not_ready.get("step4_hybrid_placements") == {"vm-db-01": "native"}
        and state_after_not_ready.get("acknowledged_warning_ids") == [],
        str(state_after_not_ready),
    )
    state_before_save_failure = state_after_not_ready

    original_save_app_state = app_module.save_app_state

    def reject_inventory_review_save(_state: dict[str, object]) -> None:
        raise OSError("/private/tmp/private-stage2-state.json")

    app_module.save_app_state = reject_inventory_review_save
    try:
        response = client.post(
            "/step3",
            data=MultiDict(
                [
                    ("action", "save_inventory_review"),
                    ("included_vm_names", "vm-db-01"),
                    ("placement:vm-db-01", "native"),
                    ("acknowledged_warning_ids", "unsupported-native"),
                ]
            ),
        )
    finally:
        app_module.save_app_state = original_save_app_state
    state_after_failure = read_state(state_id)
    check(
        "inventory review save failure renders safely without mutation",
        response.status_code == 200
        and b"could not be saved" in response.data.lower()
        and b"private-stage2-state" not in response.data
        and state_after_failure == state_before_save_failure,
        str(state_after_failure),
    )

    legacy_client, legacy_state_id = new_client()
    legacy_prior = write_state(legacy_state_id, [], {}, [])
    response = legacy_client.post(
        "/step3",
        data=MultiDict(
            [
                ("action", "add"),
                ("redirect_to", "step4"),
                ("vm_names", "vm-app-01"),
            ]
        ),
        follow_redirects=False,
    )
    legacy_state = read_state(legacy_state_id)
    check(
        "legacy inventory action cannot bypass review readiness",
        response.status_code == 200
        and b"Acknowledge advisory warnings" in response.data
        and legacy_state != legacy_prior
        and legacy_state.get("selected_vm_names") == ["vm-app-01"]
        and legacy_state.get("step4_hybrid_placements") == {"vm-app-01": "native"},
        f"status={response.status_code}, state={legacy_state}",
    )

    boundary_client, boundary_state_id = new_client()
    boundary_prior = write_state(
        boundary_state_id,
        ["vm-app-01"],
        {},
        ["unsupported-native"],
    )
    response = boundary_client.get("/step4", follow_redirects=False)
    check(
        "Step 4 boundary rejects incomplete inventory review",
        response.status_code in {302, 303}
        and response.headers.get("Location", "").endswith("/step3")
        and read_state(boundary_state_id) == boundary_prior,
        f"status={response.status_code}, location={response.headers.get('Location')}",
    )

    hybrid_client, hybrid_state_id = new_client()
    ready_state = write_state(
        hybrid_state_id,
        ["vm-app-01", "vm-legacy-01"],
        {"vm-app-01": "native", "vm-legacy-01": "ocvs"},
        ["unsupported-native"],
    )
    _rows, source_vinfo_csv = app_module.load_vms_from_vinfo(str(CSV_INVENTORY))
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = hybrid_state_id
        app_module.save_step4_snapshot(
            {
                "saved_at": "2026-01-02T03:04:05",
                "source_vinfo_csv": source_vinfo_csv,
                "vm_settings": {
                    "vm-app-01": {"hybrid_placement": "ocvs"},
                    "vm-legacy-01": {"hybrid_placement": "native"},
                },
            }
        )
    response = hybrid_client.get("/step4")
    state_after_snapshot = read_state(hybrid_state_id)
    check(
        "Step 4 snapshot cannot overwrite Stage 2 placements",
        response.status_code == 200
        and state_after_snapshot.get("step4_hybrid_placements")
        == ready_state.get("step4_hybrid_placements"),
        str(state_after_snapshot.get("step4_hybrid_placements")),
    )

    response = hybrid_client.post(
        "/step4",
        data=MultiDict(
            [
                ("action", "save"),
                ("active_scenario", "hybrid"),
                ("hybrid_placement:vm-app-01", "review"),
                ("hybrid_placement:vm-legacy-01", "native"),
            ]
        ),
        follow_redirects=True,
    )
    reviewed_state = read_state(hybrid_state_id)
    html = response.data.decode("utf-8", errors="replace")
    check(
        "Hybrid Review placement round-trips with explicit OCVS pricing",
        response.status_code == 200
        and reviewed_state.get("step4_hybrid_placements")
        == {"vm-app-01": "review", "vm-legacy-01": "native"}
        and 'name="hybrid_placement:vm-app-01"' in html
        and 'value="review" selected' in html
        and "Review (priced as OCVS)" in html,
        str(reviewed_state.get("step4_hybrid_placements")),
    )
    review_plan = app_module.build_hybrid_placement_plan(
        [{"vm_name": "vm-app-01", "os_name": "Microsoft Windows Server 2019 (64-bit)"}],
        {"vm-app-01": "review"},
        app_module.load_supported_os_signatures(),
    )
    check(
        "Review placement uses conservative OCVS pricing semantics",
        review_plan.get("review_count") == 1
        and review_plan.get("ocvs_priced_count") == 1
        and review_plan.get("rows", [{}])[0].get("hybrid_effective_target") == "ocvs",
        str(review_plan),
    )

    keyed_prior = read_state(hybrid_state_id)
    invalid_hybrid_forms = {
        "missing key": [("hybrid_placement:vm-app-01", "review")],
        "duplicate key": [
            ("hybrid_placement:vm-app-01", "review"),
            ("hybrid_placement:vm-app-01", "native"),
            ("hybrid_placement:vm-legacy-01", "native"),
        ],
        "unknown key": [
            ("hybrid_placement:vm-app-01", "review"),
            ("hybrid_placement:vm-legacy-01", "native"),
            ("hybrid_placement:not-selected", "ocvs"),
        ],
        "invalid value": [
            ("hybrid_placement:vm-app-01", "elsewhere"),
            ("hybrid_placement:vm-legacy-01", "native"),
        ],
        "legacy positional pairing": [
            ("hybrid_vm_name", "vm-app-01"),
            ("hybrid_placement", "review"),
            ("hybrid_vm_name", "vm-legacy-01"),
            ("hybrid_placement", "native"),
        ],
    }
    for label, fields in invalid_hybrid_forms.items():
        response = hybrid_client.post(
            "/step4",
            data=MultiDict([("action", "save"), ("active_scenario", "hybrid"), *fields]),
            follow_redirects=True,
        )
        state_after = read_state(hybrid_state_id)
        check(
            f"Hybrid keyed placements reject {label} without mutation",
            response.status_code == 200
            and b"valid placement for every included VM" in response.data
            and state_after == keyed_prior,
            f"status={response.status_code}, state={state_after}",
        )


def validate_large_inventory_review_containment() -> None:
    large_rows, _ = app_module.load_vms_from_vinfo(str(LARGE_INVENTORY))
    check("large inventory fixture has 950 VMs", len(large_rows) == 950, str(len(large_rows)))
    state_id = f"guided_large_{uuid4().hex}"
    with app_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["state_id"] = state_id
            sess["selected_rvtools_file"] = str(LARGE_INVENTORY)
        response = client.get("/step3")

    html = response.data.decode("utf-8", errors="replace")
    inventory_css = (ROOT / "static" / "css" / "inventory-review.css").read_text(encoding="utf-8")
    check(
        "950-row inventory keeps one bounded control tree",
        response.status_code == 200
        and html.count("<table") == 1
        and html.count('name="included_vm_names"') == 950
        and html.count('class="inventory-row-details"') == 950
        and 'class="inventory-table-wrap inventory-table-scroll"' in html,
        f"status={response.status_code}",
    )
    check(
        "desktop inventory table source is height constrained and sticky",
        re.search(
            r"\.inventory-table-wrap\s*\{[^}]*max-height:\s*clamp\([^;]+\);[^}]*overflow:\s*auto;",
            inventory_css,
            re.S,
        )
        is not None
        and re.search(r"#inventory-table thead th\s*\{[^}]*position:\s*sticky;", inventory_css, re.S)
        is not None
        and "#inventory-table tbody tr[data-inventory-row] .inventory-col-name" in inventory_css,
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
        check(
            "redwood setup shell renders",
            b"redwood-app-shell" in response.data
            and b"ORACLE" in response.data
            and b"Setup & Inventory" in response.data,
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
            and b"Selected VM Inventory" in response.data,
        )
        check(
            "manual sizing form prefilled after create",
            b"Update Summary" in response.data
            and b'name="manual_vm_count" type="number" min="1" step="1" value="6"' in response.data
            and b'name="manual_total_vcpus" type="number" min="1" step="1" value="25"' in response.data
            and b'name="manual_supported_vm_count" type="number" min="0" step="1" value="5"' in response.data,
        )
        check(
            "manual warning review lists affected vm",
            b"Warning Review" in response.data
            and b"Unsupported for OCI Native" in response.data
            and b"manual-vm-006" in response.data
            and b"Solaris 11.4" in response.data
            and b"Set OCVS" in response.data,
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


def _load_raw_app_state(raw_state: object) -> dict[str, object]:
    state_id = f"regression_{uuid4().hex}"
    state_file = app_module.APP_STATE_DIR / f"{state_id}.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(raw_state), encoding="utf-8")
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        return app_module.load_app_state()


def validate_app_state_review_inputs() -> None:
    old_state = _load_raw_app_state(
        {
            "selected_vm_names": ["legacy-vm"],
            "step4_ocvs_commitment_term": "3_year",
        }
    )
    check(
        "old app state gets review defaults",
        old_state.get("acknowledged_warning_ids") == []
        and old_state.get("assessor_recommendation") == ""
        and old_state.get("assessor_recommendation_rationale") == "",
        str(old_state),
    )
    check(
        "old app state preserves legacy values",
        old_state.get("selected_vm_names") == ["legacy-vm"]
        and old_state.get("step4_ocvs_commitment_term") == "3_year",
        str(old_state),
    )

    eighty_character_id = "a" + ("-" * 79)
    normalized = _load_raw_app_state(
        {
            "acknowledged_warning_ids": [
                "unsupported-native",
                "hybrid-cost-review",
                "unsupported-native",
                eighty_character_id,
                "Uppercase-invalid",
                "-leading-hyphen",
                "a" * 81,
                17,
                "",
            ],
            "assessor_recommendation": "native",
            "assessor_recommendation_rationale": (
                " \r\nFirst line\rSecond line\r\n" + ("x" * 5000) + " \r\n"
            ),
        }
    )
    check(
        "warning ids normalize uniquely in first-seen order",
        normalized.get("acknowledged_warning_ids")
        == ["unsupported-native", "hybrid-cost-review", eighty_character_id],
        str(normalized.get("acknowledged_warning_ids")),
    )
    rationale = normalized.get("assessor_recommendation_rationale")
    check(
        "recommendation rationale normalizes and truncates",
        isinstance(rationale, str)
        and len(rationale) == 4000
        and rationale.startswith("First line\nSecond line\n")
        and "\r" not in rationale
        and rationale == rationale.strip(),
        f"type={type(rationale).__name__}, length={len(rationale) if isinstance(rationale, str) else 'n/a'}",
    )
    boundary_rationale = _load_raw_app_state(
        {"assessor_recommendation_rationale": ("x" * 3999) + " " + "tail"}
    ).get("assessor_recommendation_rationale")
    check(
        "recommendation rationale strips whitespace exposed by truncation",
        boundary_rationale == ("x" * 3999),
        f"type={type(boundary_rationale).__name__}, length={len(boundary_rationale) if isinstance(boundary_rationale, str) else 'n/a'}, tail={repr(boundary_rationale[-5:]) if isinstance(boundary_rationale, str) else 'n/a'}",
    )

    for invalid_ids in ("unsupported-native", {"unsupported-native": True}, None, ["INVALID"]):
        invalid_collection_state = _load_raw_app_state({"acknowledged_warning_ids": invalid_ids})
        check(
            f"invalid warning collection becomes empty ({type(invalid_ids).__name__})",
            invalid_collection_state.get("acknowledged_warning_ids") == [],
            str(invalid_collection_state.get("acknowledged_warning_ids")),
        )

    for valid_recommendation in ("", "native", "ocvs", "hybrid"):
        valid_recommendation_state = _load_raw_app_state(
            {"assessor_recommendation": valid_recommendation}
        )
        check(
            f"valid recommendation retained ({valid_recommendation or 'empty'})",
            valid_recommendation_state.get("assessor_recommendation") == valid_recommendation,
            str(valid_recommendation_state.get("assessor_recommendation")),
        )

    for invalid_recommendation in ("NATIVE", "invalid", 17, None, ["native"]):
        invalid_recommendation_state = _load_raw_app_state(
            {"assessor_recommendation": invalid_recommendation}
        )
        check(
            f"invalid recommendation becomes empty ({type(invalid_recommendation).__name__})",
            invalid_recommendation_state.get("assessor_recommendation") == "",
            str(invalid_recommendation_state.get("assessor_recommendation")),
        )

    invalid_rationale_state = _load_raw_app_state(
        {"assessor_recommendation_rationale": {"text": "not a string"}}
    )
    check(
        "non-string recommendation rationale becomes empty",
        invalid_rationale_state.get("assessor_recommendation_rationale") == "",
        str(invalid_rationale_state.get("assessor_recommendation_rationale")),
    )

    snapshot_id = f"normalization_{uuid4().hex[:8]}"
    snapshot_path = app_module.APP_STATE_DIR / "saved_assessments" / f"{snapshot_id}.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "id": snapshot_id,
                "name": "Normalization check",
                "app_state": {
                    "acknowledged_warning_ids": [
                        "unsupported-native",
                        "unsupported-native",
                        "INVALID",
                    ],
                    "assessor_recommendation": "invalid",
                    "assessor_recommendation_rationale": " \r\nReviewed.\r ",
                },
            }
        ),
        encoding="utf-8",
    )
    restored_state_id = f"regression_{uuid4().hex}"
    restored_state_path = app_module.APP_STATE_DIR / f"{restored_state_id}.json"
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = restored_state_id
        load_result = app_module.load_saved_assessment(snapshot_id)
    persisted_state = json.loads(restored_state_path.read_text(encoding="utf-8"))
    check(
        "saved assessment writes normalized active state",
        load_result.get("ok") is True
        and persisted_state.get("acknowledged_warning_ids") == ["unsupported-native"]
        and persisted_state.get("assessor_recommendation") == ""
        and persisted_state.get("assessor_recommendation_rationale") == "Reviewed.",
        str(persisted_state),
    )
    snapshot_path.unlink()


def validate_saved_assessments() -> None:
    price_file = find_price_file()

    with app_module.app.test_client() as client:
        response = client.get("/")
        check(
            "empty saved assessment list still shows load controls",
            response.status_code == 200
            and b"Load Previous Assessment" in response.data
            and b"No saved assessments yet" in response.data
            and b'name="assessment_id"' in response.data,
        )

        client.post(
            "/",
            data={"action": "save_customer_name", "customer_name": "Saved Assessment Customer"},
            follow_redirects=True,
        )
        client.post(
            "/",
            data={"action": "select_pricelist", "price_list_file": price_file},
            follow_redirects=True,
        )
        response = client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "manual_vm_count": "3",
                "manual_total_vcpus": "12",
                "manual_total_memory_gb": "48",
                "manual_total_storage_gb": "600",
                "manual_supported_vm_count": "2",
                "manual_unsupported_vm_count": "1",
            },
            follow_redirects=True,
        )
        check("saved assessment source setup", response.status_code == 200 and b"Manual workload summary created" in response.data)

        with client.session_transaction() as sess:
            saved_inventory_file = str(sess.get("selected_rvtools_file", ""))

        state = app_module.load_app_state()
        state["step4_ocvs_commitment_term"] = "3_year"
        state["step4_iaas_discount_pct"] = 12.5
        state["step4_hybrid_placements"] = {"manual-vm-001": "native", "manual-vm-002": "ocvs"}
        state["acknowledged_warning_ids"] = ["unsupported-native"]
        state["assessor_recommendation"] = "native"
        state["assessor_recommendation_rationale"] = "Remediate legacy guests before migration."
        app_module.save_app_state(state)

        response = client.post(
            "/",
            data={
                "action": "save_assessment",
                "assessment_name": "Alpha Migration Review",
                "assessment_notes": "Sizing reviewed with customer architecture team.",
            },
            follow_redirects=True,
        )
        check(
            "saved assessment creates snapshot",
            response.status_code == 200
            and b"Assessment saved." in response.data
            and b"Alpha Migration Review" in response.data
            and b"Sizing reviewed with customer architecture team." in response.data,
        )

        saved_assessments = app_module.list_saved_assessments()
        saved_assessment = next(
            assessment for assessment in saved_assessments if assessment.get("name") == "Alpha Migration Review"
        )
        saved_assessment_id = str(saved_assessment["id"])
        saved_snapshot_path = app_module.APP_STATE_DIR / "saved_assessments" / f"{saved_assessment_id}.json"
        saved_snapshot = json.loads(saved_snapshot_path.read_text(encoding="utf-8"))
        check(
            "saved assessment nests review decisions in app state",
            saved_snapshot.get("app_state", {}).get("acknowledged_warning_ids") == ["unsupported-native"]
            and saved_snapshot.get("app_state", {}).get("assessor_recommendation") == "native"
            and saved_snapshot.get("app_state", {}).get("assessor_recommendation_rationale")
            == "Remediate legacy guests before migration."
            and "acknowledged_warning_ids" not in saved_snapshot
            and "assessor_recommendation" not in saved_snapshot
            and "assessor_recommendation_rationale" not in saved_snapshot,
            str(saved_snapshot),
        )

        client.post(
            "/",
            data={"action": "save_customer_name", "customer_name": "Mutated Active Assessment"},
            follow_redirects=True,
        )
        client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "manual_vm_count": "5",
                "manual_total_vcpus": "20",
                "manual_total_memory_gb": "80",
                "manual_total_storage_gb": "1000",
                "manual_supported_vm_count": "5",
                "manual_unsupported_vm_count": "0",
            },
            follow_redirects=True,
        )
        mutated_state = app_module.load_app_state()
        mutated_state["step4_ocvs_commitment_term"] = "payg"
        mutated_state["step4_iaas_discount_pct"] = 0.0
        mutated_state["step4_hybrid_placements"] = {}
        mutated_state["acknowledged_warning_ids"] = []
        mutated_state["assessor_recommendation"] = "ocvs"
        mutated_state["assessor_recommendation_rationale"] = "Changed after saving."
        app_module.save_app_state(mutated_state)

        response = client.post(
            "/",
            data={"action": "load_assessment", "assessment_id": saved_assessment_id},
            follow_redirects=True,
        )
        check(
            "saved assessment loads snapshot",
            response.status_code == 200
            and b"Assessment loaded." in response.data
            and b"Saved Assessment Customer" in response.data
            and b"Alpha Migration Review" in response.data
            and b'name="manual_vm_count" type="number" min="1" step="1" value="3"' in response.data
            and b'name="manual_total_vcpus" type="number" min="1" step="1" value="12"' in response.data,
        )

        with client.session_transaction() as sess:
            loaded_inventory_file = str(sess.get("selected_rvtools_file", ""))
            loaded_price_file = str(sess.get("selected_pricelist_file", ""))
            loaded_customer = str(sess.get("customer_name", ""))
            loaded_assessment_name = str(sess.get("active_assessment_name", ""))
            loaded_assessment_notes = str(sess.get("active_assessment_notes", ""))

        loaded_state = app_module.load_app_state()
        check("saved assessment inventory restored", loaded_inventory_file == saved_inventory_file, loaded_inventory_file)
        check("saved assessment price list restored", loaded_price_file == price_file, loaded_price_file)
        check("saved assessment customer restored", loaded_customer == "Saved Assessment Customer", loaded_customer)
        check("saved assessment name restored", loaded_assessment_name == "Alpha Migration Review", loaded_assessment_name)
        check(
            "saved assessment notes restored",
            loaded_assessment_notes == "Sizing reviewed with customer architecture team.",
            loaded_assessment_notes,
        )
        check(
            "saved assessment state restored",
            loaded_state.get("step4_ocvs_commitment_term") == "3_year"
            and loaded_state.get("step4_iaas_discount_pct") == 12.5
            and loaded_state.get("step4_hybrid_placements", {}).get("manual-vm-001") == "native"
            and len(loaded_state.get("selected_vm_names", [])) == 3,
            str(loaded_state),
        )
        check(
            "saved assessment review decisions restored",
            loaded_state.get("acknowledged_warning_ids") == ["unsupported-native"]
            and loaded_state.get("assessor_recommendation") == "native"
            and loaded_state.get("assessor_recommendation_rationale")
            == "Remediate legacy guests before migration.",
            str(loaded_state),
        )

        response = client.post(
            "/",
            data={"action": "delete_assessment", "assessment_id": saved_assessment_id},
            follow_redirects=True,
        )
        check("saved assessment deletes snapshot", response.status_code == 200 and b"Assessment deleted." in response.data)
        check(
            "saved assessment removed from list",
            all(assessment.get("id") != saved_assessment_id for assessment in app_module.list_saved_assessments()),
            str(app_module.list_saved_assessments()),
        )
        app_module.save_preferences({})


def run_workflow_and_export() -> tuple[Path, dict[str, object]]:
    inventory = CSV_INVENTORY
    price_file = find_price_file()

    with app_module.app.test_client() as client:
        response = client.get("/")
        check(
            "home route renders",
            response.status_code == 200
            and b"Assessment Identity" in response.data
            and b"OCI Pricing" in response.data
            and b"Inventory Source" in response.data,
        )
        check(
            "no default price list before selection",
            b"Active Price List" not in response.data and b"Select a saved price list" in response.data,
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
            and b'aria-label="Selected inventory summary"' in response.data
            and b"Selected VM Inventory" in response.data,
        )

        rows, _ = app_module.load_vms_from_vinfo(str(inventory))
        vm_names = [row["name"] for row in rows]
        workflow_placements = {
            "vm-app-01": "native",
            "vm-db-01": "native",
            "vm-web-01": "native",
            "vm-legacy-01": "ocvs",
        }
        response = client.post(
            "/step3",
            data=MultiDict(
                [
                    ("action", "save_inventory_review"),
                    ("continue_to_scenarios", "1"),
                    ("acknowledged_warning_ids", "unsupported-native"),
                    *[("included_vm_names", name) for name in vm_names],
                    *[
                        (f"placement:{name}", workflow_placements[name])
                        for name in vm_names
                    ],
                ]
            ),
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
        workflow_placements[first_vm] = "ocvs"
        workflow_placements[second_vm] = "native"
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
                *[
                    (f"hybrid_placement:{name}", workflow_placements[name])
                    for name in vm_names
                ],
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
                **{
                    f"hybrid_placement:{name}": workflow_placements[name]
                    for name in vm_names
                },
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
                    *[
                        (f"hybrid_placement:{name}", workflow_placements[name])
                        for name in vm_names
                    ],
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
            data={
                "action": "export_excel",
                "active_scenario": "price",
                **{
                    f"hybrid_placement:{name}": workflow_placements[name]
                    for name in vm_names
                },
            },
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
    validate_workspace_context_contracts()
    validate_workspace_shell_behavior()
    validate_workspace_source_contracts()
    validate_unsupported_currency_workspace_shell()
    validate_pricing_fallback_filename_concealment()
    validate_catalog_choice_tokens()
    validate_atomic_app_state_write()
    validate_transactional_inventory_activation()
    validate_owned_candidate_cleanup_protection()
    validate_stage1_safe_exception_messages()
    validate_saved_assessment_load_save_state_failure()
    validate_saved_assessment_load_step4_failure()
    validate_atomic_step4_snapshot_write()
    validate_shared_workspace_shell()
    validate_stage1_setup_redesign()
    validate_stage1_identity_save_and_loaded_manual_mode()
    validate_manual_sizing_input()
    validate_app_state_review_inputs()
    validate_saved_assessments()
    validate_step3_duplicate_removal()
    validate_guided_inventory_review()
    validate_inventory_review_transactions_and_step4_boundary()
    validate_large_inventory_review_containment()
    workbook_path, workflow_state = run_workflow_and_export()
    validate_pricing_invariants(workflow_state)
    validate_workbook(workbook_path)
    validate_price_list_dropdown_policy()
    print(f"REGRESSION_OK workbook={workbook_path}")


if __name__ == "__main__":
    main()
