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
NATIVE_SCENARIO_INVENTORY = app_module.RVTOOLS_DIR / "native_scenario_75.csv"
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
        self.assessment_save: dict[str, object] | None = None
        self.assessment_open: dict[str, object] | None = None
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
        if "data-assessment-save" in attributes:
            self.assessment_save = {"tag": tag, "attrs": attributes}
        if "data-assessment-open" in attributes:
            self.assessment_open = {"tag": tag, "attrs": attributes}

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


class AccessibilityMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.elements_by_id: dict[str, dict[str, str | None]] = {}
        self.label_targets: set[str] = set()
        self.controls: list[dict[str, object]] = []
        self.tabs: list[dict[str, str | None]] = []
        self.tabpanels: list[dict[str, str | None]] = []
        self.sort_headers: list[dict[str, str | None]] = []
        self._stack: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = str(attributes.get("id") or "")
        if element_id:
            self.ids.append(element_id)
            self.elements_by_id.setdefault(element_id, {"tag": tag, **attributes})

        if tag == "label" and attributes.get("for"):
            self.label_targets.add(str(attributes["for"]))

        if tag in {"input", "select", "textarea"}:
            input_type = str(attributes.get("type") or "").lower()
            is_hidden = input_type == "hidden" or "hidden" in attributes
            if not is_hidden:
                self.controls.append(
                    {
                        "tag": tag,
                        "attrs": attributes,
                        "wrapped": any(parent_tag == "label" for parent_tag, _ in self._stack),
                    }
                )

        if attributes.get("role") == "tab":
            attributes["in_tablist"] = (
                "true"
                if any(parent_attrs.get("role") == "tablist" for _, parent_attrs in self._stack)
                else "false"
            )
            self.tabs.append(attributes)
        elif attributes.get("role") == "tabpanel":
            self.tabpanels.append(attributes)

        if "data-sort" in attributes:
            header = next(
                (parent_attrs for parent_tag, parent_attrs in reversed(self._stack) if parent_tag == "th"),
                {},
            )
            self.sort_headers.append(header)

        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._stack.append((tag, attributes))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def unnamed_controls(self) -> list[str]:
        unnamed: list[str] = []
        for control in self.controls:
            attrs = control["attrs"]
            control_id = str(attrs.get("id") or "")
            labelledby = str(attrs.get("aria-labelledby") or "").split()
            has_name = bool(
                control["wrapped"]
                or attrs.get("aria-label")
                or labelledby
                or (control_id and control_id in self.label_targets)
            )
            if not has_name:
                unnamed.append(
                    f"{control['tag']}#{control_id or '-'}[name={attrs.get('name') or '-'}]"
                )
        return unnamed


def parse_accessibility_markup(response_data: bytes) -> AccessibilityMarkupParser:
    parser = AccessibilityMarkupParser()
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
            number = cell.find("m:v", XLSX_NS)
            if text is not None and text.text is not None:
                values.append(text.text)
            elif number is not None and number.text is not None:
                values.append(number.text)
            else:
                values.append("")
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
    native_scenario_rows = [
        ["VM", "Powerstate", "Template", "OS according to the configuration file", "CPUs", "Memory", "Provisioned MiB"]
    ]
    native_scenario_rows.extend(
        [
            f"native-page-vm-{index + 1:03d}",
            "poweredOn",
            "False",
            (
                "Oracle Linux 8 (64-bit)"
                if index < 70
                else "Microsoft Windows Server 2008 (64-bit)"
                if index < 73
                else "Unknown"
            ),
            "4",
            "8192",
            "102400",
        ]
        for index in range(76)
    )
    NATIVE_SCENARIO_INVENTORY.write_text(
        "\n".join(",".join(value for value in row) for row in native_scenario_rows) + "\n",
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


def validate_current_readiness_routes() -> None:
    adapter = getattr(app_module, "build_current_readiness_context", None)
    check("current readiness adapter exists", callable(adapter))

    with app_module.app.test_client() as redirect_client:
        with redirect_client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess[app_module.STEP4_UNSAVED_READINESS_SESSION_KEY] = True
        redirect_response = redirect_client.get("/step4?tab=native")
        with redirect_client.session_transaction() as sess:
            marker_preserved = (
                sess.get(app_module.STEP4_UNSAVED_READINESS_SESSION_KEY) is True
            )
        check(
            "Step 4 prerequisite redirect preserves pending unsaved readiness",
            redirect_response.status_code == 302 and marker_preserved,
            f"status={redirect_response.status_code}, preserved={marker_preserved}",
        )

    price_file = find_price_file()
    inventory_rows, _ = app_module.load_vms_from_vinfo(str(CSV_INVENTORY))
    selected_names = [str(row["name"]) for row in inventory_rows]
    state_id = f"current_readiness_{uuid4().hex}"
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        state = app_module.load_app_state()
        state["selected_vm_names"] = selected_names
        state["step4_hybrid_placements"] = {
            "vm-app-01": "native",
            "vm-db-01": "native",
            "vm-web-01": "native",
            "vm-legacy-01": "ocvs",
        }
        state["acknowledged_warning_ids"] = ["unsupported-native"]
        state["step4_vmware_license_price_per_core_yearly"] = 0.0
        app_module.save_app_state(state)

    original_builder = app_module.build_assessment_readiness
    readiness_contexts: list[dict[str, object]] = []
    readiness_results: list[dict[str, object]] = []

    def tracked_builder(context: dict[str, object]) -> dict[str, object]:
        result = original_builder(context)
        readiness_contexts.append(context)
        readiness_results.append(result)
        return result

    pricing_sheet_names = {
        "Price Comparison",
        "OCI Native Analysis",
        "OCVS Analysis",
        "Hybrid Analysis",
        "Hybrid Placement",
        "Selected VMs",
        "Non-Selected VMs",
        "Price List",
    }

    def workbook_signatures(
        zf: zipfile.ZipFile,
    ) -> tuple[
        dict[str, list[tuple[str, str]]],
        dict[str, list[tuple[str, str]]],
        tuple[tuple[str, ...], tuple[tuple[str, str], ...]],
    ]:
        sheet_map = workbook_sheet_map(zf)
        numeric_cells: dict[str, list[tuple[str, str]]] = {}
        formulas: dict[str, list[tuple[str, str]]] = {}
        for sheet_name, sheet_path in sheet_map.items():
            root = ET.fromstring(zf.read(sheet_path))
            formulas[sheet_name] = [
                (cell.attrib.get("r", ""), formula.text or "")
                for cell in root.findall(".//m:c", XLSX_NS)
                if (formula := cell.find("m:f", XLSX_NS)) is not None
            ]
            if sheet_name in pricing_sheet_names:
                numeric_cells[sheet_name] = [
                    (cell.attrib.get("r", ""), value.text or "")
                    for cell in root.findall(".//m:c", XLSX_NS)
                    if (value := cell.find("m:v", XLSX_NS)) is not None
                ]
        workbook_root = ET.fromstring(zf.read("xl/workbook.xml"))
        calc_properties = workbook_root.find("m:calcPr", XLSX_NS)
        calc_signature = (
            tuple(sorted(name for name in zf.namelist() if "calc" in name.lower())),
            tuple(sorted(calc_properties.attrib.items())) if calc_properties is not None else (),
        )
        return numeric_cells, formulas, calc_signature

    def section_rows(
        rows: list[list[str]],
        start_title: str,
        end_title: str,
    ) -> list[list[str]]:
        start_index = next(
            (
                index
                for index, row in enumerate(rows)
                if row and row[0] == start_title
            ),
            -1,
        )
        end_index = next(
            (
                index
                for index, row in enumerate(rows)
                if index > start_index and row and row[0] == end_title
            ),
            len(rows),
        )
        return rows[start_index + 1 : end_index] if start_index >= 0 else []

    app_module.build_assessment_readiness = tracked_builder
    try:
        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
                sess["state_id"] = state_id
                sess["selected_rvtools_file"] = str(CSV_INVENTORY)
                sess["selected_pricelist_file"] = price_file
                sess["selected_currency"] = "EUR"
                sess["customer_name"] = "Readiness Customer"
                sess["active_assessment_name"] = "Readiness assessment"

            for route in ("/", "/step3"):
                prior_calls = len(readiness_results)
                response = client.get(route)
                check(
                    f"{route} builds readiness once",
                    response.status_code == 200
                    and len(readiness_results) == prior_calls + 1,
                    f"status={response.status_code}, calls={len(readiness_results) - prior_calls}",
                )
                readiness = readiness_results[-1]
                check(
                    f"{route} scenarios stay explicitly incomplete",
                    all(
                        scenario.get("pricing_state") == "incomplete"
                        and scenario.get("rankable") is False
                        and scenario.get("monthly_cost") is None
                        for scenario in readiness.get("scenarios", {}).values()
                    ),
                    str(readiness.get("scenarios")),
                )

            prior_calls = len(readiness_results)
            response = client.get("/step4?tab=native")
            check(
                "/step4 builds readiness once",
                response.status_code == 200
                and len(readiness_results) == prior_calls + 1,
                f"status={response.status_code}, calls={len(readiness_results) - prior_calls}",
            )
            readiness = readiness_results[-1]
            native = readiness.get("scenarios", {}).get("native", {})
            check(
                "acknowledged Native remediation stays visible and rankable",
                native.get("technical_eligibility") == "eligible"
                and native.get("state") == "needs_attention"
                and native.get("rankable") is True
                and native.get("affected_vm_names") == ["vm-legacy-01"]
                and any(
                    item.get("id") == "unsupported-native"
                    and bool(str(item.get("title", "")).strip())
                    and bool(str(item.get("detail", "")).strip())
                    and item.get("stage") == "inventory"
                    and item.get("affected_vm_names") == ["vm-legacy-01"]
                    and item.get("severity") == "advisory"
                    and item.get("acknowledged") is True
                    for item in readiness.get("display_advisory_items", [])
                )
                and b"Unsupported for OCI Native" in response.data
                and b"These VMs remain in scope but require remediation review" in response.data,
                str(readiness),
            )
            check(
                "zero VCF price does not replace host-pricing readiness",
                all(
                    readiness.get("scenarios", {}).get(scenario_id, {}).get("pricing_state")
                    == "incomplete"
                    and readiness.get("scenarios", {}).get(scenario_id, {}).get("rankable")
                    is False
                    for scenario_id in ("ocvs", "hybrid")
                )
                and any(
                    item.get("title") == "OCVS host pricing incomplete"
                    for item in readiness.get("advisory_items", [])
                )
                and not any(
                    item.get("title") == "VCF license price not set"
                    for item in readiness.get("advisory_items", [])
                ),
                str(readiness.get("scenarios")),
            )
            check(
                "fit warnings reach readiness payload and shell",
                not any(
                    item.get("title") == "VCF license price not set"
                    for item in readiness.get("advisory_items", [])
                )
                and b"OCVS and Hybrid costs exclude VCF license cost" not in response.data
                and b"VCF license coverage" in response.data,
                str(readiness.get("advisory_items")),
            )

            prior_calls = len(readiness_results)
            response = client.post(
                "/step4",
                data={
                    "action": "save",
                    "active_scenario": "native",
                    "hybrid_placement:vm-app-01": "native",
                },
                follow_redirects=True,
            )
            check(
                "invalid Step 4 POST marks redirected readiness unsaved once",
                response.status_code == 200
                and len(readiness_results) == prior_calls + 1
                and readiness_contexts[-1].get("has_unsaved_scenario_changes") is True,
                f"status={response.status_code}, calls={len(readiness_results) - prior_calls}, context={readiness_contexts[-1]}",
            )

            valid_save_data = {
                "action": "save",
                "active_scenario": "native",
                **{
                    app_module.inventory_placement_field_name(
                        "hybrid_placement", vm_name
                    ): placement
                    for vm_name, placement in {
                        "vm-app-01": "native",
                        "vm-db-01": "native",
                        "vm-web-01": "native",
                        "vm-legacy-01": "ocvs",
                    }.items()
                },
            }
            with client.session_transaction() as sess:
                sess[app_module.STEP4_UNSAVED_READINESS_SESSION_KEY] = True
            export_data = dict(valid_save_data)
            export_data["action"] = "export_excel"
            export_data["active_scenario"] = "price"
            prior_calls = len(readiness_results)
            response = client.post("/step4", data=export_data)
            with client.session_transaction() as sess:
                export_marker_cleared = (
                    app_module.STEP4_UNSAVED_READINESS_SESSION_KEY not in sess
                )
                exported_readiness_workbook = str(
                    sess.get("last_export_file", "")
                ).strip()
            check(
                "persisted Step 4 Excel export clears pending unsaved readiness",
                response.status_code == 200
                and response.mimetype
                == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                and len(readiness_results) == prior_calls + 1
                and export_marker_cleared,
                f"status={response.status_code}, calls={len(readiness_results) - prior_calls}, cleared={export_marker_cleared}",
            )
            response.close()
            draft_numeric_cells: dict[str, list[tuple[str, str]]] = {}
            draft_formulas: dict[str, list[tuple[str, str]]] = {}
            draft_calc_signature: tuple[tuple[str, ...], tuple[tuple[str, str], ...]] = ((), ())
            if exported_readiness_workbook:
                with zipfile.ZipFile(exported_readiness_workbook) as zf:
                    sheet_map = workbook_sheet_map(zf)
                    (
                        draft_numeric_cells,
                        draft_formulas,
                        draft_calc_signature,
                    ) = workbook_signatures(zf)
                    executive_rows = sheet_text_rows(
                        zf, sheet_map["Executive Summary"]
                    )
                    price_rows = sheet_text_rows(
                        zf, sheet_map["Price Comparison"]
                    )
                    decision_rows = section_rows(
                        executive_rows,
                        "Decision Readout",
                        "Migration Path Options",
                    )
                    executive_text = " ".join(
                        value for row in executive_rows for value in row
                    )
                    raw_workbook_xml = "\n".join(
                        zf.read(name).decode("utf-8", errors="ignore")
                        for name in zf.namelist()
                        if name.endswith(".xml")
                    )
                    check(
                        "draft workbook includes readiness status and scenario completeness",
                        all(
                            token in executive_text
                            for token in (
                                "Executive Summary - Draft",
                                "Workbook Status",
                                "Draft",
                                "Assessment Readiness",
                                "Draft results available",
                                "Native Remediation Status",
                                "Required",
                                "Native Affected VM Count",
                                "OCVS Pricing Completeness",
                                "Hybrid Pricing Completeness",
                                "Complete",
                            )
                        )
                        and any(
                            row[:2] == ["Native Affected VM Count", "1"]
                            for row in executive_rows
                        ),
                        executive_text,
                    )
                    check(
                        "draft workbook includes recommendation fields and unresolved issue detail",
                        all(
                            token in executive_text
                            for token in (
                                "Specialist Recommendation",
                                "Internal Notes",
                                "Unresolved Blockers",
                                "Unresolved Advisories",
                                "Affected VMs",
                                "vm-legacy-01",
                            )
                        ),
                        executive_text,
                    )
                    check(
                        "draft workbook decision uses safe assessor choice and readiness price signal",
                        any(
                            row[:2] == ["Specialist Decision", "Undecided"]
                            for row in decision_rows
                        )
                        and any(
                            row[:2]
                            == ["Lowest complete modeled price", "OCI Native"]
                            for row in decision_rows
                        )
                        and all(
                            row[0]
                            not in {"Recommended Migration Path", "Recommended Path"}
                            for row in decision_rows + price_rows
                            if row
                        )
                        and "Recommended Migration Path" not in raw_workbook_xml,
                        str(decision_rows),
                    )
                Path(exported_readiness_workbook).unlink(missing_ok=True)

            with app_module.app.test_request_context("/"):
                app_module.session["state_id"] = state_id
                customer_ready_state = app_module.load_app_state()
                customer_ready_state["assessor_recommendation"] = "native"
                customer_ready_state["assessor_recommendation_rationale"] = (
                    "Remediate the affected legacy guest before Native migration."
                )
                app_module.save_app_state(customer_ready_state)

            prior_calls = len(readiness_results)
            response = client.post("/step4", data=export_data)
            with client.session_transaction() as sess:
                customer_ready_workbook = str(
                    sess.get("last_export_file", "")
                ).strip()
            check(
                "customer-ready Excel export uses one central readiness result",
                response.status_code == 200
                and len(readiness_results) == prior_calls + 1
                and readiness_results[-1].get("customer_ready_export") is True,
                f"status={response.status_code}, calls={len(readiness_results) - prior_calls}, readiness={readiness_results[-1]}",
            )
            response.close()
            baseline_numeric_cells: dict[str, list[tuple[str, str]]] = {}
            baseline_formulas: dict[str, list[tuple[str, str]]] = {}
            baseline_calc_signature: tuple[tuple[str, ...], tuple[tuple[str, str], ...]] = ((), ())
            if customer_ready_workbook:
                with zipfile.ZipFile(customer_ready_workbook) as zf:
                    sheet_map = workbook_sheet_map(zf)
                    (
                        baseline_numeric_cells,
                        baseline_formulas,
                        baseline_calc_signature,
                    ) = workbook_signatures(zf)
                    customer_ready_rows = sheet_text_rows(
                        zf, sheet_map["Executive Summary"]
                    )
                    customer_price_rows = sheet_text_rows(
                        zf, sheet_map["Price Comparison"]
                    )
                    customer_decision_rows = section_rows(
                        customer_ready_rows,
                        "Decision Readout",
                        "Migration Path Options",
                    )
                    executive_text = " ".join(
                        value for row in customer_ready_rows for value in row
                    )
                    raw_workbook_xml = "\n".join(
                        zf.read(name).decode("utf-8", errors="ignore")
                        for name in zf.namelist()
                        if name.endswith(".xml")
                    )
                    check(
                        "customer-ready workbook status follows central readiness",
                        all(
                            token in executive_text
                            for token in (
                                "Executive Summary - Customer ready",
                                "Workbook Status",
                                "Assessment Readiness",
                                "Customer ready",
                                "Specialist Recommendation",
                                "OCI Native",
                                "Remediate the affected legacy guest before Native migration.",
                                "OCVS Pricing Completeness",
                                "Hybrid Pricing Completeness",
                                "Incomplete",
                            )
                        )
                        and any(
                            row[:2] == ["Native Affected VM Count", "1"]
                            for row in customer_ready_rows
                        ),
                        executive_text,
                    )
                    check(
                        "customer-ready workbook decision uses assessor Native instead of heuristic Hybrid",
                        any(
                            row[:2] == ["Specialist Decision", "OCI Native"]
                            for row in customer_decision_rows
                        )
                        and not any(
                            "Hybrid" in row for row in customer_decision_rows
                        )
                        and any(
                            row[:2]
                            == ["Lowest complete modeled price", "OCI Native"]
                            for row in customer_decision_rows
                        )
                        and any(
                            row[:2] == ["Specialist Decision", "OCI Native"]
                            for row in customer_price_rows
                        )
                        and all(
                            row[0]
                            not in {"Recommended Migration Path", "Recommended Path"}
                            for row in customer_decision_rows + customer_price_rows
                            if row
                        )
                        and "Recommended Migration Path" not in raw_workbook_xml,
                        str(customer_decision_rows),
                    )
                    check(
                        "specialist decision metadata does not change workbook calculations",
                        baseline_numeric_cells == draft_numeric_cells
                        and baseline_formulas == draft_formulas
                        and baseline_calc_signature == draft_calc_signature,
                        f"pricing={baseline_numeric_cells == draft_numeric_cells}, formulas={baseline_formulas == draft_formulas}, calc={baseline_calc_signature == draft_calc_signature}",
                    )
                Path(customer_ready_workbook).unlink(missing_ok=True)

            oversized_text = "x" * 40000

            def malformed_export_builder(context: dict[str, object]) -> dict[str, object]:
                result = json.loads(json.dumps(original_builder(context)))
                result["overall_state"] = "customer_ready"
                result["customer_ready_export"] = True
                result["scenarios"]["native"]["remediation_required"] = False
                result["blocking_items"] = [
                    {
                        "id": "duplicate-id",
                        "title": "First issue",
                        "detail": "First detail.",
                        "affected_vm_names": ["vm-legacy-01"],
                    }
                ]
                result["advisory_items"] = [
                    {
                        "id": "duplicate-id",
                        "title": "Duplicate issue",
                        "detail": "Duplicate detail.",
                        "affected_vm_names": ["vm-legacy-01"],
                    },
                    {
                        "id": oversized_text,
                        "title": oversized_text,
                        "detail": oversized_text,
                        "affected_vm_names": [oversized_text],
                    },
                    *[
                        {
                            "id": f"bounded-issue-{index}",
                            "title": f"Bounded issue {index}",
                            "detail": "Review this item.",
                            "affected_vm_names": [],
                        }
                        for index in range(1000)
                    ],
                ]
                return result

            app_module.build_assessment_readiness = malformed_export_builder
            try:
                malformed_response = client.post("/step4", data=export_data)
                with client.session_transaction() as sess:
                    malformed_workbook = str(sess.get("last_export_file", "")).strip()
                malformed_export_status = malformed_response.status_code
                malformed_response.close()
            finally:
                app_module.build_assessment_readiness = tracked_builder

            malformed_artifact_ok = False
            malformed_artifact_detail = "workbook not created"
            if malformed_workbook and Path(malformed_workbook).is_file():
                with zipfile.ZipFile(malformed_workbook) as zf:
                    sheet_map = workbook_sheet_map(zf)
                    executive_path = sheet_map["Executive Summary"]
                    executive_root = ET.fromstring(zf.read(executive_path))
                    executive_rows = sheet_text_rows(zf, executive_path)
                    executive_values = [
                        value for row in executive_rows for value in row
                    ]
                    executive_text_lengths = [
                        len(node.text or "")
                        for node in executive_root.findall(".//m:t", XLSX_NS)
                    ]
                    numeric_cells, formulas, calc_signature = workbook_signatures(zf)
                    malformed_artifact_ok = all(
                        (
                            malformed_export_status == 200,
                            "Executive Summary - Draft" in executive_values,
                            "Customer ready" not in executive_values,
                            any(
                                row[:2] == ["Assessment Readiness", "Incomplete"]
                                for row in executive_rows
                            ),
                            any(
                                row[:2] == ["Native Remediation Status", "Required"]
                                for row in executive_rows
                            ),
                            executive_values.count("First issue") == 1,
                            "Duplicate issue" not in executive_values,
                            bool(executive_text_lengths),
                            max(executive_text_lengths, default=0) <= 4000,
                            len(executive_root.findall(".//m:row", XLSX_NS)) <= 1200,
                            numeric_cells == baseline_numeric_cells,
                            formulas == baseline_formulas,
                            calc_signature == baseline_calc_signature,
                        )
                    )
                    malformed_artifact_detail = (
                        f"status={malformed_export_status}, rows={len(executive_root.findall('.//m:row', XLSX_NS))}, "
                        f"max_text={max(executive_text_lengths, default=0)}, first={executive_values.count('First issue')}, "
                        f"duplicate={executive_values.count('Duplicate issue')}, pricing_match={numeric_cells == baseline_numeric_cells}, "
                        f"formula_match={formulas == baseline_formulas}, calc_match={calc_signature == baseline_calc_signature}"
                    )
                Path(malformed_workbook).unlink(missing_ok=True)
            check(
                "malformed readiness renders bounded draft XML without pricing or calculation drift",
                malformed_artifact_ok,
                malformed_artifact_detail,
            )

            malformed_metadata = app_module._workbook_readiness_metadata(
                {
                    "overall_state": "customer_ready",
                    "customer_ready_export": "true",
                    "scenarios": [],
                    "blocking_items": "none",
                    "advisory_items": {"title": "not a list"},
                },
                assessor_recommendation={"value": "native"},
                recommendation_rationale=["not text"],
            )
            check(
                "malformed workbook readiness metadata fails closed",
                malformed_metadata.get("workbook_status") == "Draft"
                and malformed_metadata.get("readiness_label") == "Incomplete"
                and malformed_metadata.get("customer_ready_export") is False
                and malformed_metadata.get("recommendation")
                == "Undecided",
                str(malformed_metadata),
            )

            with client.session_transaction() as sess:
                sess[app_module.STEP4_UNSAVED_READINESS_SESSION_KEY] = True
            prior_calls = len(readiness_results)
            response = client.post(
                "/step4",
                data=valid_save_data,
                follow_redirects=True,
            )
            check(
                "successful Step 4 save resets redirected readiness unsaved",
                response.status_code == 200
                and len(readiness_results) == prior_calls + 1
                and readiness_contexts[-1].get("has_unsaved_scenario_changes") is False,
                f"status={response.status_code}, calls={len(readiness_results) - prior_calls}, context={readiness_contexts[-1]}",
            )

            prior_calls = len(readiness_results)
            response = client.get("/step4?tab=native")
            check(
                "unsaved readiness does not persist after successful save",
                response.status_code == 200
                and len(readiness_results) == prior_calls + 1
                and readiness_contexts[-1].get("has_unsaved_scenario_changes") is False,
                f"status={response.status_code}, calls={len(readiness_results) - prior_calls}, context={readiness_contexts[-1]}",
            )
    finally:
        app_module.build_assessment_readiness = original_builder


def validate_workbook_readiness_metadata_safety() -> None:
    def customer_ready_metadata(recommendation: str = "ocvs") -> dict[str, object]:
        scenarios = {
            scenario_id: {
                "technical_eligibility": "eligible",
                "pricing_state": "complete",
                "rankable": True,
                "customer_ready": True,
                "remediation_required": False,
                "affected_vm_names": [],
            }
            for scenario_id in ("native", "ocvs", "hybrid")
        }
        return {
            "overall_state": "customer_ready",
            "customer_ready_export": True,
            "scenarios": scenarios,
            "blocking_items": [],
            "advisory_items": [],
            "lowest_complete_scenario": recommendation,
            "recommendation": recommendation,
        }

    contradiction_inputs: list[tuple[str, dict[str, object], object, object]] = []

    incomplete_ocvs = customer_ready_metadata("ocvs")
    incomplete_ocvs["scenarios"]["ocvs"]["pricing_state"] = "incomplete"
    contradiction_inputs.append(("incomplete selected OCVS", incomplete_ocvs, "ocvs", "Reviewed."))

    missing_ocvs = customer_ready_metadata("ocvs")
    del missing_ocvs["scenarios"]["ocvs"]
    contradiction_inputs.append(("missing selected OCVS", missing_ocvs, "ocvs", "Reviewed."))

    ineligible_ocvs = customer_ready_metadata("ocvs")
    ineligible_ocvs["scenarios"]["ocvs"]["technical_eligibility"] = "ineligible"
    contradiction_inputs.append(("ineligible selected OCVS", ineligible_ocvs, "ocvs", "Reviewed."))

    wrong_boolean = customer_ready_metadata("ocvs")
    wrong_boolean["scenarios"]["ocvs"]["technical_eligibility"] = True
    contradiction_inputs.append(("wrong readiness field type", wrong_boolean, "ocvs", "Reviewed."))

    wrong_rankable_boolean = customer_ready_metadata("ocvs")
    wrong_rankable_boolean["scenarios"]["ocvs"]["rankable"] = "true"
    contradiction_inputs.append(("wrong scenario boolean type", wrong_rankable_boolean, "ocvs", "Reviewed."))

    scenario_not_customer_ready = customer_ready_metadata("ocvs")
    scenario_not_customer_ready["scenarios"]["ocvs"]["customer_ready"] = False
    contradiction_inputs.append(("selected scenario not customer ready", scenario_not_customer_ready, "ocvs", "Reviewed."))

    incoherent_overall = customer_ready_metadata("ocvs")
    incoherent_overall["overall_state"] = "draft_review_required"
    contradiction_inputs.append(("incoherent overall/export state", incoherent_overall, "ocvs", "Reviewed."))

    incoherent_export = customer_ready_metadata("ocvs")
    incoherent_export["customer_ready_export"] = False
    contradiction_inputs.append(("incoherent customer-ready export", incoherent_export, "ocvs", "Reviewed."))

    contradictory_native = customer_ready_metadata("native")
    contradictory_native["scenarios"]["native"]["affected_vm_names"] = ["legacy-vm"]
    contradiction_inputs.append(("Native affected names without remediation", contradictory_native, "native", "Treat legacy-vm."))

    contradiction_results = []
    for label, readiness, recommendation, rationale in contradiction_inputs:
        normalized = app_module._workbook_readiness_metadata(
            readiness,
            assessor_recommendation=recommendation,
            recommendation_rationale=rationale,
        )
        contradiction_results.append(
            (
                label,
                normalized.get("workbook_status"),
                normalized.get("readiness_label"),
                normalized.get("customer_ready_export"),
                normalized.get("native_remediation_status"),
            )
        )

    duplicate_issues = customer_ready_metadata("ocvs")
    duplicate_issues["overall_state"] = "draft_review_required"
    duplicate_issues["customer_ready_export"] = False
    duplicate_issues["blocking_items"] = [
        {
            "id": "duplicate-id",
            "title": "First issue",
            "detail": "First detail.",
            "affected_vm_names": ["vm-a"],
        }
    ]
    duplicate_issues["advisory_items"] = [
        {
            "id": "duplicate-id",
            "title": "Duplicate issue",
            "detail": "Duplicate detail.",
            "affected_vm_names": ["vm-a", "vm-a"],
        }
    ]
    duplicate_normalized = app_module._workbook_readiness_metadata(
        duplicate_issues,
        assessor_recommendation="ocvs",
        recommendation_rationale="Reviewed.",
    )
    duplicate_rows = [
        *duplicate_normalized.get("blockers", []),
        *duplicate_normalized.get("advisories", []),
    ]

    excessive_issues = customer_ready_metadata("ocvs")
    excessive_issues["overall_state"] = "draft_review_required"
    excessive_issues["customer_ready_export"] = False
    excessive_issues["advisory_items"] = [
        {
            "id": f"issue-{index}",
            "title": f"Issue {index}",
            "detail": "Review this item.",
            "affected_vm_names": [],
        }
        for index in range(1001)
    ]
    excessive_normalized = app_module._workbook_readiness_metadata(
        excessive_issues,
        assessor_recommendation="ocvs",
        recommendation_rationale="Reviewed.",
    )
    excessive_rows = [
        *excessive_normalized.get("blockers", []),
        *excessive_normalized.get("advisories", []),
    ]

    oversized_text = "x" * 40000
    oversized_metadata = customer_ready_metadata("ocvs")
    oversized_metadata["overall_state"] = "draft_review_required"
    oversized_metadata["customer_ready_export"] = False
    oversized_metadata["advisory_items"] = [
        {
            "id": oversized_text,
            "title": oversized_text,
            "detail": oversized_text,
            "affected_vm_names": [oversized_text],
        }
    ]
    oversized_normalized = app_module._workbook_readiness_metadata(
        oversized_metadata,
        assessor_recommendation="ocvs",
        recommendation_rationale=oversized_text,
    )
    oversized_issue = oversized_normalized.get("advisories", [{}])[0]

    expanding_title_metadata = customer_ready_metadata("ocvs")
    expanding_title_metadata["overall_state"] = "draft_review_required"
    expanding_title_metadata["customer_ready_export"] = False
    expanding_title_metadata["advisory_items"] = [
        {
            "id": "\u00df" * 4000,
            "title": "",
            "detail": "Fallback title must remain bounded.",
            "affected_vm_names": [],
        }
    ]
    expanding_title_normalized = app_module._workbook_readiness_metadata(
        expanding_title_metadata,
        assessor_recommendation="ocvs",
        recommendation_rationale="Reviewed.",
    )
    expanding_title_issue = expanding_title_normalized.get("advisories", [{}])[0]

    literal_xml = app_module._xlsx_cell_xml("=" + oversized_text, 1, 0)
    literal_cell = ET.fromstring(literal_xml)
    literal_text_node = literal_cell.find(".//t")
    trusted_formula_xml = app_module._xlsx_cell_xml(
        app_module._xlsx_formula("SUM(A1:A2)"), 1, 0
    )

    quality_failures = [
        label
        for label, status, readiness_label, export_allowed, native_status in contradiction_results
        if status != "Draft"
        or readiness_label != "Incomplete"
        or export_allowed is not False
        or (label == "Native affected names without remediation" and native_status != "Required")
    ]
    if not (
        duplicate_normalized.get("readiness_label") == "Incomplete"
        and len(duplicate_rows) == 1
        and duplicate_rows[0].get("id") == "duplicate-id"
        and duplicate_rows[0].get("title") == "First issue"
    ):
        quality_failures.append(f"duplicate issue handling: {duplicate_normalized}")
    if not (
        excessive_normalized.get("readiness_label") == "Incomplete"
        and excessive_normalized.get("customer_ready_export") is False
        and len(excessive_rows) == 1000
    ):
        quality_failures.append(
            f"issue count bound: status={excessive_normalized.get('readiness_label')}, rows={len(excessive_rows)}"
        )
    if not (
        oversized_normalized.get("readiness_label") == "Incomplete"
        and oversized_normalized.get("customer_ready_export") is False
        and len(str(oversized_normalized.get("recommendation_rationale", ""))) <= 4000
        and bool(oversized_issue.get("id"))
        and all(
            len(str(oversized_issue.get(field, ""))) <= 4000
            for field in ("id", "title", "detail", "affected_vms")
        )
    ):
        quality_failures.append(
            f"metadata text bounds: rationale={len(str(oversized_normalized.get('recommendation_rationale', '')))}, issue={oversized_issue}"
        )
    if not (
        expanding_title_normalized.get("readiness_label") == "Incomplete"
        and len(str(expanding_title_issue.get("title", ""))) <= 4000
    ):
        quality_failures.append(
            f"generated title bound: status={expanding_title_normalized.get('readiness_label')}, "
            f"title={len(str(expanding_title_issue.get('title', '')))}"
        )
    if not (
        literal_text_node is not None
        and len(literal_text_node.text or "") == 32767
        and "<f>" not in literal_xml
        and "<f>SUM(A1:A2)</f>" in trusted_formula_xml
    ):
        quality_failures.append(
            f"XLSX text/formula boundary: literal={len(literal_text_node.text or '') if literal_text_node is not None else 'missing'}, formula={trusted_formula_xml}"
        )

    check(
        "Task 11 workbook readiness rejects contradictions and bounds rendered metadata",
        not quality_failures,
        "; ".join(str(item) for item in quality_failures),
    )


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
        and empty_shell.assessment_trigger is None
        and empty_shell.assessment_panel is None,
        f"stages={empty_stage_signature}, options={empty_shell.mobile_options}, footer={empty_shell.footer_controls}",
    )
    check(
        "workspace header omits noisy assessment dropdown",
        b"data-assessment-menu" not in empty_response.data
        and b"data-assessment-menu-trigger" not in empty_response.data
        and b"assessment-menu-panel" not in empty_response.data
        and "menu" not in empty_shell.roles
        and "menuitem" not in empty_shell.roles
        and empty_shell.assessment_import is None
        and empty_shell.assessment_export is None,
        f"trigger={empty_shell.assessment_trigger}, panel={empty_shell.assessment_panel}, roles={empty_shell.roles}",
    )
    check(
        "global header no longer exposes portable JSON actions",
        b"data-assessment-save" not in empty_response.data
        and b"data-assessment-open" not in empty_response.data
        and b">Export assessment JSON</button>" not in empty_response.data
        and b">Import assessment JSON</button>" not in empty_response.data
        and b">Save</button>" not in empty_response.data
        and b">Open</button>" not in empty_response.data
        and b"Export current assessment" not in empty_response.data
        and b'href="/#saved-assessments">Save' not in empty_response.data
        and b'href="/#saved-assessments">Open' not in empty_response.data,
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
        and configured_shells[0].assessment_export is None
        and configured_shells[0].assessment_import is None,
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
    inventory_html = configured_responses[1].data.decode("utf-8", errors="replace")
    scenario_html = configured_responses[2].data.decode("utf-8", errors="replace")
    check(
        "form-driven stages retain one shared footer save control",
        'id="continue_step4_form"' in inventory_html
        and inventory_html.count('form="continue_step4_form"') == 1
        and inventory_html.count('inventory-button inventory-button--primary') == 0
        and "Save &amp; Continue" in inventory_html
        and 'id="step4-form"' in scenario_html
        and re.search(
            r'(?s)<footer[^>]*class="workspace-stage-actions"[^>]*>.*?'
            r'<button[^>]*form="step4-form"[^>]*name="continue_to_results"[^>]*'
            r'value="1"[^>]*>.*?Save &amp; Continue.*?</button>',
            scenario_html,
        )
        is not None,
    )


def validate_workspace_source_contracts() -> None:
    workspace_css = (ROOT / "static" / "css" / "workspace.css").read_text(encoding="utf-8")
    scenario_css = (ROOT / "static" / "css" / "scenarios.css").read_text(encoding="utf-8")
    results_css = (ROOT / "static" / "css" / "results.css").read_text(encoding="utf-8")
    workspace_js = (ROOT / "static" / "js" / "workspace.js").read_text(encoding="utf-8")
    redwood_theme = (ROOT / "templates" / "_redwood_theme.html").read_text(encoding="utf-8")
    base_template = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    readiness_template = (ROOT / "templates" / "_readiness_panel.html").read_text(encoding="utf-8")
    results_template = (ROOT / "templates" / "_results_comparison.html").read_text(encoding="utf-8")
    workload_profile_template = (ROOT / "templates" / "_workload_profile.html").read_text(encoding="utf-8")

    secondary_button_exclusions = [
        r"button:not\(\.remove\):not\(\.move-btn\):not\(\.btn-secondary\):not\(\.scenario-tab\)\s*\{",
        r"button:not\(\.remove\):not\(\.move-btn\):not\(\.btn-secondary\):not\(\.scenario-tab\):hover\s*\{",
    ]
    check(
        "Task 12 Redwood primary-button rules preserve secondary controls",
        all(re.search(pattern, redwood_theme) for pattern in secondary_button_exclusions),
    )

    check(
        "Task 12 scenario and Results focus rings use the opaque workspace focus color",
        all(
            "outline: 3px solid var(--workspace-focus);" in stylesheet
            and "rgb(47 107 69 / 28%)" not in stylesheet
            for stylesheet in (scenario_css, results_css)
        ),
    )

    check(
        "advisory-only readiness review uses an informational tone",
        "readiness-panel--info" in readiness_template
        and "readiness-panel--attention" in readiness_template
        and "Readiness notes" in readiness_template
        and ".readiness-panel--info" in workspace_css
        and "#f3faf8" in workspace_css,
    )
    check(
        "Results readiness notes use compact disclosure treatment",
        "readiness_panel_variant" in base_template
        and "workspace_stage == 'results'" in base_template
        and "readiness-panel--compact" in readiness_template
        and "<details" in readiness_template
        and "<summary" in readiness_template
        and ".readiness-panel--compact" in workspace_css,
    )

    check(
        "scenario tabs restore Native OCVS Hybrid color identity",
        all(
            token in scenario_css
            for token in (
                "--scenario-native",
                "--scenario-ocvs",
                "--scenario-hybrid",
                '[data-scenario-tab="native"]',
                '[data-scenario-tab="ocvs"]',
                '[data-scenario-tab="hybrid"]',
                ".scenario-tab::before",
            )
        ),
    )

    check(
        "Results cards expose modeled rank medals",
        "result-rank-medal" in results_template
        and "scenario.price_rank" in results_template
        and "result-rank-medal--gold" in results_css
        and "result-rank-medal--silver" in results_css
        and "result-rank-medal--bronze" in results_css
        and re.search(r"\.result-scenario__path\s*\{[^}]*white-space:\s*nowrap;", results_css, re.S)
        is not None
        and re.search(r"\.result-rank-medal\s*\{[^}]*white-space:\s*nowrap;", results_css, re.S)
        is not None,
    )
    check(
        "Results migration path cards use aligned colored headers",
        all(
            token in results_css
            for token in (
                "--result-path-color",
                "--result-path-soft",
                "--result-path-line",
                ".result-scenario--native",
                ".result-scenario--ocvs",
                ".result-scenario--hybrid",
            )
        )
        and re.search(
            r"\.result-scenario__heading\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s+auto;",
            results_css,
            re.S,
        )
        is not None
        and re.search(
            r"\.result-rank-medal\s*\{[^}]*min-width:\s*112px;",
            results_css,
            re.S,
        )
        is not None,
    )
    check(
        "Results workload profile uses existing summary data and CSS bars",
        "workload_summary.top_os_rows" in workload_profile_template
        and "workload-profile__bar-fill" in workload_profile_template
        and ".workload-profile" in results_css
        and ".workload-profile__bar-fill" in results_css,
    )

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
    check(
        "assessment JSON save and import share native picker defaults",
        "showSaveFilePicker" in workspace_js
        and "createWritable" in workspace_js
        and "data-assessment-save-form" in workspace_js
        and "data-assessment-open" in workspace_js
        and "showOpenFilePicker" in workspace_js
        and "ASSESSMENT_FILE_PICKER_ID" in workspace_js
        and "ASSESSMENT_FILE_START_DIRECTORY" in workspace_js
        and workspace_js.count("id: ASSESSMENT_FILE_PICKER_ID") >= 2
        and workspace_js.count("startIn: ASSESSMENT_FILE_START_DIRECTORY") >= 2,
    )


def validate_task12_accessibility_and_responsive_contracts() -> None:
    price_file = find_price_file()
    inventory_rows, _ = app_module.load_vms_from_vinfo(str(CSV_INVENTORY))
    vm_names = [str(row["name"]) for row in inventory_rows]
    state_id = f"task12_markup_{uuid4().hex}"
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        state = app_module.load_app_state()
        state["selected_vm_names"] = vm_names
        state["step4_hybrid_placements"] = {
            "vm-app-01": "native",
            "vm-db-01": "native",
            "vm-web-01": "native",
            "vm-legacy-01": "ocvs",
        }
        state["acknowledged_warning_ids"] = ["unsupported-native"]
        state["assessor_recommendation"] = "hybrid"
        state["assessor_recommendation_rationale"] = (
            "Retain the legacy workload on OCVS while the supported estate moves to OCI Native."
        )
        app_module.save_app_state(state)

    routes = {
        "Setup": "/",
        "Inventory": "/step3",
        "Native": "/step4?tab=native",
        "OCVS": "/step4?tab=ocvs",
        "Hybrid": "/step4?tab=hybrid",
        "Results": "/step4?tab=price",
    }
    with app_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["state_id"] = state_id
            sess["selected_rvtools_file"] = str(CSV_INVENTORY)
            sess["selected_pricelist_file"] = price_file
            sess["selected_currency"] = "EUR"
            sess["active_assessment_name"] = "Task 12 accessibility assessment"
            sess["customer_name"] = "Task 12 responsive customer"
        responses = {name: client.get(route) for name, route in routes.items()}

    parsed = {
        name: parse_accessibility_markup(response.data)
        for name, response in responses.items()
    }
    check(
        "Task 12 stage fixtures render for markup audit",
        all(response.status_code == 200 for response in responses.values()),
        str({name: response.status_code for name, response in responses.items()}),
    )

    unnamed_by_stage = {
        name: parser.unnamed_controls()
        for name, parser in parsed.items()
        if parser.unnamed_controls()
    }
    check(
        "Task 12 every rendered form control has an accessible name",
        not unnamed_by_stage,
        str(unnamed_by_stage),
    )

    broken_label_refs: dict[str, list[str]] = {}
    for name, parser in parsed.items():
        missing: list[str] = []
        for control in parser.controls:
            attrs = control["attrs"]
            for reference in str(attrs.get("aria-labelledby") or "").split():
                if reference not in parser.elements_by_id:
                    missing.append(reference)
        if missing:
            broken_label_refs[name] = missing
    check(
        "Task 12 form-control aria-labelledby references resolve",
        not broken_label_refs,
        str(broken_label_refs),
    )

    duplicate_ids = {
        name: sorted({element_id for element_id in parser.ids if parser.ids.count(element_id) > 1})
        for name, parser in parsed.items()
    }
    duplicate_ids = {name: ids for name, ids in duplicate_ids.items() if ids}
    check("Task 12 rendered stages have no duplicate IDs", not duplicate_ids, str(duplicate_ids))

    inventory_parser = parsed["Inventory"]
    check(
        "Task 12 sortable inventory headers own initial aria-sort semantics",
        bool(inventory_parser.sort_headers)
        and all(header.get("aria-sort") in {"none", "ascending", "descending"} for header in inventory_parser.sort_headers),
        str(inventory_parser.sort_headers),
    )

    scenario_parser = parsed["Native"]
    panels_by_id = {
        str(panel.get("id") or ""): panel
        for panel in scenario_parser.tabpanels
        if panel.get("id")
    }
    tabs_by_id = {
        str(tab.get("id") or ""): tab
        for tab in scenario_parser.tabs
        if tab.get("id")
    }
    tabs_valid = all(
        tab.get("in_tablist") == "true"
        and tab.get("aria-selected") in {"true", "false"}
        and tab.get("tabindex") in {"0", "-1"}
        and str(tab.get("aria-controls") or "") in panels_by_id
        and panels_by_id[str(tab.get("aria-controls"))].get("aria-labelledby") == tab.get("id")
        for tab in scenario_parser.tabs
    )
    panels_valid = all(
        str(panel.get("aria-labelledby") or "") in tabs_by_id
        and tabs_by_id[str(panel.get("aria-labelledby"))].get("aria-controls") == panel.get("id")
        for panel in scenario_parser.tabpanels
    )
    check(
        "Task 12 tablist tabs and tabpanels have reciprocal relationships",
        len(scenario_parser.tabs) == 3
        and len(scenario_parser.tabpanels) == 3
        and tabs_valid
        and panels_valid,
        f"tabs={scenario_parser.tabs}, panels={scenario_parser.tabpanels}",
    )

    setup_html = responses["Setup"].data.decode("utf-8", errors="replace")
    inventory_html = responses["Inventory"].data.decode("utf-8", errors="replace")
    native_html = responses["Native"].data.decode("utf-8", errors="replace")
    hybrid_html = responses["Hybrid"].data.decode("utf-8", errors="replace")
    results_html = responses["Results"].data.decode("utf-8", errors="replace")
    check(
        "Task 12 flash dirty selection messages are live regions without floating Undo",
        all(
            parser.elements_by_id.get("workspace-status", {}).get("role") == "status"
            and parser.elements_by_id.get("workspace-status", {}).get("aria-live") == "polite"
            for parser in parsed.values()
        )
        and re.search(r'data-selection-status(?=[^>]*role="status")(?=[^>]*aria-live="polite")', inventory_html)
        and 'id="inventory-undo"' not in inventory_html
        and "data-undo" not in inventory_html
        and len(re.findall(r'data-scenario-dirty-live(?=[^>]*role="status")(?=[^>]*aria-live="polite")', native_html)) == 3,
    )
    check(
        "Task 12 status meaning is exposed with text beyond color",
        'class="results-readiness__indicator" aria-hidden="true"' in results_html
        and len(re.findall(r'class="result-status result-status--[^\"]+"', results_html)) >= 3
        and "Technical eligibility" in results_html
        and "Pricing completeness" in results_html
        and "Scenario readiness" in results_html,
    )

    submitted_control_counts = {
        "inventory-included": inventory_html.count('name="included_vm_names"'),
        "inventory-placement": len(re.findall(r'name="placement:[^"]+"', inventory_html)),
        "native-name": len(re.findall(r'<input[^>]+name="vm_name"', native_html)),
        "native-shape": len(re.findall(r'<select[^>]+name="oci_shape"', native_html)),
        "native-ocpu": len(re.findall(r'<input[^>]+name="vm_ocpu"', native_html)),
        "native-burst": len(re.findall(r'<select[^>]+name="vm_burst"', native_html)),
        "native-vpu": len(re.findall(r'<select[^>]+name="vm_vpu"', native_html)),
        "native-license": len(re.findall(r'<(?:input|select)[^>]+name="vm_os_license"', native_html)),
        "hybrid-placement": len(re.findall(r'name="hybrid_placement:[^"]+"', hybrid_html)),
    }
    check(
        "Task 12 mobile editors keep one submitted control DOM",
        all(count == len(vm_names) for count in submitted_control_counts.values()),
        f"expected={len(vm_names)}, counts={submitted_control_counts}",
    )

    workspace_css = (ROOT / "static" / "css" / "workspace.css").read_text(encoding="utf-8")
    inventory_js = (ROOT / "static" / "js" / "inventory-review.js").read_text(encoding="utf-8")
    scenario_js = (ROOT / "static" / "js" / "scenario-editor.js").read_text(encoding="utf-8")
    check(
        "Task 12 fixed stage actions reserve matching main-content padding",
        re.search(r"--workspace-mobile-action-reserve:\s*[1-9][0-9]+px", workspace_css)
        and re.search(r"#main-workspace\s*\{[^}]*padding-bottom:\s*var\(--workspace-mobile-action-reserve\)", workspace_css, re.S)
        and re.search(r"\.workspace-stage-actions\s*\{[^}]*position:\s*fixed;[^}]*bottom:\s*0;[^}]*min-height:\s*var\(--workspace-mobile-action-reserve\)", workspace_css, re.S),
    )
    check(
        "Task 12 fixed stage actions stay aligned to the workspace shell",
        re.search(r"\.workspace-stage-actions\s*\{[^}]*left:\s*calc\(var\(--workspace-rail-width\)[^;]+;", workspace_css, re.S)
        and re.search(r"@media \(max-width:\s*920px\).*?\.workspace-stage-actions\s*\{[^}]*left:\s*14px;[^}]*right:\s*14px;", workspace_css, re.S),
    )
    check(
        "Task 12 inventory sort updates header aria-sort semantics",
        'header.setAttribute("aria-sort", ascending ? "ascending" : "descending")' in inventory_js
        and 'control.header.setAttribute("aria-sort", "none")' in inventory_js,
    )
    check(
        "Task 12 Inventory bulk actions do not render or wire floating Undo",
        "undoReturnFocus" not in inventory_js
        and "undoButton" not in inventory_js
        and "undoSnapshot" not in inventory_js
        and "data-undo" not in inventory_js,
    )
    workspace_js = (ROOT / "static" / "js" / "workspace.js").read_text(encoding="utf-8")
    dialog_disclosure_contracts = {
        "dialog role": 'role="dialog"' in native_html,
        "modal state": 'aria-modal="true"' in native_html,
        "dialog focus target": 'tabindex="-1"' in native_html,
        "dialog Escape": 'event.key === "Escape"' in scenario_js,
        "dialog return focus": "dialogReturnFocus" in scenario_js,
        "dialog initial focus": "focusDialog" in scenario_js,
        "dialog Tab trap": 'event.key !== "Tab"' in scenario_js,
        "menu Escape": 'event.key === "Escape"' in workspace_js,
        "menu return focus": "closeMenu(true)" in workspace_js,
        "source details": "<details" in setup_html and "<summary" in setup_html,
    }
    check(
        "Task 12 dialogs and disclosures implement Escape and focus return",
        all(dialog_disclosure_contracts.values()),
        str(dialog_disclosure_contracts),
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
    inventory_rows, inventory_source = app_module.load_vms_from_vinfo(str(INVENTORY_REVIEW_INVENTORY))
    inventory_info = app_module.build_source_file_info(str(INVENTORY_REVIEW_INVENTORY))
    inventory_summary = app_module.build_inventory_import_summary(inventory_rows, inventory_source)

    with app_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["selected_pricelist_file"] = price_file
            sess["selected_currency"] = "EUR"
            sess["selected_rvtools_file"] = str(INVENTORY_REVIEW_INVENTORY)
            sess["rvtools_file_info"] = {
                "file_path": str(INVENTORY_REVIEW_INVENTORY),
                "file_name": INVENTORY_REVIEW_INVENTORY.name,
                "size_kb": inventory_info.get("size_kb", ""),
            }
            sess["rvtools_import_summary"] = inventory_summary

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
            "Stage 1 Assessment Identity asks only for customer project details",
            response.status_code == 200
            and "Assessment Identity" in assessment_html
            and 'id="customer_name"' in assessment_html
            and 'id="assessment_notes"' in assessment_html
            and "Customer / project name" in assessment_html
            and ">Notes<" in assessment_html,
        )
        identity_form = re.search(
            r'<form[^>]*>(?:(?!</form>).)*id="customer_name".*?</form>',
            assessment_html,
            re.S,
        )
        identity_form_html = identity_form.group(0) if identity_form else ""
        check(
            "Assessment Identity form saves without a visible assessment name",
            'name="assessment_name"' not in identity_form_html
            and 'id="assessment_name"' not in identity_form_html
            and "Assessment name" not in identity_form_html
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
            "Stage 1 inventory source prioritizes RVTools upload and keeps manual entry as a fallback",
            "Inventory Source" in inventory_html
            and "Upload an RVTools export or reuse a previously uploaded file." in inventory_html
            and "Upload RVTools file" in inventory_html
            and "Use saved inventory" in inventory_html
            and "No RVTools file? Create manual summary" in inventory_html
            and "Manual Workload Summary" in inventory_html
            and "Upload or catalog" not in inventory_html
            and "Inventory mode" not in inventory_html
            and len(re.findall(r'name="inventory_mode"', inventory_html)) == 2
            and 'value="upload"' in inventory_html
            and 'value="manual"' in inventory_html
            and inventory_html.index("Upload RVTools file") < inventory_html.index("Use saved inventory")
            and re.search(r'<details[^>]+class="manual-inventory-fallback"[^>]*>', inventory_html)
            and not re.search(r'<details[^>]+class="manual-inventory-fallback"[^>]*\sopen(?:\s|=|>)', inventory_html),
            inventory_html,
        )
        inventory_upload_grid = re.search(
            r'<div class="inventory-upload-grid">(?P<body>.*?)</div>\s*</div>\s*</div>',
            inventory_html,
            re.S,
        )
        upload_grid_html = inventory_upload_grid.group("body") if inventory_upload_grid else ""
        index_template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        check(
            "Stage 1 inventory upload controls share aligned field layout",
            upload_grid_html.count("inventory-upload-card") == 2
            and "grid-template-rows: auto auto minmax(40px, 1fr) auto;" in index_template
            and ".inventory-upload-card .setup-actions" in index_template
            and "margin-top: auto;" in index_template,
            upload_grid_html,
        )
        check(
            "Stage 1 warning review remains informational without edit actions",
            "Inventory quality checks" in inventory_html
            and "Warning Review" not in inventory_html
            and "warning-review__action" not in html
            and "<th>Recommendation</th>" not in inventory_html
            and "<th>Action</th>" not in inventory_html
            and "?warning=missing-storage" not in html
            and "Edit storage" not in html
            and "Set OCVS" not in html
            and "Review storage inputs" in html
            and "Review Native treatment" in html,
            inventory_html,
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
        and "[data-manual-inventory-fallback]" in setup_js_text
        and "setInventoryMode" in setup_js_text
        and ".hidden =" in setup_js_text
        and 'setAttribute("aria-hidden"' in setup_js_text
        and "errorSummary.focus(" in setup_js_text
        and re.search(r"\.value\s*=[^=]", setup_js_text) is None,
    )


def validate_stage1_identity_save_and_loaded_manual_mode() -> None:
    price_file = find_price_file()
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
            r'<form[^>]*>(?:(?!</form>).)*id="customer_name".*?</form>',
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
        generated_name = saved_session_identity["name"]
        generated_name_matches = re.fullmatch(
            rf"{re.escape(customer_name)} - \d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}",
            generated_name,
        )
        check(
            "identity Save Assessment button persists visible values and auto-generates the assessment name",
            response.status_code == 200
            and submitted_action == "save_assessment"
            and generated_name_matches
            and saved_session_identity == {"name": generated_name, "customer": customer_name, "notes": assessment_notes}
            and saved_snapshot.get("name") == generated_name
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
        upload_panel = re.search(
            r'<div(?=[^>]*data-inventory-mode-panel="upload")[^>]*>',
            loaded_html,
            re.S,
        )
        upload_panel_tag = upload_panel.group(0) if upload_panel else ""
        with client.session_transaction() as sess:
            loaded_manual_path = str(sess.get("selected_rvtools_file", ""))
        check(
            "loading saved manual assessment keeps replacement inventory controls visible",
            response.status_code == 200
            and loaded_manual_path == saved_manual_path
            and manual_radio is not None
            and 'aria-hidden="false"' in manual_panel_tag
            and re.search(r"\shidden(?:\s|>)", manual_panel_tag) is None
            and 'aria-hidden="false"' in upload_panel_tag
            and re.search(r"\shidden(?:\s|>)", upload_panel_tag) is None
            and "Upload RVTools File" in loaded_html
            and "Use Saved Inventory" in loaded_html,
            f"loaded={loaded_manual_path}, manual_panel={manual_panel_tag}, upload_panel={upload_panel_tag}",
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
        and issues_by_id.get("missing-storage", {}).get("severity") == "advisory",
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
            state.get("acknowledged_warning_ids") == ["unsupported-native", "missing-storage", "unknown-os"],
            str(state.get("acknowledged_warning_ids")),
        )
        check(
            "missing values are handled as acknowledgeable information warnings",
            "missing-storage" in state.get("acknowledged_warning_ids", []),
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
            and 'id="inventory-undo"' not in html
            and "data-undo" not in html
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
        hidden_detail_rows = re.findall(
            r'<tr[^>]*class="inventory-details-row"[^>]*data-inventory-detail="[^"]+"[^>]*hidden',
            html,
        )
        check(
            "Stage 2 starts row details collapsed without mobile row duplication",
            len(hidden_detail_rows) == len(rows)
            and "Inventory quality notes" in html
            and "Warning inbox" not in html,
            f"hidden={len(hidden_detail_rows)}, rows={len(rows)}",
        )
        check(
            "inventory quality notes are merged into the table review flow",
            "0 blocking" not in html
            and "0 critical" not in html
            and (
                f"{sum(1 for issue in issues if issue.get('severity') == 'advisory')} advisory notes"
                in html
            )
            and "All VMs" in html
            and "Follow-up" in html
            and "Use the note filters with the VM inventory table below" in html
            and "Review in table" not in html
            and "View affected VMs" not in html
            and "additional affected VM" not in html
            and "Affected VMs and detected values" not in html,
        )
        check(
            "warning filtering and note badges keep affected VM values in the inventory table",
            "warning-item__affected-list" not in html
            and 'data-warning-filter="unsupported-native"' in html
            and 'data-warning-item="unsupported-native"' in html
            and 'data-note-details-target="inventory-details-' in html
            and "inventory-warning-button" in html
            and "inventory-note-list" in html
            and "Detected value" in html
            and "Native migration requires a documented remediation treatment" in html
            and "warning-item__treatment" not in html
            and "Recommended treatment" not in html
            and "Action:" not in html
        )
        check(
            "warning inbox is informational without remediation controls",
            "I reviewed the affected VMs and treatment." not in html
            and "Correct in Setup or source inventory" not in html
            and "warning-item__remediation" not in html,
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

    client, warning_state_id = inventory_client(INVENTORY_REVIEW_INVENTORY)
    response = client.post(
        "/step3",
        data=MultiDict(
            [
                ("action", "save_inventory_review"),
                ("continue_to_scenarios", "1"),
                ("included_vm_names", "review-supported"),
                ("placement:review-supported", "native"),
            ]
        ),
    )
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = warning_state_id
        warning_continue_state = app_module.load_app_state()
    check(
        "inventory review warnings persist while Continue remains available",
        response.status_code in {302, 303}
        and response.headers.get("Location", "").endswith("/step4?tab=native")
        and warning_continue_state.get("selected_vm_names") == ["review-supported"]
        and warning_continue_state.get("step4_hybrid_placements") == {"review-supported": "native"}
        and warning_continue_state.get("acknowledged_warning_ids") == [],
        f"status={response.status_code}, location={response.headers.get('Location')}, state={warning_continue_state}",
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
        "inventory review continue allows unacknowledged advisory warnings",
        response.status_code in {302, 303}
        and response.headers.get("Location", "").endswith("/step4?tab=native"),
        f"status={response.status_code}, location={response.headers.get('Location')}",
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
    inventory_css = (ROOT / "static" / "css" / "inventory-review.css").read_text(encoding="utf-8")
    mobile_hidden_columns_match = re.search(
        r"@media \(max-width: 767px\).*?#inventory-table \.inventory-col-power(?P<block>.*?)\{\s*display:\s*none;",
        inventory_css,
        re.S,
    )
    mobile_hidden_columns = mobile_hidden_columns_match.group("block") if mobile_hidden_columns_match else ""
    check(
        "inventory controller uses bounded cached interactions without floating undo state",
        "const rowRecords" in inventory_js
        and "function visibleRecords" in inventory_js
        and "clearTimeout(searchTimer)" in inventory_js
        and "setTimeout" in inventory_js
        and "function runBulk" in inventory_js
        and "placementsByIndex" in inventory_js
        and "snapshotState" not in inventory_js
        and "undoSnapshot" not in inventory_js
        and "undoButton" not in inventory_js
        and '.closest("th")' in inventory_js,
    )
    check(
        "inventory row details remain reachable on mobile without rendering every detail row",
        "function syncDetailVisibility(record)" in inventory_js
        and "function setDetailsOpen(record, open)" in inventory_js
        and "noteButton" in inventory_js
        and "[data-note-details-target]" in inventory_js
        and "record.detailRow.hidden = !rowVisible || !detailsOpen" in inventory_js
        and ".inventory-col-details" not in mobile_hidden_columns,
        mobile_hidden_columns,
    )
    check(
        "inventory quality note context is compact and table-integrated",
        re.search(
            r"\.warning-inbox__summary\s*\{[^}]*display:\s*grid;",
            inventory_css,
            re.S,
        )
        is not None
        and re.search(
            r"\.warning-context\s*\{[^}]*display:\s*grid;",
            inventory_css,
            re.S,
        )
        is not None,
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
        "inventory review persists valid state while advisories remain nonblocking",
        response.status_code in {302, 303}
        and response.headers.get("Location", "").endswith("/step4?tab=native")
        and state_after_not_ready.get("selected_vm_names") == ["vm-db-01"]
        and state_after_not_ready.get("step4_hybrid_placements") == {"vm-db-01": "native"}
        and state_after_not_ready.get("acknowledged_warning_ids") == [],
        f"status={response.status_code}, location={response.headers.get('Location')}, state={state_after_not_ready}",
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
        "legacy inventory action saves valid state and returns to the current review boundary",
        response.status_code in {302, 303}
        and response.headers.get("Location", "").endswith("/step3")
        and legacy_state != legacy_prior
        and legacy_state.get("selected_vm_names") == ["vm-app-01"]
        and legacy_state.get("step4_hybrid_placements") == {"vm-app-01": "native"},
        f"status={response.status_code}, location={response.headers.get('Location')}, state={legacy_state}",
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
    check(
        "desktop inventory bulk placement aligns Apply with the select control",
        re.search(
            r"\.inventory-command-group--placement\s*\{[^}]*align-items:\s*flex-end;",
            inventory_css,
            re.S,
        )
        is not None
        and re.search(
            r"\.inventory-command-group--placement\s+\.inventory-button\s*\{[^}]*align-self:\s*flex-end;",
            inventory_css,
            re.S,
        )
        is not None,
    )


def validate_task7_native_scenario_workspace() -> None:
    rows, _source = app_module.load_vms_from_vinfo(str(NATIVE_SCENARIO_INVENTORY))
    check("Task 7 Native fixture has 75 selected and one non-selected VM", len(rows) == 76, str(len(rows)))
    selected_names = [str(row["name"]) for row in rows[:75]]
    non_selected_name = str(rows[75]["name"])
    rows_by_name = {str(row["name"]): row for row in rows}
    supported_signatures = app_module.load_supported_os_signatures()
    placements = {
        name: app_module.default_inventory_placement(rows_by_name[name], supported_signatures)
        for name in selected_names
    }
    state_id = f"task7_native_{uuid4().hex}"
    price_file = find_price_file()

    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        state = app_module.load_app_state()
        state["selected_vm_names"] = selected_names
        state["step4_hybrid_placements"] = placements
        state["acknowledged_warning_ids"] = ["unsupported-native", "unknown-os"]
        state["step4_vm_shapes"] = {
            selected_names[0]: "E4",
            selected_names[50]: "E5",
        }
        state["step4_vm_bursts"] = {
            selected_names[0]: "100%",
            selected_names[50]: "12.5%",
        }
        app_module.save_app_state(state)
        app_module.save_step4_snapshot({"marker": "task7-prior-snapshot"})

    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
        sess["state_id"] = state_id
        sess["selected_rvtools_file"] = str(NATIVE_SCENARIO_INVENTORY)
        sess["selected_pricelist_file"] = price_file
        sess["customer_name"] = "Task 7 Customer"
        sess["active_assessment_name"] = "Task 7 Assessment"

    paths_response = client.get("/step4?tab=paths", follow_redirects=False)
    paths_alias = client.get("/scenario/paths", follow_redirects=False)
    price_alias = client.get("/scenario/price", follow_redirects=False)
    native_alias = client.get("/scenario/native", follow_redirects=False)
    check(
        "Task 7 scenario aliases route to their owning stages",
        paths_response.status_code in {302, 303}
        and paths_response.headers.get("Location", "").endswith("/step3")
        and paths_alias.headers.get("Location", "").endswith("/step3")
        and price_alias.headers.get("Location", "").endswith("/step4?tab=price")
        and native_alias.headers.get("Location", "").endswith("/step4?tab=native"),
        str(
            {
                "paths": paths_response.headers.get("Location"),
                "paths_alias": paths_alias.headers.get("Location"),
                "price_alias": price_alias.headers.get("Location"),
                "native_alias": native_alias.headers.get("Location"),
            }
        ),
    )

    response = client.get("/step4?tab=native")
    html = response.data.decode("utf-8", errors="replace")
    tablist_match = re.search(
        r'<div\b[^>]*class="[^"]*top-tabs[^"]*"[^>]*role="tablist"[^>]*>(.*?)</div>',
        html,
        re.S,
    )
    tablist_html = tablist_match.group(1) if tablist_match else ""
    tabs = re.findall(r'<button\b([^>]*role="tab"[^>]*)>(.*?)</button>', tablist_html, re.S)
    tab_labels = [re.sub(r"<[^>]+>", "", label).strip() for _attrs, label in tabs]
    active_tabs = [attrs for attrs, _label in tabs if 'aria-selected="true"' in attrs]
    inactive_tabs = [attrs for attrs, _label in tabs if 'aria-selected="false"' in attrs]
    check(
        "Stage 3 tablist contains only Native OCVS and Hybrid with one roving active tab",
        response.status_code == 200
        and tab_labels == ["Native", "OCVS", "Hybrid"]
        and len(active_tabs) == 1
        and 'tabindex="0"' in active_tabs[0]
        and all('tabindex="-1"' in attrs for attrs in inactive_tabs)
        and "Migration Paths" not in tablist_html
        and "Price Comparison" not in tablist_html
        and "disabled" not in tabs[0][0],
        f"labels={tab_labels}, active={active_tabs}, inactive={inactive_tabs}",
    )
    check(
        "Results remains reachable through shared four-stage navigation",
        'href="/step4?tab=price"' in html and "Results &amp; Export" in html,
    )

    page_rows = re.findall(r'<tr\b[^>]*data-native-editor-row[^>]*>', html)
    monthly_cost_match = re.search(r'data-native-monthly-cost="([0-9.]+)"', html)
    check(
        "Native page one renders 50 of 75 editor rows while retaining full-scope totals",
        len(page_rows) == 50
        and 'data-native-workload-count="75"' in html
        and 'data-native-editor-filtered-count="75"' in html
        and 'data-native-page="1"' in html
        and 'data-native-page-count="2"' in html
        and monthly_cost_match is not None
        and float(monthly_cost_match.group(1)) > 0,
        f"rows={len(page_rows)}, monthly={monthly_cost_match.group(1) if monthly_cost_match else None}",
    )
    native_header = re.search(r'<header\b[^>]*data-scenario-header="native".*?</header>', html, re.S)
    native_header_html = native_header.group(0) if native_header else ""
    check(
        "Native summary uses backend readiness and keeps unsupported workloads modeled",
        'data-native-readiness-state="needs_attention"' in native_header_html
        and "Requires remediation" in native_header_html
        and "Recalculate &amp; Save" in html
        and "75 VMs" in native_header_html
        and "Ineligible" not in native_header_html
        and "Not saved yet" in native_header_html,
        native_header_html[:800],
    )

    first_vm = selected_names[0]
    expected_labels = {
        "ocpu": "OCPU",
        "burst": "burst",
        "vpu": "VPU",
        "oci-shape": "OCI target shape",
    }
    accessible_controls = True
    for suffix, setting in expected_labels.items():
        control_id = f"native-row-1-{suffix}"
        label_match = re.search(
            rf'<label\b[^>]*for="{re.escape(control_id)}"[^>]*>(.*?)</label>',
            html,
            re.S,
        )
        label_text = re.sub(r"<[^>]+>", " ", label_match.group(1)) if label_match else ""
        accessible_controls = accessible_controls and bool(
            label_match and first_vm in " ".join(label_text.split()) and setting in label_text
        )
    rendered_ids = re.findall(r'\bid="([^"]+)"', html)
    check(
        "Native editor controls use stable row IDs and explicit VM setting labels",
        accessible_controls
        and all(first_vm not in control_id for control_id in rendered_ids)
        and 'aria-label="Native editor rows"' in html,
    )
    submitted_control_counts = {
        name: len(re.findall(rf'<(?:input|select)\b[^>]*name="{re.escape(name)}"', html))
        for name in ("vm_name", "vm_ocpu", "vm_burst", "vm_vpu", "oci_shape", "vm_os_license")
    }
    submitted_control_counts["editor_rows"] = html.count("data-native-editor-row")
    check(
        "Native desktop and mobile rendering share one submitted control tree",
        all(count == 50 for count in submitted_control_counts.values()),
        str(submitted_control_counts),
    )
    native_pagination = re.search(
        r'<nav\b[^>]*class="[^"]*native-pagination[^"]*"[^>]*>(.*?)</nav>',
        html,
        re.S,
    )
    native_pagination_html = native_pagination.group(1) if native_pagination else ""
    check(
        "Native pagination separates page status from a bounded page list",
        native_pagination is not None
        and 'class="native-pagination__status"' in native_pagination_html
        and 'class="native-pagination__list"' in native_pagination_html,
        native_pagination_html[:800],
    )
    mobile_nav = re.search(
        r'<div\b[^>]*data-native-mobile-nav[^>]*>(.*?)</div>',
        html,
        re.S,
    )
    mobile_nav_html = mobile_nav.group(1) if mobile_nav else ""
    check(
        "Native mobile editor exposes one accessible focused-row navigator",
        mobile_nav is not None
        and 'data-native-mobile-previous' in mobile_nav_html
        and 'data-native-mobile-next' in mobile_nav_html
        and 'data-native-mobile-status' in mobile_nav_html
        and 'aria-live="polite"' in mobile_nav_html
        and 'VM 1 of 50' in mobile_nav_html
        and html.count('data-native-mobile-active="true"') == 1
        and html.count("data-native-mobile-index=") == 50,
        mobile_nav_html,
    )

    remediation_response = client.get("/step4?tab=native&native_support=remediation")
    remediation_html = remediation_response.data.decode("utf-8", errors="replace")
    remediation_rows = re.findall(r'<tr\b[^>]*data-native-editor-row[^>]*>', remediation_html)
    remediation_label = re.search(
        r'<label\b[^>]*for="native-row-71-os-license"[^>]*>(.*?)</label>',
        remediation_html,
        re.S,
    )
    remediation_label_text = re.sub(r"<[^>]+>", " ", remediation_label.group(1)) if remediation_label else ""
    remediation_cost_match = re.search(r'data-native-monthly-cost="([0-9.]+)"', remediation_html)
    check(
        "Native remediation filter renders three supported-in-scope editor rows with full totals",
        remediation_response.status_code == 200
        and len(remediation_rows) == 3
        and remediation_html.count("Requires remediation") >= 4
        and 'data-native-editor-filtered-count="3"' in remediation_html
        and remediation_cost_match is not None
        and monthly_cost_match is not None
        and remediation_cost_match.group(1) == monthly_cost_match.group(1)
        and "native-page-vm-071" in remediation_label_text
        and "OS license" in remediation_label_text,
        f"rows={len(remediation_rows)}, label={remediation_label_text}",
    )

    search_response = client.get("/step4?tab=native&native_search=075")
    search_html = search_response.data.decode("utf-8", errors="replace")
    check(
        "Native search filters editor rows only",
        search_html.count("data-native-editor-row") == 1
        and 'data-native-editor-filtered-count="1"' in search_html
        and 'data-native-workload-count="75"' in search_html,
    )
    invalid_response = client.get(
        "/step4?tab=native&native_page=not-a-page&native_page_size=77&native_support=invalid"
    )
    invalid_html = invalid_response.data.decode("utf-8", errors="replace")
    clamped_response = client.get("/step4?tab=native&native_page=999")
    clamped_html = clamped_response.data.decode("utf-8", errors="replace")
    preserved_query_response = client.get(
        "/step4?tab=native&native_page=2&native_page_size=25&native_search=native-page&native_support=supported"
    )
    preserved_query_html = preserved_query_response.data.decode("utf-8", errors="replace")
    check(
        "Native pagination inputs normalize malformed values and clamp ranges",
        'data-native-page="1"' in invalid_html
        and 'data-native-page-size="50"' in invalid_html
        and 'data-native-support="all"' in invalid_html
        and 'data-native-page="2"' in clamped_html
        and clamped_html.count("data-native-editor-row") == 25,
    )
    check(
        "Native pagination links preserve tab search size and support state",
        "tab=native" in preserved_query_html
        and "native_page_size=25" in preserved_query_html
        and "native_search=native-page" in preserved_query_html
        and "native_support=supported" in preserved_query_html,
    )

    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        state_before_empty_save = app_module.load_app_state()
    empty_save = client.post(
        "/step4",
        data={"action": "save", "active_scenario": "native"},
        follow_redirects=False,
    )
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        state_after_empty_save = app_module.load_app_state()
    with client.session_transaction() as sess:
        empty_save_marked_unsaved = (
            sess.get(app_module.STEP4_UNSAVED_READINESS_SESSION_KEY) is True
        )
    empty_save_redirect = client.get(empty_save.headers.get("Location", ""))
    check(
        "Empty Native save rejects without persistence and marks redirected readiness unsaved",
        empty_save.status_code in {302, 303}
        and state_after_empty_save == state_before_empty_save
        and empty_save_marked_unsaved
        and b"No scenario settings were submitted" in empty_save_redirect.data,
        f"status={empty_save.status_code}, location={empty_save.headers.get('Location')}",
    )

    def native_page_form(page_names: list[str], page: int) -> MultiDict:
        form = MultiDict(
            [
                ("action", "save"),
                ("active_scenario", "native"),
                ("native_page", str(page)),
                ("native_page_size", "50"),
                ("native_search", ""),
                ("native_support", "all"),
            ]
        )
        form.setlist("vm_name", page_names)
        form.setlist("oci_shape", ["E6"] * len(page_names))
        form.setlist("vm_ocpu", ["3"] * len(page_names))
        form.setlist("vm_burst", ["50%"] * len(page_names))
        form.setlist("vm_vpu", ["20"] * len(page_names))
        form.setlist(
            "vm_os_license",
            [
                "BYOL"
                if "windows server" in str(rows_by_name.get(name, {}).get("raw_os", "")).lower()
                else ""
                for name in page_names
            ],
        )
        return form

    def persistence_bytes() -> tuple[bytes, bytes]:
        with app_module.app.test_request_context("/"):
            app_module.session["state_id"] = state_id
            return (
                app_module._state_file_path().read_bytes(),
                app_module._step4_snapshot_file_path().read_bytes(),
            )

    page_one_names = selected_names[:50]
    page_two_names = selected_names[50:]
    adversarial_forms: list[tuple[str, MultiDict]] = []

    duplicate_identity = native_page_form(page_one_names, 1)
    duplicate_identity.setlist("vm_name", [page_one_names[0], page_one_names[0], *page_one_names[2:]])
    adversarial_forms.append(("duplicate Native VM identity", duplicate_identity))

    unknown_identity = native_page_form(page_one_names, 1)
    unknown_identity.setlist("vm_name", ["unknown-native-vm", *page_one_names[1:]])
    adversarial_forms.append(("unknown Native VM identity mixed with valid rows", unknown_identity))

    non_selected_identity = native_page_form(page_one_names, 1)
    non_selected_identity.setlist("vm_name", [non_selected_name, *page_one_names[1:]])
    adversarial_forms.append(("non-selected Native VM identity", non_selected_identity))

    off_page_identity = native_page_form(page_one_names, 1)
    off_page_identity.setlist("vm_name", [page_two_names[0], *page_one_names[1:]])
    adversarial_forms.append(("off-page Native VM identity", off_page_identity))

    missing_identity = native_page_form(page_one_names[:-1], 1)
    adversarial_forms.append(("missing Native VM identity", missing_identity))

    extra_identity = native_page_form([*page_one_names, page_two_names[0]], 1)
    adversarial_forms.append(("extra Native VM identity", extra_identity))

    tampered_page = native_page_form(page_two_names, 1)
    adversarial_forms.append(("tampered Native page declaration", tampered_page))

    missing_control = native_page_form(page_one_names, 1)
    missing_control.setlist("vm_vpu", missing_control.getlist("vm_vpu")[:-1])
    adversarial_forms.append(("missing Native positional control", missing_control))

    extra_control = native_page_form(page_one_names, 1)
    extra_control.setlist("oci_shape", [*extra_control.getlist("oci_shape"), "E6"])
    adversarial_forms.append(("extra Native positional control", extra_control))

    for field_name, invalid_value in (
        ("oci_shape", "not-a-shape"),
        ("vm_ocpu", "1.5"),
        ("vm_burst", "75%"),
        ("vm_vpu", "15"),
    ):
        invalid_form = native_page_form(page_one_names, 1)
        invalid_values = invalid_form.getlist(field_name)
        invalid_values[1] = invalid_value
        invalid_form.setlist(field_name, invalid_values)
        adversarial_forms.append((f"invalid Native {field_name} mixed with valid rows", invalid_form))

    invalid_license = native_page_form(page_two_names, 2)
    invalid_license_values = invalid_license.getlist("vm_os_license")
    invalid_license_values[20] = "invalid-license"
    invalid_license.setlist("vm_os_license", invalid_license_values)
    adversarial_forms.append(("invalid Native license mixed with valid rows", invalid_license))

    for field_name, invalid_value in (
        ("bulk_apply_oci_shape", "not-a-shape"),
        ("bulk_apply_burst", "75%"),
        ("bulk_apply_vpu", "15"),
        ("bulk_apply_os_license", "invalid-license"),
    ):
        invalid_bulk = MultiDict(
            [
                ("action", "save"),
                ("active_scenario", "native"),
                ("native_page", "1"),
                ("native_page_size", "50"),
                ("native_search", ""),
                ("native_support", "all"),
                (field_name, invalid_value),
            ]
        )
        adversarial_forms.append((f"invalid Native {field_name}", invalid_bulk))

    adversarial_results: list[tuple[str, bool, str]] = []
    for label, form in adversarial_forms:
        before_state_bytes, before_snapshot_bytes = persistence_bytes()
        invalid_response = client.post("/step4", data=form, follow_redirects=False)
        after_state_bytes, after_snapshot_bytes = persistence_bytes()
        with client.session_transaction() as sess:
            marked_unsaved = sess.get(app_module.STEP4_UNSAVED_READINESS_SESSION_KEY) is True
        adversarial_results.append(
            (
                label,
                invalid_response.status_code in {302, 303}
                and marked_unsaved
                and after_state_bytes == before_state_bytes
                and after_snapshot_bytes == before_snapshot_bytes,
                invalid_response.headers.get("Location", ""),
            )
        )
    check(
        "Adversarial Native page payloads reject transactionally",
        all(passed for _label, passed, _location in adversarial_results),
        str(adversarial_results),
    )

    prior_state_bytes, prior_snapshot_bytes = persistence_bytes()
    original_save_step4_snapshot = app_module.save_step4_snapshot

    def reject_native_snapshot(_snapshot: dict[str, object]) -> None:
        raise OSError("injected Native snapshot persistence failure")

    app_module.save_step4_snapshot = reject_native_snapshot
    try:
        persistence_failure = client.post(
            "/step4",
            data=native_page_form(page_one_names, 1),
            follow_redirects=False,
        )
    finally:
        app_module.save_step4_snapshot = original_save_step4_snapshot
    after_failure_state_bytes, after_failure_snapshot_bytes = persistence_bytes()
    with client.session_transaction() as sess:
        persistence_failure_marked_unsaved = (
            sess.get(app_module.STEP4_UNSAVED_READINESS_SESSION_KEY) is True
        )
    check(
        "Native persistence failure rolls back app state and snapshot byte-for-byte",
        persistence_failure.status_code in {302, 303}
        and persistence_failure_marked_unsaved
        and after_failure_state_bytes == prior_state_bytes
        and after_failure_snapshot_bytes == prior_snapshot_bytes,
        f"status={persistence_failure.status_code}",
    )

    page_two_post = client.post(
        "/step4",
        data=native_page_form(page_two_names, 2),
        follow_redirects=False,
    )
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        updated_state = app_module.load_app_state()
    check(
        "Valid Native page 2 POST updates its exact controls and preserves page 1",
        page_two_post.status_code in {302, 303}
        and updated_state.get("step4_vm_shapes", {}).get(first_vm) == "E4"
        and updated_state.get("step4_vm_shapes", {}).get(selected_names[1]) is None
        and all(updated_state.get("step4_vm_shapes", {}).get(name) == "E6" for name in page_two_names)
        and updated_state.get("step4_vm_bursts", {}).get(first_vm) == "100%"
        and all(updated_state.get("step4_vm_bursts", {}).get(name) == "50%" for name in page_two_names)
        and all(updated_state.get("step4_vm_vpus", {}).get(name) == 20 for name in page_two_names)
        and updated_state.get("step4_hybrid_placements") == placements,
        f"location={page_two_post.headers.get('Location')}, state={updated_state}",
    )

    task7_export = client.post(
        "/step4",
        data={"action": "export_excel", "active_scenario": "native"},
        follow_redirects=False,
    )
    task7_export_path = ""
    with client.session_transaction() as sess:
        task7_export_path = str(sess.get("last_export_file", "") or "")
    try:
        with zipfile.ZipFile(BytesIO(task7_export.data)) as zf:
            task7_sheet_map = workbook_sheet_map(zf)
            selected_vm_rows = sheet_text_rows(zf, task7_sheet_map["Selected VMs"])
            exported_vm_names = {
                row[0]
                for row in selected_vm_rows[1:]
                if row and row[0]
            }
            native_analysis_rows = sheet_text_rows(
                zf,
                task7_sheet_map["OCI Native Analysis"],
            )
            native_aggregates = {
                row[0]: row[1]
                for row in native_analysis_rows
                if len(row) >= 2
                and row[0] in {"VM Count", "vCPU Count", "Memory GB", "Storage GB"}
            }
        check(
            "Task 7 workbook export retains all 75 VMs and full-list Native totals",
            task7_export.status_code == 200
            and task7_export.data.startswith(b"PK")
            and exported_vm_names == set(selected_names)
            and native_aggregates == {
                "VM Count": "75",
                "vCPU Count": "300",
                "Memory GB": "600",
                "Storage GB": "7500",
            },
            f"names={len(exported_vm_names)}, aggregates={native_aggregates}",
        )
    finally:
        if task7_export_path:
            Path(task7_export_path).unlink(missing_ok=True)

    price_response = client.get("/step4?tab=price")
    price_html = price_response.data.decode("utf-8", errors="replace")
    check(
        "Price renders as Stage 4 Results without joining the Stage 3 tablist",
        price_response.status_code == 200
        and "Step 4 of 4" in price_html
        and 'role="tablist"' not in price_html
        and 'id="scenario-panel-price"' in price_html
        and 'aria-labelledby="scenario-tab-price"' not in price_html
        and 'aria-label="Results and price comparison"' in price_html,
    )
    check(
        "Task 9 Results exposes explicit status cost and decision fields",
        'data-results-comparison' in price_html
        and 'data-overall-readiness="draft_review_required"' in price_html
        and price_html.count('data-result-scenario="') == 3
        and price_html.count("Technical eligibility") == 3
        and price_html.count("Pricing completeness") == 3
        and price_html.count("Modeled cost") == 3
        and price_html.count("Scenario readiness") == 3
        and re.search(
            r'data-result-scenario="native"[^>]*data-readiness-state="needs_attention".*?result-status--attention"[^>]*>.*?Needs attention',
            price_html,
            re.S,
        )
        is not None
        and 'data-result-scenario="ocvs"' in price_html
        and 'data-result-scenario="hybrid"' in price_html
        and all(
            label in price_html
            for label in (
                "Monthly",
                "Annual",
                "3-year",
                "Cost per VM",
                "Placement split",
                "Assumptions and sizing",
                "Benefits",
                "Trade-offs",
            )
        ),
    )
    check(
        "Task 9 Results removes remediation requirements and VCF blocker copy",
        "Remediation requirements" not in price_html
        and "VCF license price not set" not in price_html
        and "Lowest complete modeled price" not in price_html
        and (
            price_html.count("Complete pricing")
            + price_html.count("Incomplete pricing")
        ) >= 3
        and (
            price_html.count("Complete modeled amount")
            + price_html.count("Partial modeled amount")
        ) >= 3,
    )
    check(
        "Task 9 Results has no automatic choice language",
        re.search(
            r"\b(winner|best)\b",
            visible_text_outside_details(price_response.data),
            re.I,
        )
        is None
        and "automatic recommendation" not in price_html.lower(),
    )
    check(
        "Task 9 Results displays modeled rank medals",
        price_html.count("result-rank-medal") >= 3
        and "Rank 1" in price_html
        and "Rank 2" in price_html
        and "Rank 3" in price_html
        and "result-rank-medal--gold" in price_html
        and "result-rank-medal--silver" in price_html
        and "result-rank-medal--bronze" in price_html,
    )
    check(
        "Task 9 Results compacts informational readiness notes",
        "readiness-panel--compact" in price_html
        and "<details" in price_html
        and "<summary" in price_html
        and "View informational notes" in price_html,
    )
    check(
        "Task 9 Results restores workload profile analytics",
        'data-workload-profile' in price_html
        and "Operating system mix" in price_html
        and "Power state" in price_html
        and "OCI Native readiness" in price_html
        and "Avg vCPU / VM" in price_html,
    )
    check(
        "Task 9 recommendation and export controls distinguish draft status from Excel action",
        'name="recommendation"' in price_html
        and all(f'value="{value}"' in price_html for value in ("native", "ocvs", "hybrid", ""))
        and "Migration specialist recommendation" in price_html
        and "Save the recommended path for the report. This choice does not recalculate costs or change the ranking." in price_html
        and "Record the preferred path for internal review. This does not change modeled pricing." not in price_html
        and "Recommended path" in price_html
        and "Undecided" in price_html
        and "Internal notes" in price_html
        and "Optional notes explaining the recommendation." in price_html
        and "Save decision" in price_html
        and "Assessor recommendation" not in price_html
        and "Required for customer-ready Native treatment" not in price_html
        and 'name="recommendation_rationale"' in price_html
        and 'maxlength="4000"' in price_html
        and 'aria-live="polite"' in price_html
        and 'value="save_assessment"' in price_html
        and 'value="export_excel"' in price_html
        and re.search(
            r'<button type="submit" class="results-button">\s*Export Excel\s*</button>',
            price_html,
        )
        is not None
        and "Export Draft" not in price_html
        and "export_json" not in price_html
        and "Portable JSON" not in price_html,
    )

    task9_rationale = "Keep the OCVS path available while VCF pricing is confirmed."
    recommendation_response = client.post(
        "/step4",
        data={
            "action": "save_recommendation",
            "recommendation": "ocvs",
            "recommendation_rationale": task9_rationale,
        },
        follow_redirects=False,
    )
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        recommendation_state = app_module.load_app_state()
    recommendation_reload = client.get("/step4?tab=price")
    recommendation_html = recommendation_reload.data.decode("utf-8", errors="replace")
    check(
        "Task 9 persists an incomplete-scenario draft recommendation through reload",
        recommendation_response.status_code == 303
        and recommendation_response.headers.get("Location", "").endswith("/step4?tab=price")
        and recommendation_state.get("assessor_recommendation") == "ocvs"
        and recommendation_state.get("assessor_recommendation_rationale") == task9_rationale
        and re.search(r'value="ocvs"\s+checked', recommendation_html) is not None
        and task9_rationale in recommendation_html
        and "Incomplete pricing" in recommendation_html,
        f"status={recommendation_response.status_code}, state={recommendation_state}",
    )

    invalid_recommendation_forms = {
        "duplicate recommendation": MultiDict(
            [
                ("action", "save_recommendation"),
                ("recommendation", "native"),
                ("recommendation", "hybrid"),
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
                ("winner", "native"),
            ]
        ),
    }
    invalid_results: list[tuple[str, bool]] = []
    for label, form in invalid_recommendation_forms.items():
        before_state_bytes, before_snapshot_bytes = persistence_bytes()
        invalid_response = client.post("/step4", data=form, follow_redirects=False)
        after_state_bytes, after_snapshot_bytes = persistence_bytes()
        invalid_results.append(
            (
                label,
                invalid_response.status_code == 303
                and invalid_response.headers.get("Location", "").endswith("/step4?tab=price")
                and after_state_bytes == before_state_bytes
                and after_snapshot_bytes == before_snapshot_bytes,
            )
        )
    check(
        "Task 9 rejects duplicate oversized and unknown recommendation fields transactionally",
        all(passed for _label, passed in invalid_results),
        str(invalid_results),
    )

    malformed_recommendation_values = (
        ("integer", 1),
        ("list", ["native"]),
        ("mapping", {"value": "native"}),
        ("none", None),
    )
    malformed_type_results: list[tuple[str, bool]] = []
    for field_name, expected_error in (
        ("recommendation", "Specialist recommendation must be text."),
        ("recommendation_rationale", "Recommendation rationale must be text."),
    ):
        for value_label, malformed_value in malformed_recommendation_values:
            malformed_form_values = {
                "action": "save_recommendation",
                "recommendation": "ocvs",
                "recommendation_rationale": task9_rationale,
            }
            malformed_form_values[field_name] = malformed_value
            malformed_form = MultiDict(malformed_form_values.items())
            _parsed, malformed_errors = app_module.parse_recommendation_submission(
                malformed_form
            )
            before_state_bytes, before_snapshot_bytes = persistence_bytes()
            with app_module.app.test_request_context("/step4", method="POST"):
                app_module.session["_app_instance_id"] = app_module.APP_INSTANCE_ID
                app_module.session["state_id"] = state_id
                app_module.session["selected_rvtools_file"] = str(
                    NATIVE_SCENARIO_INVENTORY
                )
                app_module.session["selected_pricelist_file"] = price_file
                app_module.session["customer_name"] = "Task 7 Customer"
                app_module.session["active_assessment_name"] = "Task 7 Assessment"
                app_module.request.form = malformed_form
                route_result = app_module.step4()
            after_state_bytes, after_snapshot_bytes = persistence_bytes()
            route_status = route_result[1] if isinstance(route_result, tuple) else 0
            malformed_type_results.append(
                (
                    f"{field_name} {value_label}",
                    expected_error in malformed_errors
                    and route_status == 303
                    and after_state_bytes == before_state_bytes
                    and after_snapshot_bytes == before_snapshot_bytes,
                )
            )
    check(
        "Task 9 rejects non-string recommendation fields before normalization",
        all(passed for _label, passed in malformed_type_results),
        str(malformed_type_results),
    )

    saved_response = client.post(
        "/",
        data={
            "action": "save_assessment",
            "assessment_name": "Task 9 Results Snapshot",
            "assessment_notes": "Recommendation persistence regression.",
        },
        follow_redirects=True,
    )
    saved_results = next(
        item
        for item in app_module.list_saved_assessments()
        if item.get("name") == "Task 9 Results Snapshot"
    )
    saved_results_id = str(saved_results["id"])
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        mutated_recommendation_state = app_module.load_app_state()
        mutated_recommendation_state["assessor_recommendation"] = "hybrid"
        mutated_recommendation_state["assessor_recommendation_rationale"] = "Mutated after save."
        app_module.save_app_state(mutated_recommendation_state)
    loaded_response = client.post(
        "/",
        data={"action": "load_assessment", "assessment_id": saved_results_id},
        follow_redirects=True,
    )
    with client.session_transaction() as sess:
        restored_state_id = str(sess.get("state_id", ""))
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = restored_state_id
        restored_recommendation_state = app_module.load_app_state()
    check(
        "Task 9 recommendation survives local assessment save and load",
        saved_response.status_code == 200
        and loaded_response.status_code == 200
        and restored_recommendation_state.get("assessor_recommendation") == "ocvs"
        and restored_recommendation_state.get("assessor_recommendation_rationale") == task9_rationale,
        str(restored_recommendation_state),
    )
    client.post(
        "/",
        data={"action": "delete_assessment", "assessment_id": saved_results_id},
        follow_redirects=True,
    )
    app_module.save_preferences({})

    positive_vcf_response = client.post(
        "/step4",
        data={
            "action": "save",
            "active_scenario": "ocvs",
            "vmware_license_price_per_core_yearly": "400",
        },
        follow_redirects=False,
    )
    native_rationale = "Remediate unsupported guests before Native placement."
    ready_recommendation = client.post(
        "/step4",
        data={
            "action": "save_recommendation",
            "recommendation": "native",
            "recommendation_rationale": native_rationale,
        },
        follow_redirects=True,
    )
    ready_html = ready_recommendation.data.decode("utf-8", errors="replace")
    ready_ids = re.findall(r'\bid="([^"]+)"', ready_html)
    check(
        "Task 9 Native treatment unlocks only the customer-ready export label",
        positive_vcf_response.status_code in {302, 303}
        and ready_recommendation.status_code == 200
        and re.search(
            r'<button type="submit" class="results-button">\s*Export Excel\s*</button>',
            ready_html,
        )
        is not None
        and native_rationale in ready_html
        and re.search(
            r'data-result-scenario="native"[^>]*data-readiness-state="needs_attention".*?result-status--attention"[^>]*>.*?Needs attention',
            ready_html,
            re.S,
        )
        is not None
        and re.search(
            r'data-result-scenario="ocvs"[^>]*data-readiness-state="incomplete".*?result-status--blocked"[^>]*>.*?Incomplete',
            ready_html,
            re.S,
        )
        is not None
        and re.search(
            r'data-result-scenario="hybrid"[^>]*data-readiness-state="incomplete".*?result-status--blocked"[^>]*>.*?Incomplete',
            ready_html,
            re.S,
        )
        is not None
        and "Technical eligibility" in ready_html
        and len(ready_ids) == len(set(ready_ids)),
        f"positive={positive_vcf_response.status_code}, duplicates={len(ready_ids) - len(set(ready_ids))}",
    )

    step4_source = (ROOT / "templates" / "step4.html").read_text(encoding="utf-8")
    header_partial_path = ROOT / "templates" / "_scenario_header.html"
    native_partial_path = ROOT / "templates" / "_scenario_native.html"
    scenarios_css_path = ROOT / "static" / "css" / "scenarios.css"
    scenario_js_path = ROOT / "static" / "js" / "scenario-editor.js"
    scenarios_css = scenarios_css_path.read_text(encoding="utf-8") if scenarios_css_path.exists() else ""
    scenario_js = scenario_js_path.read_text(encoding="utf-8") if scenario_js_path.exists() else ""
    results_partial_path = ROOT / "templates" / "_results_comparison.html"
    export_partial_path = ROOT / "templates" / "_export_center.html"
    results_css_path = ROOT / "static" / "css" / "results.css"
    results_css = results_css_path.read_text(encoding="utf-8") if results_css_path.exists() else ""
    check(
        "Task 9 Results uses dedicated partials and responsive non-gradient styling",
        results_partial_path.exists()
        and export_partial_path.exists()
        and results_css_path.exists()
        and '{% include "_results_comparison.html" %}' in step4_source
        and '{% include "_export_center.html" %}' in step4_source
        and "css/results.css" in step4_source
        and "gradient" not in results_css.lower()
        and "@media (max-width: 640px)" in results_css
        and "minmax(0, 1fr)" in results_css,
    )
    check(
        "Stage 3 uses scenario header and Native content partials with conventional assets",
        header_partial_path.exists()
        and native_partial_path.exists()
        and '{% include "_scenario_header.html" %}' in step4_source
        and '{% include "_scenario_native.html" %}' in step4_source
        and "css/scenarios.css" in step4_source
        and "js/scenario-editor.js" in step4_source,
    )
    check(
        "Scenario editor source retains dirty state across tabs and focuses one mobile row",
        all(token in scenario_js for token in [
            "ArrowLeft",
            "ArrowRight",
            "Home",
            "End",
            "aria-selected",
            "tabindex",
            "beforeunload",
            "data-scenario-dirty-live",
            "data-dirty-navigation",
            "workspace-stage-select",
            "control.form === scenarioForm",
            "confirmScenarioSwitch",
            "navigationConfirmed",
            "matchMedia(\"(max-width: 767px)\")",
            "data-native-mobile-active",
            "row.hidden",
        ])
        and "clearDirty" not in scenario_js,
    )
    check(
        "Scenario CSS provides sticky desktop and focused mobile editor contracts",
        all(token in scenarios_css for token in [
            "overflow-x: auto",
            "position: sticky",
            "max-width: 100%",
            "@media (max-width: 600px)",
            "@media (max-width: 767px)",
            ".native-mobile-row-nav",
            'data-native-mobile-active="true"',
            "--oracle-red",
            "--status-green",
        ])
        and re.search(
            r"html,\s*body\s*\{[^}]*overflow-x:\s*clip;",
            scenarios_css,
            re.S,
        ) is not None
        and re.search(
            r"html,\s*body\s*\{[^}]*overflow-x:\s*hidden;",
            scenarios_css,
            re.S,
        ) is None
        and re.search(
            r"#scenario-panel-native\s*\{[^}]*overflow:\s*visible\s*!important;",
            scenarios_css,
            re.S,
        ) is not None
        and re.search(
            r"#scenario-panel-native\s*\{[^}]*overflow:\s*(?:hidden|clip);",
            scenarios_css,
            re.S,
        ) is None
        and re.search(
            r"\.native-editor-scroll\s*\{[^}]*overflow-x:\s*auto;",
            scenarios_css,
            re.S,
        ) is not None,
    )
    check(
        "Native pagination keeps long page sets bounded and aligned",
        ".native-pagination__status" in scenarios_css
        and ".native-pagination__list" in scenarios_css
        and re.search(
            r"\.native-pagination__list\s*\{[^}]*flex-wrap:\s*wrap;",
            scenarios_css,
            re.S,
        ) is not None
        and re.search(
            r"\.native-pagination__list\s*\{[^}]*max-width:\s*100%;",
            scenarios_css,
            re.S,
        ) is not None,
    )


def validate_task8_ocvs_hybrid_configuration() -> None:
    rows, _source = app_module.load_vms_from_vinfo(str(NATIVE_SCENARIO_INVENTORY))
    selected_rows = rows[:75]
    selected_names = [str(row["name"]) for row in selected_rows]
    placements = {name: "native" for name in selected_names}
    placements[selected_names[0]] = "ocvs"
    placements[selected_names[1]] = "review"
    expected_plan = app_module.build_hybrid_placement_plan(
        [
            {
                "vm_name": str(row["name"]),
                "os_name": str(row.get("raw_os") or "Unknown / Empty"),
            }
            for row in selected_rows
        ],
        placements,
        app_module.load_supported_os_signatures(),
    )
    state_id = f"task8_ocvs_hybrid_{uuid4().hex}"
    price_file = find_price_file()

    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        state = app_module.load_app_state()
        state["selected_vm_names"] = selected_names
        state["step4_hybrid_placements"] = placements
        state["acknowledged_warning_ids"] = ["unsupported-native", "unknown-os"]
        state["step4_ocvs_profile"] = "BM.Standard.E4.128"
        state["step4_ocvs_commitment_term"] = "3_year"
        state["step4_vmware_license_price_per_core_yearly"] = 0.0
        app_module.save_app_state(state)

    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
        sess["state_id"] = state_id
        sess["selected_rvtools_file"] = str(NATIVE_SCENARIO_INVENTORY)
        sess["selected_pricelist_file"] = price_file
        sess["customer_name"] = "Task 8 Customer"
        sess["active_assessment_name"] = "Task 8 Assessment"

    zero_vcf_response = client.get("/step4?tab=ocvs")
    zero_vcf_html = zero_vcf_response.data.decode("utf-8", errors="replace")
    step4_source = (ROOT / "templates" / "step4.html").read_text(encoding="utf-8")
    ocvs_partial = ROOT / "templates" / "_scenario_ocvs.html"
    hybrid_partial = ROOT / "templates" / "_scenario_hybrid.html"
    check(
        "Task 8 extracts OCVS and Hybrid into scenario partials",
        zero_vcf_response.status_code == 200
        and ocvs_partial.exists()
        and hybrid_partial.exists()
        and '{% include "_scenario_ocvs.html" %}' in step4_source
        and '{% include "_scenario_hybrid.html" %}' in step4_source,
    )
    check(
        "OCVS groups the established controls under four decision headings",
        all(
            heading in zero_vcf_html
            for heading in ("Profile &amp; Term", "Capacity Policy", "Resilience", "VCF Licensing")
        )
        and zero_vcf_html.count('name="ocvs_profile"') == 1
        and zero_vcf_html.count('name="ocvs_commitment_term"') == 1
        and zero_vcf_html.count('name="ocvs_vcpu_per_ocpu"') == 1
        and zero_vcf_html.count('name="ocvs_cpu_headroom_pct"') == 1
        and zero_vcf_html.count('name="ocvs_memory_headroom_pct"') == 1
        and zero_vcf_html.count('name="ocvs_storage_headroom_pct"') == 1
        and zero_vcf_html.count('name="ocvs_dense_vsan_usable_pct"') == 1
        and zero_vcf_html.count('name="ocvs_standard_storage_vpu"') == 1
        and zero_vcf_html.count('name="ocvs_dr_nodes"') == 1
        and zero_vcf_html.count('name="vmware_license_price_per_core_yearly"') == 1,
    )
    selected_discount = app_module.ocvs_term_discount_pct("BM.Standard.E4.128", "3_year")
    check(
        "OCVS renders the selected shape term discount from the backend",
        'data-ocvs-commitment-term="3_year"' in zero_vcf_html
        and f'data-ocvs-term-discount-pct="{selected_discount:.2f}"' in zero_vcf_html
        and f"{selected_discount:.0f}% selected-shape discount" in zero_vcf_html,
    )
    check(
        "Zero VCF price preserves infrastructure subtotal and stays rankable",
        'data-ocvs-readiness-state="ready"' in zero_vcf_html
        and 'data-ocvs-rankable="true"' in zero_vcf_html
        and 'data-hybrid-rankable="true"' in zero_vcf_html
        and re.search(r'data-ocvs-infrastructure-subtotal="[1-9][0-9.]*"', zero_vcf_html) is not None
        and "Infrastructure subtotal" in zero_vcf_html
        and "Optional add-on" in zero_vcf_html
        and "Base monthly total" in zero_vcf_html
        and "Pricing incomplete" not in zero_vcf_html,
    )

    hybrid_response = client.get("/step4?tab=hybrid")
    hybrid_html = hybrid_response.data.decode("utf-8", errors="replace")
    check(
        "Hybrid renders workload partitions subset sizing and override values",
        hybrid_response.status_code == 200
        and f'data-hybrid-native-count="{expected_plan["native_count"]}"' in hybrid_html
        and f'data-hybrid-ocvs-count="{expected_plan["ocvs_count"]}"' in hybrid_html
        and f'data-hybrid-review-count="{expected_plan["review_count"]}"' in hybrid_html
        and f'data-hybrid-ocvs-priced-count="{expected_plan["ocvs_priced_count"]}"' in hybrid_html
        and f'data-hybrid-manual-override-count="{expected_plan["manual_override_count"]}"' in hybrid_html
        and "OCVS Subset Sizing" in hybrid_html
        and "Hybrid OCVS assumptions" in hybrid_html
        and "Inherited from OCVS scenario" in hybrid_html
        and 'name="hybrid_ocvs_profile"' in hybrid_html
        and 'name="hybrid_vmware_license_price_per_core_yearly"' in hybrid_html,
    )
    check(
        "Hybrid owns keyed placement controls and separate OCVS assumption inputs",
        len(re.findall(r'<select\b[^>]*data-hybrid-placement-select', hybrid_html, re.S)) == len(selected_names)
        and all(hybrid_html.count(f'name="hybrid_placement:{name}"') == 1 for name in selected_names)
        and hybrid_html.count('name="vmware_license_price_per_core_yearly"') == 1
        and hybrid_html.count('name="hybrid_vmware_license_price_per_core_yearly"') == 1
        and hybrid_html.count('name="hybrid_ocvs_commitment_term"') == 1
        and hybrid_html.count('name="hybrid_ocvs_vcpu_per_ocpu"') == 1
        and hybrid_html.count('name="hybrid_ocvs_standard_storage_vpu"') == 1,
    )

    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        before_spoof = app_module.load_app_state()
    spoof_form = MultiDict([("action", "save"), ("active_scenario", "hybrid")])
    for name in selected_names:
        spoof_form.add(f"hybrid_placement:{name}", placements[name])
    spoof_form.add("hybrid_placement:not-in-scope", "ocvs")
    spoof_response = client.post("/step4", data=spoof_form, follow_redirects=False)
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        after_spoof = app_module.load_app_state()
    check(
        "Hybrid exact-scope keyed spoof rejects without partial mutation",
        spoof_response.status_code in {302, 303} and after_spoof == before_spoof,
        f"status={spoof_response.status_code}",
    )

    rows_by_name = {str(row["name"]): row for row in selected_rows}
    native_page_names = sorted(selected_names, key=str.lower)[:50]
    ocvs_policy = app_module.normalize_ocvs_policy({})

    def ocvs_save_form(
        vcf_price: str,
        *,
        native_ocpu: str = "3",
        dr_nodes: str = "0",
    ) -> MultiDict:
        form = MultiDict(
            [
                ("action", "save"),
                ("active_scenario", "ocvs"),
                ("native_page", "1"),
                ("native_page_size", "50"),
                ("native_search", ""),
                ("native_support", "all"),
                ("iaas_discount_pct", "0"),
                ("ocvs_profile", "BM.Standard.E4.128"),
                ("ocvs_commitment_term", "3_year"),
                ("ocvs_vcpu_per_ocpu", str(ocvs_policy["vcpu_per_ocpu"])),
                ("ocvs_cpu_headroom_pct", str(ocvs_policy["cpu_headroom_pct"])),
                ("ocvs_memory_headroom_pct", str(ocvs_policy["memory_headroom_pct"])),
                ("ocvs_storage_headroom_pct", str(ocvs_policy["storage_headroom_pct"])),
                ("ocvs_dense_vsan_usable_pct", str(ocvs_policy["dense_vsan_usable_pct"])),
                ("ocvs_standard_storage_vpu", str(ocvs_policy["standard_storage_vpu"])),
                ("ocvs_dr_nodes", dr_nodes),
                ("vmware_license_price_per_core_yearly", vcf_price),
            ]
        )
        form.setlist("vm_name", native_page_names)
        form.setlist("oci_shape", ["E6"] * len(native_page_names))
        form.setlist("vm_ocpu", [native_ocpu] * len(native_page_names))
        form.setlist("vm_burst", ["50%"] * len(native_page_names))
        form.setlist("vm_vpu", ["20"] * len(native_page_names))
        form.setlist(
            "vm_os_license",
            [
                "BYOL"
                if "windows server" in str(rows_by_name[name].get("raw_os", "")).lower()
                else ""
                for name in native_page_names
            ],
        )
        for name in selected_names:
            form.add(f"hybrid_placement:{name}", placements[name])
        return form

    positive_vcf_post = client.post(
        "/step4",
        data=ocvs_save_form("360.00"),
        follow_redirects=False,
    )
    positive_vcf_response = client.get(positive_vcf_post.headers.get("Location", ""))
    positive_vcf_html = positive_vcf_response.data.decode("utf-8", errors="replace")
    positive_hybrid_response = client.get("/step4?tab=hybrid")
    positive_hybrid_html = positive_hybrid_response.data.decode("utf-8", errors="replace")
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        priced_state = app_module.load_app_state()
        priced_snapshot = app_module.load_step4_snapshot()
    check(
        "OCVS POST persists positive VCF pricing through the normal save transaction",
        positive_vcf_post.status_code in {302, 303}
        and positive_vcf_post.headers.get("Location", "").endswith("/step4?tab=ocvs")
        and priced_state.get("step4_vmware_license_price_per_core_yearly") == 360.0
        and priced_snapshot.get("vmware_license_price_per_core_yearly") == 360.0,
        f"status={positive_vcf_post.status_code}, location={positive_vcf_post.headers.get('Location')}",
    )
    check(
        "Redirected OCVS and reloaded Hybrid become pricing-complete and rankable",
        positive_vcf_response.status_code == 200
        and positive_hybrid_response.status_code == 200
        and 'data-ocvs-rankable="true"' in positive_vcf_html
        and 'data-ocvs-readiness-state="ready"' in positive_vcf_html
        and 'data-hybrid-rankable="true"' in positive_hybrid_html
        and 'data-hybrid-readiness-state="ready"' in positive_hybrid_html
        and positive_vcf_html.count("Complete monthly total") >= 2
        and positive_hybrid_html.count("Complete monthly total") >= 2
        and "Unit price required" not in positive_vcf_html
        and "Unit price required" not in positive_hybrid_html,
    )
    check(
        "Hybrid defaults inherit OCVS pricing until customized",
        positive_vcf_html.count('name="vmware_license_price_per_core_yearly"') == 1
        and positive_vcf_html.count('name="hybrid_vmware_license_price_per_core_yearly"') == 1
        and positive_hybrid_html.count('name="vmware_license_price_per_core_yearly"') == 1
        and positive_hybrid_html.count('name="hybrid_vmware_license_price_per_core_yearly"') == 1
        and re.search(
            r'name="hybrid_vmware_license_price_per_core_yearly"[^>]*value="360\.00"',
            positive_hybrid_html,
            re.S,
        )
        is not None
        and 'data-hybrid-rankable="true"' in positive_hybrid_html,
    )

    def hybrid_save_form(vcf_price: str, *, commitment_term: str = "payg") -> MultiDict:
        form = MultiDict(
            [
                ("action", "save"),
                ("active_scenario", "hybrid"),
                ("hybrid_ocvs_profile", "BM.Standard.E4.128"),
                ("hybrid_ocvs_commitment_term", commitment_term),
                ("hybrid_ocvs_vcpu_per_ocpu", str(ocvs_policy["vcpu_per_ocpu"])),
                ("hybrid_ocvs_cpu_headroom_pct", str(ocvs_policy["cpu_headroom_pct"])),
                ("hybrid_ocvs_memory_headroom_pct", str(ocvs_policy["memory_headroom_pct"])),
                ("hybrid_ocvs_storage_headroom_pct", str(ocvs_policy["storage_headroom_pct"])),
                ("hybrid_ocvs_dense_vsan_usable_pct", str(ocvs_policy["dense_vsan_usable_pct"])),
                ("hybrid_ocvs_standard_storage_vpu", str(ocvs_policy["standard_storage_vpu"])),
                ("hybrid_ocvs_dr_nodes", "0"),
                ("hybrid_vmware_license_price_per_core_yearly", vcf_price),
            ]
        )
        for name in selected_names:
            form.add(f"hybrid_placement:{name}", placements[name])
        return form

    hybrid_custom_post = client.post(
        "/step4",
        data=hybrid_save_form("120.00", commitment_term="payg"),
        follow_redirects=False,
    )
    hybrid_custom_response = client.get(hybrid_custom_post.headers.get("Location", ""))
    hybrid_custom_html = hybrid_custom_response.data.decode("utf-8", errors="replace")
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        customized_state = app_module.load_app_state()
    check(
        "Hybrid OCVS assumptions persist separately after customization",
        hybrid_custom_post.status_code in {302, 303}
        and hybrid_custom_post.headers.get("Location", "").endswith("/step4?tab=hybrid")
        and customized_state.get("step4_ocvs_commitment_term") == "3_year"
        and customized_state.get("step4_vmware_license_price_per_core_yearly") == 360.0
        and customized_state.get("step4_hybrid_ocvs_customized") is True
        and customized_state.get("step4_hybrid_ocvs_commitment_term") == "payg"
        and customized_state.get("step4_hybrid_vmware_license_price_per_core_yearly") == 120.0
        and "Customized for Hybrid" in hybrid_custom_html
        and re.search(
            r'name="hybrid_vmware_license_price_per_core_yearly"[^>]*value="120\.00"',
            hybrid_custom_html,
            re.S,
        )
        is not None
        and re.search(
            r'name="vmware_license_price_per_core_yearly"[^>]*value="360\.00"',
            hybrid_custom_html,
            re.S,
        )
        is not None,
        f"status={hybrid_custom_post.status_code}, location={hybrid_custom_post.headers.get('Location')}",
    )

    scenario_js = (ROOT / "static" / "js" / "scenario-editor.js").read_text(encoding="utf-8")
    scenario_css = (ROOT / "static" / "css" / "scenarios.css").read_text(encoding="utf-8")

    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        state_path = app_module._state_file_path()
        snapshot_path = app_module._step4_snapshot_file_path()
    baseline_state_bytes = state_path.read_bytes()
    baseline_snapshot_bytes = snapshot_path.read_bytes()

    rendered_scalar_fields = (
        "action",
        "active_scenario",
        "continue_to_results",
        "native_page",
        "native_page_size",
        "native_search",
        "native_support",
        "bulk_apply_oci_shape",
        "bulk_apply_burst",
        "bulk_apply_vpu",
        "bulk_apply_os_license",
        "native_shape_strategy_enabled",
        "iaas_discount_pct",
        "ocvs_profile",
        "ocvs_commitment_term",
        "ocvs_vcpu_per_ocpu",
        "ocvs_cpu_headroom_pct",
        "ocvs_memory_headroom_pct",
        "ocvs_storage_headroom_pct",
        "ocvs_dense_vsan_usable_pct",
        "ocvs_standard_storage_vpu",
        "ocvs_dr_nodes",
        "vmware_license_price_per_core_yearly",
    )
    malformed_scalar_cases = (
        ("unknown action", "action", "publish"),
        ("unknown active scenario", "active_scenario", "elsewhere"),
        ("unknown OCVS profile", "ocvs_profile", "BM.Unknown.1"),
        ("aliased commitment term", "ocvs_commitment_term", "one_year"),
        ("malformed DR nodes", "ocvs_dr_nodes", "not-a-number"),
        ("NaN DR nodes", "ocvs_dr_nodes", "nan"),
        ("fractional DR nodes", "ocvs_dr_nodes", "1.5"),
        ("out-of-range DR nodes", "ocvs_dr_nodes", "3"),
        ("NaN IaaS discount", "iaas_discount_pct", "nan"),
        ("negative IaaS discount", "iaas_discount_pct", "-0.01"),
        ("out-of-range IaaS discount", "iaas_discount_pct", "100.01"),
        ("malformed VCF price", "vmware_license_price_per_core_yearly", "not-a-price"),
        ("infinite VCF price", "vmware_license_price_per_core_yearly", "inf"),
        ("negative VCF price", "vmware_license_price_per_core_yearly", "-1"),
        ("out-of-range VCF price", "vmware_license_price_per_core_yearly", "1000000.01"),
        ("NaN vCPU/OCPU policy", "ocvs_vcpu_per_ocpu", "nan"),
        ("infinite vCPU/OCPU policy", "ocvs_vcpu_per_ocpu", "inf"),
        ("low vCPU/OCPU policy", "ocvs_vcpu_per_ocpu", "0.9"),
        ("high vCPU/OCPU policy", "ocvs_vcpu_per_ocpu", "16.1"),
        ("negative CPU headroom", "ocvs_cpu_headroom_pct", "-1"),
        ("fractional CPU headroom", "ocvs_cpu_headroom_pct", "20.5"),
        ("high RAM headroom", "ocvs_memory_headroom_pct", "91"),
        ("infinite RAM headroom", "ocvs_memory_headroom_pct", "-inf"),
        ("NaN storage headroom", "ocvs_storage_headroom_pct", "nan"),
        ("fractional storage headroom", "ocvs_storage_headroom_pct", "25.5"),
        ("low dense vSAN usable", "ocvs_dense_vsan_usable_pct", "9"),
        ("high dense vSAN usable", "ocvs_dense_vsan_usable_pct", "96"),
        ("fractional dense vSAN usable", "ocvs_dense_vsan_usable_pct", "50.5"),
        ("malformed storage VPU", "ocvs_standard_storage_vpu", "none"),
        ("fractional storage VPU", "ocvs_standard_storage_vpu", "10.5"),
        ("off-step storage VPU", "ocvs_standard_storage_vpu", "15"),
        ("high storage VPU", "ocvs_standard_storage_vpu", "121"),
    )
    invalid_post_failures: list[str] = []
    invalid_post_cases: list[tuple[str, str, list[str]]] = [
        (label, field_name, [value])
        for label, field_name, value in malformed_scalar_cases
    ]
    for field_name in rendered_scalar_fields:
        base_values = ocvs_save_form("360.00").getlist(field_name) or [""]
        invalid_post_cases.append(
            (f"duplicate {field_name}", field_name, [base_values[0], base_values[0]])
        )

    for label, field_name, submitted_values in invalid_post_cases:
        form = ocvs_save_form("360.00", native_ocpu="4", dr_nodes="2")
        form.setlist(field_name, submitted_values)
        before_state_bytes = state_path.read_bytes()
        before_snapshot_bytes = snapshot_path.read_bytes()
        response = client.post("/step4", data=form, follow_redirects=False)
        after_state_bytes = state_path.read_bytes()
        after_snapshot_bytes = snapshot_path.read_bytes()
        with client.session_transaction() as sess:
            marked_unsaved = (
                sess.get(app_module.STEP4_UNSAVED_READINESS_SESSION_KEY) is True
            )
        location = response.headers.get("Location", "")
        redirected = client.get(location) if location else response
        rejected = (
            response.status_code in {302, 303}
            and before_state_bytes == after_state_bytes
            and before_snapshot_bytes == after_snapshot_bytes
            and marked_unsaved
            and b"No scenario settings were saved" in redirected.data
        )
        if not rejected:
            invalid_post_failures.append(
                f"{label}(status={response.status_code}, state={before_state_bytes == after_state_bytes}, "
                f"snapshot={before_snapshot_bytes == after_snapshot_bytes}, unsaved={marked_unsaved})"
            )
        state_path.write_bytes(baseline_state_bytes)
        snapshot_path.write_bytes(baseline_snapshot_bytes)

    unknown_review_plan = app_module.build_hybrid_placement_plan(
        [{"vm_name": "review-baseline", "os_name": "Unknown"}],
        {"review-baseline": "review"},
        app_module.load_supported_os_signatures(),
    )
    unknown_native_plan = app_module.build_hybrid_placement_plan(
        [{"vm_name": "review-baseline", "os_name": "Unknown"}],
        {"review-baseline": "native"},
        app_module.load_supported_os_signatures(),
    )
    unknown_ocvs_plan = app_module.build_hybrid_placement_plan(
        [{"vm_name": "review-baseline", "os_name": "Unknown"}],
        {"review-baseline": "ocvs"},
        app_module.load_supported_os_signatures(),
    )
    review_row = unknown_review_plan.get("rows", [{}])[0]
    hybrid_review_baseline_valid = (
        review_row.get("hybrid_recommended_placement") == "review"
        and review_row.get("hybrid_effective_target") == "ocvs"
        and review_row.get("hybrid_manual_override") is False
        and unknown_review_plan.get("manual_override_count") == 0
        and unknown_review_plan.get("ocvs_priced_count") == 1
        and unknown_native_plan.get("manual_override_count") == 1
        and unknown_ocvs_plan.get("manual_override_count") == 1
    )

    rendered_ids = re.findall(r'\bid="([^"]+)"', positive_vcf_html)
    compact_live_ids = ("ocvs-dirty-status", "hybrid-dirty-status")
    compact_live_semantics_valid = (
        len(rendered_ids) == len(set(rendered_ids))
        and positive_vcf_html.count("data-scenario-dirty-live") == 3
        and all(
            re.search(
                rf'<span(?=[^>]*\bid="{status_id}")(?=[^>]*\bdata-scenario-dirty-live)(?=[^>]*\brole="status")(?=[^>]*\baria-live="polite")[^>]*>',
                positive_vcf_html,
            )
            is not None
            for status_id in compact_live_ids
        )
        and ".scenario-save-bar--compact > span" not in scenario_js
    )

    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        corrupt_state = app_module.load_app_state()
        corrupt_snapshot = app_module.load_step4_snapshot()
        corrupt_state["step4_iaas_discount_pct"] = float("inf")
        corrupt_state["step4_vmware_license_price_per_core_yearly"] = float("nan")
        corrupt_state["step4_ocvs_policy"] = {
            "vcpu_per_ocpu": float("inf"),
            "cpu_headroom_pct": float("nan"),
            "memory_headroom_pct": float("-inf"),
            "storage_headroom_pct": float("nan"),
            "dense_vsan_usable_pct": float("inf"),
            "standard_storage_vpu": float("nan"),
        }
        corrupt_snapshot["vmware_license_price_per_core_yearly"] = float("inf")
        app_module.save_app_state(corrupt_state)
        app_module.save_step4_snapshot(corrupt_snapshot)
    corrupt_response = client.get("/step4?tab=ocvs")
    corrupt_html = corrupt_response.data.decode("utf-8", errors="replace")
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        normalized_corrupt_state = app_module.load_app_state()
    normalized_policy = normalized_corrupt_state.get("step4_ocvs_policy", {})
    finite_stored_state_valid = (
        app_module._bounded_float(float("nan"), 0.0, 0.0, 100.0) == 0.0
        and app_module._bounded_float(float("inf"), 0.0, 0.0, 100.0) == 0.0
        and app_module._bounded_float(float("-inf"), 0.0, 0.0, 100.0) == 0.0
        and normalized_corrupt_state.get("step4_iaas_discount_pct") == 0.0
        and normalized_corrupt_state.get("step4_vmware_license_price_per_core_yearly") == 0.0
        and normalized_policy == app_module.OCVS_DEFAULT_SIZING_POLICY
        and 'value="0.00"' in corrupt_html
        and "Optional add-on" in corrupt_html
        and "Unit price required" not in corrupt_html
        and 'data-ocvs-readiness-state="ready"' in corrupt_html
        and 'data-ocvs-rankable="true"' in corrupt_html
        and 'data-hybrid-rankable="true"' in corrupt_html
    )
    state_path.write_bytes(baseline_state_bytes)
    snapshot_path.write_bytes(baseline_snapshot_bytes)

    quality_failures = [
        *invalid_post_failures,
        *([] if finite_stored_state_valid else ["stored non-finite normalization"]),
        *([] if hybrid_review_baseline_valid else ["Hybrid Review recommendation baseline"]),
        *([] if compact_live_semantics_valid else ["compact dirty live-region semantics"]),
    ]
    check(
        "Task 8 strict scalar transactions finite stored state Review baseline and live semantics",
        not quality_failures,
        "; ".join(quality_failures),
    )

    # Task 8 browser probes at 390/1280 verified page/filtered/all bulk scope,
    # Undo, and preservation of later row edits. Task 12 owns automated events.
    check(
        "Hybrid editor exposes search support placement scope bulk and surgical Undo hooks",
        all(
            token in hybrid_html
            for token in (
                'data-hybrid-search',
                'data-hybrid-support-filter',
                'data-hybrid-placement-filter',
                'data-hybrid-bulk-scope',
                'data-hybrid-bulk-placement',
                'data-hybrid-bulk-apply',
                'data-hybrid-bulk-undo',
                "Current page",
                "All filtered rows",
                "All selected VMs",
            )
        )
        and all(
            token in scenario_js
            for token in (
                "hybridBulkSnapshot",
                "data-hybrid-bulk-undo",
                "affectedRows",
                "function rowsForBulkScope()",
                'if (scope === "all") return rows;',
                'if (scope === "filtered") return filteredRows;',
                "return pageRows;",
                "if (item.select.value !== item.applied) return;",
                "later row edits were kept",
                "renderHybridEditor",
                "markDirty",
            )
        )
        and ".hybrid-editor-scroll" in scenario_css
        and "overflow-x: auto" in scenario_css,
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
            b"Inventory quality checks" in response.data
            and b"Unsupported for OCI Native" in response.data
            and b"manual-vm-006" in response.data
            and b"Solaris 11.4" in response.data
            and b"Review Native treatment" in response.data,
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
            "empty saved assessment list uses concise load controls",
            response.status_code == 200
            and b"Saved Assessments" in response.data
            and b"Saved assessment" in response.data
            and b"No saved assessments yet" in response.data
            and b'name="assessment_id"' in response.data
            and b"Load Previous Assessment" not in response.data
            and b"Reopen or remove a locally saved workspace." not in response.data
            and b"Previously saved" not in response.data,
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


def validate_start_fresh_assessment() -> None:
    price_file = find_price_file()
    _price_lookup, expected_currency, _source_file = app_module.load_price_lookup(price_file)
    expected_currency = str(expected_currency or "").upper().strip()

    with app_module.app.test_client() as client:
        response = client.get("/")
        response_html = response.data.decode("utf-8", errors="ignore")
        header_html_match = re.search(r'<header class="workspace-header">.*?</header>', response_html, re.S)
        header_html = header_html_match.group(0) if header_html_match else ""
        saved_assessments_match = re.search(
            r'<div id="saved-assessments".*?</div>\s*</section>',
            response_html,
            re.S,
        )
        saved_assessments_html = saved_assessments_match.group(0) if saved_assessments_match else ""
        check(
            "start fresh action renders in guided header",
            response.status_code == 200
            and 'data-start-fresh-assessment' in header_html
            and 'value="start_fresh_assessment"' in header_html
            and "Start Fresh Assessment" in header_html
            and "workspace-action--primary" in header_html
            and "setup-button--danger" not in header_html
            and 'value="start_fresh_assessment"' not in saved_assessments_html,
        )

        client.post(
            "/",
            data={"action": "select_pricelist", "price_list_file": price_file},
        )
        response = client.post(
            "/",
            data={
                "action": "create_manual_inventory",
                "inventory_mode": "manual",
                "manual_vm_count": "2",
                "manual_total_vcpus": "8",
                "manual_total_memory_gb": "32",
                "manual_total_storage_gb": "400",
                "manual_supported_vm_count": "2",
                "manual_unsupported_vm_count": "0",
            },
        )
        with client.session_transaction() as sess:
            source_inventory_file = str(sess.get("selected_rvtools_file", ""))
        source_state = app_module.load_app_state()
        check(
            "start fresh source inventory created",
            response.status_code == 200
            and "/manual/" in source_inventory_file.replace("\\", "/")
            and source_state.get("selected_vm_names") == ["manual-vm-001", "manual-vm-002"],
            f"file={source_inventory_file}, state={source_state}",
        )

        response = client.post(
            "/",
            data={
                "action": "save_assessment",
                "customer_name": "Reset Source Customer",
                "assessment_notes": "This saved assessment should stay available.",
            },
        )
        check("start fresh source assessment saved", response.status_code == 200 and b"Assessment saved." in response.data)

        saved_assessments = app_module.list_saved_assessments()
        saved_assessment = next(
            assessment
            for assessment in saved_assessments
            if assessment.get("customer_name") == "Reset Source Customer"
        )
        saved_assessment_id = str(saved_assessment["id"])

        state = app_module.load_app_state()
        state["selected_vm_names"] = ["manual-vm-001", "manual-vm-002"]
        state["step4_ocvs_commitment_term"] = "3_year"
        state["step4_iaas_discount_pct"] = 22.0
        state["step4_hybrid_placements"] = {"manual-vm-001": "native"}
        state["acknowledged_warning_ids"] = ["unsupported-native"]
        state["assessor_recommendation"] = "hybrid"
        state["assessor_recommendation_rationale"] = "Use mixed placement."
        app_module.save_app_state(state)
        app_module.save_step4_snapshot({"active_scenario": "hybrid", "rows": ["manual-vm-001"]})
        with client.session_transaction() as sess:
            sess["last_export_file"] = "downloads/exports/reset_source.xlsx"
            sess["rvtools_rejected_info"] = {"file_name": "bad.xlsx"}

        response = client.post("/", data={"action": "start_fresh_assessment"})
        response_html = response.data.decode("utf-8", errors="ignore")
        check(
            "start fresh clears guided workspace",
            response.status_code == 200
            and "Started a fresh assessment" in response_html
            and "Start Fresh Assessment" in response_html,
        )

        with client.session_transaction() as sess:
            selected_price_file = str(sess.get("selected_pricelist_file", ""))
            selected_currency = str(sess.get("selected_currency", ""))
            cleared_customer = str(sess.get("customer_name", ""))
            cleared_inventory = str(sess.get("selected_rvtools_file", ""))
            cleared_assessment_name = str(sess.get("active_assessment_name", ""))
            cleared_assessment_notes = str(sess.get("active_assessment_notes", ""))
            cleared_last_export = str(sess.get("last_export_file", ""))
            rejected_info = sess.get("rvtools_rejected_info")

        reset_state = app_module.load_app_state()
        check("start fresh preserves price list", selected_price_file == price_file, selected_price_file)
        check("start fresh preserves currency", selected_currency == expected_currency, selected_currency)
        check(
            "start fresh clears active session",
            not cleared_customer
            and not cleared_inventory
            and not cleared_assessment_name
            and not cleared_assessment_notes
            and not cleared_last_export
            and rejected_info is None,
        )
        check(
            "start fresh resets guided app state",
            reset_state.get("selected_vm_names") == []
            and reset_state.get("step4_ocvs_commitment_term") == "payg"
            and reset_state.get("step4_iaas_discount_pct") == 0.0
            and reset_state.get("step4_hybrid_placements") == {}
            and reset_state.get("acknowledged_warning_ids") == []
            and reset_state.get("assessor_recommendation") == ""
            and reset_state.get("assessor_recommendation_rationale") == "",
            str(reset_state),
        )
        check("start fresh clears guided step4 snapshot", app_module.load_step4_snapshot() == {})
        check(
            "start fresh keeps saved assessment",
            any(assessment.get("id") == saved_assessment_id for assessment in app_module.list_saved_assessments()),
        )
        app_module.delete_saved_assessment(saved_assessment_id)
        app_module.save_preferences({})


def validate_portable_assessments() -> None:
    fixture_id = uuid4().hex[:8]
    portable_name = f"Portable Task 10 {fixture_id}"
    portable_notes = "Portable customer review with retained sizing decisions."
    portable_customer = f"Portable Customer {fixture_id}"
    source_inventory = app_module.RVTOOLS_DIR / f"portable_source_{fixture_id}.csv"
    source_pricing = app_module.DOWNLOADS_DIR / f"oci_pricing_EUR_portable_{fixture_id}.json"
    source_inventory.write_bytes(CSV_INVENTORY.read_bytes())
    source_pricing.write_bytes(Path(find_price_file()).read_bytes())
    inventory_rows, inventory_source = app_module.load_vms_from_vinfo(
        str(source_inventory)
    )
    selected_names = ["vm-app-01", "vm-db-01", "vm-legacy-01"]
    placements = {
        "vm-app-01": "native",
        "vm-db-01": "native",
        "vm-legacy-01": "ocvs",
    }
    rationale = "Keep the legacy workload on OCVS while modernizing the application tier."
    state_id = f"portable_state_{fixture_id}"
    state = app_module._default_app_state()
    state.update(
        selected_vm_names=selected_names,
        step4_hybrid_placements=placements,
        step4_iaas_discount_pct=17.5,
        step4_ocvs_commitment_term="3_year",
        acknowledged_warning_ids=["unsupported-native"],
        assessor_recommendation="hybrid",
        assessor_recommendation_rationale=rationale,
    )
    step4_snapshot = {
        "saved_at": "2026-07-04T12:00:00",
        "source_vinfo_csv": str(source_inventory).replace("\\", "/"),
        "vm_settings": {
            "vm-app-01": {
                "selected": True,
                "oci_shape": "VM.Standard.E5.Flex",
                "ocpu": 2,
                "burst": "100%",
                "vpu": 20,
                "os_license": "BYOL",
                "hybrid_placement": "native",
            },
            "vm-db-01": {
                "selected": True,
                "oci_shape": "VM.Standard.E5.Flex",
                "ocpu": 4,
                "burst": "50%",
                "vpu": 30,
                "os_license": "BYOL",
                "hybrid_placement": "native",
            },
        },
        "ocvs_commitment_term": "3_year",
    }
    with app_module.app.test_request_context("/"):
        app_module.session["state_id"] = state_id
        app_module.save_app_state(state)
        app_module.save_step4_snapshot(step4_snapshot)

    saved_dir = app_module.APP_STATE_DIR / "saved_assessments"

    def file_tree_bytes(root: Path) -> dict[str, bytes]:
        if not root.exists():
            return {}
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    with app_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_app_instance_id"] = app_module.APP_INSTANCE_ID
            sess["state_id"] = state_id
            sess["selected_rvtools_file"] = str(source_inventory).replace("\\", "/")
            sess["rvtools_file_info"] = app_module.build_source_file_info(
                source_inventory
            )
            sess["rvtools_import_summary"] = app_module.build_inventory_import_summary(
                inventory_rows,
                inventory_source,
            )
            sess["selected_pricelist_file"] = str(source_pricing).replace("\\", "/")
            sess["selected_currency"] = "EUR"
            sess["customer_name"] = portable_customer
            sess["active_assessment_name"] = portable_name
            sess["active_assessment_notes"] = portable_notes

        library_before_export = file_tree_bytes(saved_dir)
        export_response = client.post(
            "/",
            data={
                "action": "export_assessment",
                "assessment_name": portable_name,
                "assessment_notes": portable_notes,
            },
        )
        package_bytes = bytes(export_response.data)
        package = json.loads(package_bytes.decode("utf-8"))
        check(
            "Task 10 unsaved assessment exports deterministic portable JSON",
            export_response.status_code == 200
            and export_response.mimetype == "application/json"
            and "attachment" in export_response.headers.get("Content-Disposition", "")
            and package.get("package_type") == "vmware_to_oci_assessment"
            and package.get("schema_version") == 1
            and len(package.get("inventory", {}).get("rows", [])) == EXPECTED_VM_COUNT
            and bool(package.get("pricing", {}).get("document", {}).get("items"))
            and package.get("assessment", {}).get("app_state", {}).get(
                "selected_vm_names"
            )
            == selected_names
            and "source_vinfo_csv" not in json.dumps(package)
            and file_tree_bytes(saved_dir) == library_before_export,
            export_response.headers.get("Content-Disposition", ""),
        )

        source_inventory.unlink()
        source_pricing.unlink()
        with client.session_transaction() as sess:
            sess["active_assessment_name"] = "Prior local assessment"
            sess["active_assessment_notes"] = "Must be replaced only after success."
            sess["customer_name"] = "Prior customer"
        prior_state = app_module._default_app_state()
        prior_state["selected_vm_names"] = ["prior-vm"]
        with app_module.app.test_request_context("/"):
            app_module.session["state_id"] = state_id
            app_module.save_app_state(prior_state)
            app_module.save_step4_snapshot({"marker": "prior-step4"})

        import_response = client.post(
            "/assessment/import",
            data={
                "action": "import_assessment",
                "assessment_file": (BytesIO(package_bytes), "portable_assessment.json"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        with client.session_transaction() as sess:
            imported_session = dict(sess)
        imported_id = str(imported_session.get("active_assessment_id", ""))
        imported_inventory = str(imported_session.get("selected_rvtools_file", ""))
        imported_pricing = str(imported_session.get("selected_pricelist_file", ""))
        imported_state_path = app_module.APP_STATE_DIR / f"{state_id}.json"
        imported_step4_path = app_module.APP_STATE_DIR / f"{state_id}_step4_snapshot.json"
        imported_state = json.loads(imported_state_path.read_text(encoding="utf-8"))
        imported_step4 = json.loads(imported_step4_path.read_text(encoding="utf-8"))
        restored_rows, _ = app_module.load_vms_from_vinfo(imported_inventory)
        generated_inventory = json.loads(
            Path(imported_inventory).read_text(encoding="utf-8")
        )
        restored_prices, restored_currency, _ = app_module.load_price_lookup(
            imported_pricing
        )
        check(
            "Task 10 import restores a self-contained assessment after source deletion",
            import_response.status_code == 200
            and b"Assessment imported" in import_response.data
            and bool(imported_id)
            and imported_session.get("active_assessment_name") == portable_name
            and imported_session.get("active_assessment_notes") == portable_notes
            and imported_session.get("customer_name") == portable_customer
            and imported_session.get("selected_currency") == "EUR"
            and Path(imported_inventory).is_file()
            and Path(imported_pricing).is_file()
            and f"imported_assessments/{imported_id}" in imported_inventory.replace(
                "\\", "/"
            )
            and restored_rows == package.get("inventory", {}).get("rows")
            and generated_inventory.get("inventory") == package.get("inventory")
            and bool(restored_prices)
            and restored_currency == "EUR"
            and imported_state.get("selected_vm_names") == selected_names
            and imported_state.get("step4_hybrid_placements") == placements
            and imported_state.get("step4_iaas_discount_pct") == 17.5
            and imported_state.get("step4_ocvs_commitment_term") == "3_year"
            and imported_state.get("acknowledged_warning_ids")
            == ["unsupported-native"]
            and imported_state.get("assessor_recommendation") == "hybrid"
            and imported_state.get("assessor_recommendation_rationale") == rationale
            and imported_step4.get("source_vinfo_csv") == imported_inventory
            and imported_step4.get("vm_settings") == step4_snapshot["vm_settings"],
            f"session={imported_session}, state={imported_state}, step4={imported_step4}",
        )

        shell_response = client.get("/")
        shell = parse_workspace_markup(shell_response.data)
        results_response = client.get("/step4?tab=price")
        check(
            "Task 10 Results portability controls remain enabled without global header menu",
            shell_response.status_code == 200
            and shell.assessment_export is None
            and shell.assessment_import is None
            and b"data-assessment-menu" not in shell_response.data
            and b'name="assessment_file"' not in shell_response.data
            and b'action="/assessment/import"' not in shell_response.data
            and results_response.status_code == 200
            and b'value="export_assessment"' in results_response.data
            and b'name="assessment_file"' in results_response.data
            and b'action="/assessment/import"' in results_response.data,
            f"results_status={results_response.status_code}",
        )

        second_response = client.post(
            "/assessment/import",
            data={
                "action": "import_assessment",
                "assessment_file": (BytesIO(package_bytes), "portable_assessment.json"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        with client.session_transaction() as sess:
            second_name = str(sess.get("active_assessment_name", ""))
            second_id = str(sess.get("active_assessment_id", ""))
        check(
            "Task 10 duplicate import gets deterministic suffix and fresh id",
            second_response.status_code == 200
            and second_name == f"{portable_name} (Imported 2)"
            and second_id
            and second_id != imported_id,
            f"name={second_name}, first={imported_id}, second={second_id}",
        )

        with client.session_transaction() as sess:
            preserved_session = json.loads(json.dumps(dict(sess)))
        preserved_app_state = file_tree_bytes(app_module.APP_STATE_DIR)
        preserved_library = file_tree_bytes(saved_dir)
        imported_root = app_module.DOWNLOADS_DIR / "imported_assessments"
        preserved_artifacts = file_tree_bytes(imported_root)
        preserved_workbooks = file_tree_bytes(app_module.EXPORTS_DIR)
        invalid_wrong_type = json.loads(package_bytes.decode("utf-8"))
        invalid_wrong_type["package_type"] = "wrong"
        invalid_version = json.loads(package_bytes.decode("utf-8"))
        invalid_version["schema_version"] = 2
        invalid_missing = json.loads(package_bytes.decode("utf-8"))
        invalid_missing.pop("pricing")
        invalid_duplicate = json.loads(package_bytes.decode("utf-8"))
        invalid_duplicate["inventory"]["rows"].append(
            dict(invalid_duplicate["inventory"]["rows"][0])
        )
        invalid_negative = json.loads(package_bytes.decode("utf-8"))
        invalid_negative["inventory"]["rows"][0]["cpus"] = -1
        invalid_cases = [
            ("wrong extension", package_bytes, "portable.txt"),
            ("malformed JSON", b"{not-json", "portable.json"),
            ("wrong package type", json.dumps(invalid_wrong_type).encode("utf-8"), "portable.json"),
            ("unsupported version", json.dumps(invalid_version).encode("utf-8"), "portable.json"),
            ("missing section", json.dumps(invalid_missing).encode("utf-8"), "portable.json"),
            ("duplicate VM name", json.dumps(invalid_duplicate).encode("utf-8"), "portable.json"),
            ("negative number", json.dumps(invalid_negative).encode("utf-8"), "portable.json"),
            (
                "oversized package",
                b"{" + (b" " * app_module.MAX_PACKAGE_BYTES),
                "portable.json",
            ),
        ]
        invalid_results: list[tuple[str, bool]] = []
        for label, invalid_bytes, filename in invalid_cases:
            response = client.post(
                "/assessment/import",
                data={
                    "action": "import_assessment",
                    "assessment_file": (BytesIO(invalid_bytes), filename),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            with client.session_transaction() as sess:
                current_session = json.loads(json.dumps(dict(sess)))
            invalid_results.append(
                (
                    label,
                    response.status_code == 200
                    and current_session == preserved_session
                    and file_tree_bytes(app_module.APP_STATE_DIR) == preserved_app_state
                    and file_tree_bytes(saved_dir) == preserved_library
                    and file_tree_bytes(imported_root) == preserved_artifacts
                    and file_tree_bytes(app_module.EXPORTS_DIR) == preserved_workbooks,
                )
            )
        check(
            "Task 10 invalid imports never mutate local assessment data",
            all(passed for _label, passed in invalid_results),
            str(invalid_results),
        )


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
            is_results_route = route == "/step5"
            if is_results_route:
                check(
                    f"{route} controls render",
                    b"Save decision" in response.data
                    and b"Save assessment" in response.data
                    and re.search(
                        rb'<button type="submit" class="results-button">\s*Export Excel\s*</button>',
                        response.data,
                    )
                    is not None,
                )
                check(
                    f"{route} export status UI renders",
                    b'data-customer-ready-export="false"' in response.data,
                )
            else:
                check(
                    f"{route} controls render",
                    b"Save Settings" in response.data
                    and b"Export to Excel" not in response.data
                    and b"Export Excel" not in response.data,
                )
                check(
                    f"{route} has no scenario export status UI",
                    b'id="export-status"' not in response.data
                    and b"Excel export created:" not in response.data
                    and b"Open file" not in response.data,
                )
            check(f"{route} has no JSON export", b"Export to JSON" not in response.data and b"Export to Json" not in response.data)
            response_text = response.data.decode("utf-8", errors="ignore")
            cost_pattern = (
                r"<dt>Cost per VM</dt>\s*<dd>\s*([^<]+?)\s*</dd>"
                if is_results_route
                else r"<span>Cost / VM / month</span>\s*<strong[^>]*>\s*([^<]+?)\s*</strong>"
            )
            scenario_cost_per_vm_values = [
                float(re.sub(r"[^0-9.]", "", value) or 0)
                for value in re.findall(cost_pattern, response_text)
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

        literal_rationale = "=1+1; preserve this assessor rationale as literal text."
        recommendation_response = client.post(
            "/step4?tab=price",
            data={
                "action": "save_recommendation",
                "recommendation": "hybrid",
                "recommendation_rationale": literal_rationale,
            },
            follow_redirects=False,
        )
        check(
            "workbook recommendation fixture saved",
            recommendation_response.status_code == 303,
            str(recommendation_response.status_code),
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
        check(
            "workbook XML has no automatic migration recommendation labels",
            "Recommended Migration Path" not in raw_xml
            and "<t>Recommended Path</t>" not in raw_xml,
        )

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
        check(
            "executive summary includes draft readiness and specialist recommendation",
            all(
                token in sheet_data["Executive Summary"][0]
                for token in (
                    "Executive Summary - Draft",
                    "Workbook Status",
                    "Draft",
                    "Assessment Readiness",
                    "Draft results available",
                    "Specialist Recommendation",
                    "Hybrid",
                    "Internal Notes",
                    "=1+1; preserve this assessor rationale as literal text.",
                )
            ),
            sheet_data["Executive Summary"][0],
        )
        executive_xml = zf.read(sheet_map["Executive Summary"]).decode("utf-8")
        check(
            "specialist recommendation text stays literal in workbook XML",
            "<t>=1+1; preserve this assessor rationale as literal text.</t>"
            in executive_xml
            and "<f>=1+1; preserve this assessor rationale as literal text.</f>"
            not in executive_xml,
        )
        executive_rows = sheet_text_rows(zf, sheet_map["Executive Summary"])
        decision_rows = []
        decision_start = next(
            (
                index
                for index, row in enumerate(executive_rows)
                if row and row[0] == "Decision Readout"
            ),
            -1,
        )
        decision_end = next(
            (
                index
                for index, row in enumerate(executive_rows)
                if index > decision_start
                and row
                and row[0] == "Migration Path Options"
            ),
            len(executive_rows),
        )
        if decision_start >= 0:
            decision_rows = executive_rows[decision_start + 1 : decision_end]
        check(
            "executive decision readout uses assessor choice and readiness-derived price label",
            any(row[:2] == ["Specialist Decision", "Hybrid"] for row in decision_rows)
            and any(
                row and row[0] == "Lowest complete modeled price"
                for row in decision_rows
            ),
            str(decision_rows),
        )
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
    validate_task12_accessibility_and_responsive_contracts()
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
    validate_workbook_readiness_metadata_safety()
    validate_current_readiness_routes()
    validate_stage1_setup_redesign()
    validate_stage1_identity_save_and_loaded_manual_mode()
    validate_manual_sizing_input()
    validate_app_state_review_inputs()
    validate_saved_assessments()
    validate_start_fresh_assessment()
    validate_portable_assessments()
    validate_step3_duplicate_removal()
    validate_guided_inventory_review()
    validate_inventory_review_transactions_and_step4_boundary()
    validate_large_inventory_review_containment()
    validate_task7_native_scenario_workspace()
    validate_task8_ocvs_hybrid_configuration()
    workbook_path, workflow_state = run_workflow_and_export()
    validate_pricing_invariants(workflow_state)
    validate_workbook(workbook_path)
    validate_price_list_dropdown_policy()
    print(f"REGRESSION_OK workbook={workbook_path}")


if __name__ == "__main__":
    main()
