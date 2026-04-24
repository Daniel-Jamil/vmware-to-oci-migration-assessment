from __future__ import annotations

import json
import os
import csv
import math
import io
import zipfile
import ssl
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import time

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for


app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me-in-production"

OCI_PRODUCTS_API_BASE = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"
DOWNLOADS_DIR = Path("downloads")
RVTOOLS_DIR = Path("rvtools")

# Common currencies supported by OCI pricing API.
SUPPORTED_CURRENCIES = [
    "USD",
    "EUR",
    "GBP",
    "AUD",
    "CAD",
    "JPY",
    "SGD",
    "CHF",
    "SEK",
    "NOK",
    "DKK",
]

SUPPORTED_RVTOOLS_EXTENSIONS = {".xlsx", ".csv"}
OS_MAPPING_CONFIG_PATH = Path("config/os_mapping.json")
OCI_SUPPORTED_OS_PATH = Path("OCI-SupportedOS.txt")
OCI_PRICE_MAPPING_PATH = Path("OCI-PriceMapping")
APP_STATE_DIR = Path("downloads/app_state")



def _cleanup_legacy_session_keys() -> None:
    """Remove large legacy client-side session keys (cookie bloat guard)."""
    session.pop("selected_vm_names", None)
    session.pop("step4_os_shapes", None)


def _default_app_state() -> dict[str, Any]:
    return {
        "selected_vm_names": [],
        "step4_os_shapes": {},
        "step4_vm_shapes": {},
        "step4_vm_ocpus": {},
        "step4_vm_bursts": {},
        "step4_vm_vpus": {},
        "step4_vm_os_license": {},
        "step4_iaas_discount_pct": 0.0,
    }


def _state_file_path() -> Path:
    state_id = str(session.get("state_id", "")).strip()
    if not state_id:
        state_id = uuid4().hex
        session["state_id"] = state_id

    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return APP_STATE_DIR / f"{state_id}.json"


def _step4_snapshot_file_path() -> Path:
    """Per-session persistent Step 4 snapshot file path."""
    state_id = str(session.get("state_id", "")).strip()
    if not state_id:
        state_id = uuid4().hex
        session["state_id"] = state_id

    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return APP_STATE_DIR / f"{state_id}_step4_snapshot.json"


def load_step4_snapshot() -> dict[str, Any]:
    snapshot_file = _step4_snapshot_file_path()
    if not snapshot_file.exists():
        return {}
    try:
        loaded = json.loads(snapshot_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_step4_snapshot(snapshot: dict[str, Any]) -> None:
    snapshot_file = _step4_snapshot_file_path()
    snapshot_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")


def clear_step4_snapshot() -> None:
    """Remove Step 4 snapshot for current session if it exists."""
    snapshot_file = _step4_snapshot_file_path()
    try:
        if snapshot_file.exists():
            snapshot_file.unlink()
    except Exception:
        pass


def load_app_state() -> dict[str, Any]:
    """Load server-side app state so large selections don't live in cookies."""
    state_file = _state_file_path()
    if not state_file.exists():
        return _default_app_state()

    try:
        loaded = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return _default_app_state()

    if not isinstance(loaded, dict):
        return _default_app_state()

    default = _default_app_state()
    default.update(loaded)
    if not isinstance(default.get("selected_vm_names"), list):
        default["selected_vm_names"] = []
    if not isinstance(default.get("step4_os_shapes"), dict):
        default["step4_os_shapes"] = {}
    if not isinstance(default.get("step4_vm_shapes"), dict):
        default["step4_vm_shapes"] = {}
    if not isinstance(default.get("step4_vm_ocpus"), dict):
        default["step4_vm_ocpus"] = {}
    if not isinstance(default.get("step4_vm_bursts"), dict):
        default["step4_vm_bursts"] = {}
    if not isinstance(default.get("step4_vm_vpus"), dict):
        default["step4_vm_vpus"] = {}
    if not isinstance(default.get("step4_vm_os_license"), dict):
        default["step4_vm_os_license"] = {}
    try:
        discount_value = float(default.get("step4_iaas_discount_pct", 0.0))
    except (TypeError, ValueError):
        discount_value = 0.0
    default["step4_iaas_discount_pct"] = max(0.0, min(100.0, discount_value))
    return default


def save_app_state(state: dict[str, Any]) -> None:
    state_file = _state_file_path()
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_supported_os_signatures() -> list[str]:
    """Load OCI supported OS names from text file as lowercase match signatures."""
    if not OCI_SUPPORTED_OS_PATH.exists():
        return []

    signatures: list[str] = []
    for line in OCI_SUPPORTED_OS_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        clean = line.strip().lower()
        if clean:
            signatures.append(clean)
    return signatures


def is_oci_supported_os(raw_os: str, supported_signatures: list[str]) -> bool:
    """Return True when OS is OCI-supported and not 32-bit."""
    value = (raw_os or "").strip().lower()
    if not value:
        return False
    if "32-bit" in value:
        return False
    return any(sig in value for sig in supported_signatures)


def load_oci_target_shapes() -> list[str]:
    """Load OCI target shapes from OCI-PriceMapping CSV (first field per row)."""
    fallback = [
        "VM.Standard3.Flex (Intel)",
        "VM.Standard.E4.Flex (AMD)",
        "VM.Standard.E5.Flex (AMD)",
        "VM.Standard.E6.Flex (AMD)",
    ]

    if not OCI_PRICE_MAPPING_PATH.exists():
        return fallback

    shapes: list[str] = []
    try:
        with OCI_PRICE_MAPPING_PATH.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                shape = str(row[0]).strip()
                if shape:
                    shapes.append(shape)
    except Exception:
        return fallback

    deduped = list(dict.fromkeys(shapes))
    return deduped if deduped else fallback


def load_oci_price_mapping_details() -> dict[str, dict[str, str]]:
    """Load shape -> pricing display names from OCI-PriceMapping CSV."""
    mapping: dict[str, dict[str, str]] = {}
    if not OCI_PRICE_MAPPING_PATH.exists():
        return mapping

    try:
        with OCI_PRICE_MAPPING_PATH.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 3:
                    continue
                shape_name = str(row[0]).strip()
                ocpu_display = str(row[1]).strip()
                memory_display = str(row[2]).strip()
                if shape_name:
                    mapping[shape_name] = {
                        "ocpu_display_name": ocpu_display,
                        "memory_display_name": memory_display,
                    }
    except Exception:
        return {}

    return mapping


def load_latest_price_lookup() -> tuple[dict[str, float], str, str]:
    """Load latest saved OCI price file and return displayName->unit price lookup."""
    return load_price_lookup(None)


def list_downloaded_price_lists() -> list[str]:
    """List saved OCI price list JSON files (newest first)."""
    files = list(DOWNLOADS_DIR.glob("oci_pricing_*.json"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p).replace("\\", "/") for p in files]


def load_price_lookup(preferred_file: str | None = None) -> tuple[dict[str, float], str, str]:
    """Load OCI price lookup from a preferred file, falling back to latest."""
    candidate: Path | None = None

    preferred = str(preferred_file or "").strip()
    if preferred:
        preferred_path = Path(preferred)
        if preferred_path.exists() and preferred_path.is_file():
            candidate = preferred_path

    files = list(DOWNLOADS_DIR.glob("oci_pricing_*.json"))
    if candidate is None:
        if not files:
            return {}, "", ""
        candidate = max(files, key=lambda p: p.stat().st_mtime)

    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        return {}, "", ""

    items = payload.get("items", [])
    if not isinstance(items, list):
        return {}, "", ""

    lookup: dict[str, float] = {}
    currency = ""

    for item in items:
        if not isinstance(item, dict):
            continue
        display_name = str(item.get("displayName", "")).strip()
        if not display_name:
            continue

        localizations = item.get("currencyCodeLocalizations", [])
        if not isinstance(localizations, list) or not localizations:
            continue

        chosen = None
        for loc in localizations:
            if not isinstance(loc, dict):
                continue
            prices = loc.get("prices", [])
            if not isinstance(prices, list):
                continue
            payg = next((p for p in prices if isinstance(p, dict) and str(p.get("model", "")).upper() == "PAY_AS_YOU_GO"), None)
            if payg is None:
                payg = next((p for p in prices if isinstance(p, dict)), None)
            if payg is not None:
                chosen = (loc, payg)
                break

        if chosen is None:
            continue

        loc, price = chosen
        try:
            unit_price = float(price.get("value", 0.0))
        except (TypeError, ValueError):
            continue

        lookup[display_name] = unit_price
        if not currency:
            currency = str(loc.get("currencyCode", "")).strip()

    return lookup, currency, str(candidate).replace("\\", "/")


def fetch_oci_price_list(currency_code: str) -> dict[str, Any]:
    """Fetch OCI list pricing from Oracle CE tools API for a specific currency."""
    params = urlencode({"currencyCode": currency_code})
    url = f"{OCI_PRODUCTS_API_BASE}?{params}"

    # Some enterprise networks/proxies reject requests with the default Python user-agent.
    # Use browser-like headers and retry transient network failures.
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) vmw2oci/1.0",
            "Accept": "application/json",
        },
    )

    ssl_contexts: list[ssl.SSLContext] = [ssl.create_default_context()]

    # If certifi is available, prefer its CA bundle as an additional fallback.
    try:  # pragma: no cover - optional dependency path
        import certifi  # type: ignore

        certifi_ctx = ssl.create_default_context(cafile=certifi.where())
        ssl_contexts.append(certifi_ctx)
    except Exception:
        pass

    last_network_exc: Exception | None = None
    for ctx in ssl_contexts:
        for attempt in range(1, 4):
            try:
                with urlopen(req, timeout=60, context=ctx) as response:
                    body = response.read().decode("utf-8")
                break
            except (URLError, TimeoutError, ConnectionResetError) as exc:
                last_network_exc = exc
                if attempt == 3:
                    # try next SSL context before giving up
                    pass
                else:
                    time.sleep(attempt)
                    continue
        else:
            continue
        break
    else:  # pragma: no cover - defensive fallback
        if last_network_exc:
            raise last_network_exc
        raise URLError("Unknown network error while contacting Oracle API")

    data = json.loads(body)
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("Unexpected response format received from OCI pricing API.")
    return data


def filter_compute_vm_items(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only needed OCI pricing items for Step 1 and downstream costing."""
    items = payload.get("items", [])
    if not isinstance(items, list):
        return payload

    allowed_categories = {
        "Compute - Virtual Machine",
        "Storage - Block Volumes",
    }

    filtered_items = [
        item
        for item in items
        if isinstance(item, dict)
        and (
            str(item.get("serviceCategory", "")).strip() in allowed_categories
            or str(item.get("displayName", "")).strip() == "Compute - Windows OS"
        )
    ]

    filtered_payload = dict(payload)
    filtered_payload["items"] = filtered_items
    return filtered_payload


def save_price_list(currency_code: str, payload: dict[str, Any]) -> Path:
    """Persist downloaded price list JSON locally and return the file path."""
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = DOWNLOADS_DIR / f"oci_pricing_{currency_code}_{timestamp}.json"
    file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return file_path


def list_rvtools_export_files() -> list[str]:
    """List RVTools export files recursively under rvtools directory."""
    if not RVTOOLS_DIR.exists():
        return []

    files: list[str] = []
    for root, _, filenames in os.walk(RVTOOLS_DIR):
        root_path = Path(root)
        for name in filenames:
            file_path = root_path / name
            if file_path.suffix.lower() in SUPPORTED_RVTOOLS_EXTENSIONS:
                files.append(str(file_path).replace("\\", "/"))
    return sorted(files)


def load_os_mapping_config() -> dict[str, Any]:
    """Load OS mapping rules from config file, creating a default if absent."""
    default_config: dict[str, Any] = {
        "default": "Unmapped / Review",
        "rules": [
            {"contains": "windows server 2022", "mapped": "Windows Server 2022"},
            {"contains": "windows server 2019", "mapped": "Windows Server 2019"},
            {"contains": "windows", "mapped": "Windows"},
            {"contains": "ubuntu", "mapped": "Linux - Ubuntu"},
            {"contains": "rocky", "mapped": "Linux - Rocky"},
            {"contains": "linux", "mapped": "Linux"},
            {"contains": "freebsd", "mapped": "FreeBSD"},
        ],
    }

    if not OS_MAPPING_CONFIG_PATH.exists():
        OS_MAPPING_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        OS_MAPPING_CONFIG_PATH.write_text(
            json.dumps(default_config, indent=2),
            encoding="utf-8",
        )
        return default_config

    loaded = json.loads(OS_MAPPING_CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return default_config
    return loaded


def map_os_name(raw_os: str, mapping_config: dict[str, Any]) -> str:
    os_value = (raw_os or "").strip().lower()
    rules = mapping_config.get("rules", [])
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        contains = str(rule.get("contains", "")).lower().strip()
        mapped = str(rule.get("mapped", "")).strip()
        if contains and contains in os_value and mapped:
            return mapped
    return str(mapping_config.get("default", "Unmapped / Review"))


def resolve_vinfo_csv(selected_path: str) -> Path:
    """Resolve RVTools vInfo CSV path strictly from selected artifact context."""
    selected = Path(selected_path)
    # RVTools names the vInfo file RVTools_tabvInfo.csv; also allow prefixed names
    # (e.g. example_RVTools_tabvInfo.csv) when the file is a direct CSV selection.
    n = selected.name.lower()
    if selected.is_file() and n.endswith("rvtools_tabvinfo.csv"):
        return selected

    # If an export archive was selected, try extracted folder with same stem.
    if "RVTools_export_all_" in selected.name:
        base_name = selected.stem
        direct_candidate = RVTOOLS_DIR / base_name / "RVTools_tabvInfo.csv"
        nested_candidate = RVTOOLS_DIR / base_name / base_name / "RVTools_tabvInfo.csv"
        export_csv_candidate = RVTOOLS_DIR / "export_to_csv" / base_name / "RVTools_tabvInfo.csv"
        for candidate in (direct_candidate, nested_candidate, export_csv_candidate):
            if candidate.exists():
                return candidate

    raise FileNotFoundError(
        "Could not locate RVTools_tabvInfo.csv for the selected export. "
        "Please select a matching RVTools export file/folder."
    )


def _col_letters_to_index(col_letters: str) -> int:
    idx = 0
    for ch in col_letters:
        if "A" <= ch <= "Z":
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(idx - 1, 0)


def parse_vinfo_from_xlsx(xlsx_path: Path) -> list[dict[str, str]]:
    """Parse vInfo sheet from RVTools XLSX without external dependencies."""
    ns_main = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    offdoc_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    with zipfile.ZipFile(xlsx_path) as z:
        workbook_xml = ET.fromstring(z.read("xl/workbook.xml"))
        rels_xml = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rel_map: dict[str, str] = {
            rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
            for rel in rels_xml.findall(f"{{{rel_ns}}}Relationship")
        }

        vinfo_target: str | None = None
        sheets = workbook_xml.find("m:sheets", ns_main)
        if sheets is not None:
            for sheet in sheets:
                name = (sheet.attrib.get("name") or "").strip().lower()
                rel_id = sheet.attrib.get(f"{{{offdoc_rel_ns}}}id", "")
                if name == "vinfo":
                    vinfo_target = rel_map.get(rel_id)
                    break

        if not vinfo_target:
            raise ValueError("Could not find vInfo sheet in selected XLSX.")

        normalized_target = str(vinfo_target or "").replace("\\", "/").lstrip("/")
        if normalized_target.startswith("xl/"):
            sheet_xml_path = normalized_target
        else:
            sheet_xml_path = f"xl/{normalized_target}"
        sheet_xml = ET.fromstring(z.read(sheet_xml_path))

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst_xml = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sst_xml.findall("m:si", ns_main):
                text_parts = [t.text or "" for t in si.findall(".//m:t", ns_main)]
                shared_strings.append("".join(text_parts))

        def read_cell_value(cell: ET.Element) -> str:
            cell_type = cell.attrib.get("t", "")
            if cell_type == "inlineStr":
                t = cell.find("m:is/m:t", ns_main)
                return (t.text or "") if t is not None else ""

            v = cell.find("m:v", ns_main)
            raw = (v.text or "") if v is not None else ""
            if cell_type == "s" and raw.isdigit():
                idx = int(raw)
                if 0 <= idx < len(shared_strings):
                    return shared_strings[idx]
            return raw

        rows: list[dict[int, str]] = []
        sheet_data = sheet_xml.find("m:sheetData", ns_main)
        if sheet_data is None:
            return []

        for row in sheet_data.findall("m:row", ns_main):
            data: dict[int, str] = {}
            for cell in row.findall("m:c", ns_main):
                ref = cell.attrib.get("r", "")
                col_letters = "".join(ch for ch in ref if ch.isalpha()).upper()
                col_idx = _col_letters_to_index(col_letters) if col_letters else len(data)
                data[col_idx] = read_cell_value(cell).strip()
            rows.append(data)

        if not rows:
            return []

        headers_by_idx = rows[0]
        headers: dict[int, str] = {
            idx: value.strip() for idx, value in headers_by_idx.items() if value.strip()
        }

        parsed: list[dict[str, str]] = []
        for row in rows[1:]:
            record: dict[str, str] = {}
            for idx, header in headers.items():
                record[header] = row.get(idx, "")
            if any(v.strip() for v in record.values()):
                parsed.append(record)

        return parsed


def load_vms_from_vinfo(selected_path: str) -> tuple[list[dict[str, Any]], str]:
    """Load VM list from RVTools vInfo CSV and return rows + source CSV path."""
    selected = Path(selected_path)
    mapping_config = load_os_mapping_config()

    def _build_vm_rows(records: list[dict[str, str]]) -> list[dict[str, Any]]:
        def _first_value(rec: dict[str, str], *keys: str) -> str:
            for key in keys:
                val = rec.get(key)
                if val is not None and str(val).strip():
                    return str(val).strip()
            return ""

        def _to_short_power_state(raw_value: str) -> str:
            value = str(raw_value or "").strip().lower().replace(" ", "")
            if value in {"poweredon", "on", "running"}:
                return "On"
            return "Off"

        parsed_rows: list[dict[str, Any]] = []
        for rec in records:
            # Prefer VM name; if blank (some exports), use VM ID (RVTools column is often "VM ID").
            vm_name = _first_value(rec, "VM", "VM ID", "VM-ID", "VMID")
            if not vm_name:
                continue
            raw_os = (rec.get("OS according to the configuration file") or "").strip()
            power_state_raw = _first_value(rec, "Powerstate", "PowerState", "Power State")
            cpus_raw = (rec.get("CPUs") or "").strip()
            mem_raw = (rec.get("Memory") or "").strip()
            provisioned_mib_raw = _first_value(rec, "Provisioned MiB", "Provisioned MB")

            parsed_rows.append(
                {
                    "name": vm_name,
                    "power_state": _to_short_power_state(power_state_raw),
                    "raw_os": raw_os,
                    "mapped_os": map_os_name(raw_os, mapping_config),
                    "cpus": cpus_raw,
                    "memory_mb": mem_raw,
                    "provisioned_mib": provisioned_mib_raw,
                }
            )
        return parsed_rows

    # If a workbook was selected, parse vInfo directly from that workbook.
    if selected.suffix.lower() == ".xlsx":
        records = parse_vinfo_from_xlsx(selected)
        return _build_vm_rows(records), f"{str(selected).replace('\\', '/')}::vInfo"

    vinfo_csv = resolve_vinfo_csv(selected_path)

    records: list[dict[str, str]] = []
    last_exc: Exception | None = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with vinfo_csv.open("r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                records = [dict(r) for r in reader]
            break
        except UnicodeDecodeError as exc:
            last_exc = exc

    if not records and last_exc is not None:
        raise last_exc

    return _build_vm_rows(records), str(vinfo_csv).replace("\\", "/")


def format_total_memory_gb_or_tb(total_mb: int) -> str:
    """Format total RAM for Step 3: GB if under 1 TiB, otherwise TB (1024-based)."""
    total_mb = max(0, int(total_mb))
    total_gb = total_mb / 1024.0
    if total_gb < 1024.0:
        return f"{total_gb:,.1f} GB"
    return f"{total_gb / 1024.0:,.2f} TB"


@app.route("/", methods=["GET", "POST"])
def index() -> str:
    _cleanup_legacy_session_keys()

    download_info: dict[str, Any] | None = None
    selected_rvtools_file = str(session.get("selected_rvtools_file", ""))
    rvtools_file_info: dict[str, Any] | None = session.get("rvtools_file_info")
    selected_currency = "USD"
    rvtools_files = list_rvtools_export_files()
    downloaded_price_lists = list_downloaded_price_lists()
    selected_pricelist_file = str(session.get("selected_pricelist_file", "")).strip().replace("\\", "/")

    if selected_pricelist_file and selected_pricelist_file not in downloaded_price_lists:
        selected_pricelist_file = ""
        session.pop("selected_pricelist_file", None)
    if not selected_pricelist_file and downloaded_price_lists:
        selected_pricelist_file = downloaded_price_lists[0]
        session["selected_pricelist_file"] = selected_pricelist_file

    selected_pricelist_info: dict[str, Any] | None = None
    if selected_pricelist_file:
        price_lookup_preview, selected_pricing_currency, source_file = load_price_lookup(selected_pricelist_file)
        if source_file:
            selected_pricelist_info = {
                "file_path": source_file,
                "currency": selected_pricing_currency or "Unknown",
                "item_count": len(price_lookup_preview),
            }

    if request.method == "POST":
        action = request.form.get("action", "download_pricing")

        if action == "download_pricing":
            selected_currency = request.form.get("currency_code", "USD").upper().strip()

            if selected_currency not in SUPPORTED_CURRENCIES:
                flash("Please select a supported currency.", "error")
                return render_template(
                    "index.html",
                    currencies=SUPPORTED_CURRENCIES,
                    selected_currency=selected_currency,
                    download_info=download_info,
                    downloaded_price_lists=downloaded_price_lists,
                    selected_pricelist_file=selected_pricelist_file,
                    selected_pricelist_info=selected_pricelist_info,
                    rvtools_files=rvtools_files,
                    selected_rvtools_file=selected_rvtools_file,
                    rvtools_file_info=rvtools_file_info,
                )

            try:
                payload = fetch_oci_price_list(selected_currency)
                payload = filter_compute_vm_items(payload)
                saved_file = save_price_list(selected_currency, payload)
                item_count = len(payload.get("items", []))

                download_info = {
                    "currency": selected_currency,
                    "file_path": str(saved_file),
                    "last_updated": payload.get("lastUpdated", "Unknown"),
                    "item_count": item_count,
                }
                session["selected_pricelist_file"] = str(saved_file).replace("\\", "/")
                flash("OCI price list downloaded successfully.", "success")
            except HTTPError as exc:
                flash(
                    f"Oracle API returned an HTTP error ({exc.code}). Please try again.",
                    "error",
                )
            except URLError as exc:
                reason = getattr(exc, "reason", None)
                detail = f" ({reason})" if reason else ""
                guidance = ""
                if reason and "CERTIFICATE_VERIFY_FAILED" in str(reason):
                    guidance = " Please install/update trusted CA certificates (or certifi)."
                flash(f"Could not reach Oracle API. Check connectivity and try again{detail}.{guidance}", "error")
            except (TimeoutError, ValueError, json.JSONDecodeError) as exc:
                flash(f"Could not process OCI pricing response: {exc}", "error")
            except Exception as exc:  # pragma: no cover - fallback guard
                flash(f"Unexpected error: {exc}", "error")

        elif action == "select_rvtools_file":
            selected_rvtools_file = request.form.get("rvtools_file", "").strip().replace("\\", "/")
            if not selected_rvtools_file or selected_rvtools_file not in rvtools_files:
                flash("Please select a valid RVTools export file.", "error")
            else:
                p = Path(selected_rvtools_file)
                rvtools_file_info = {
                    "file_path": selected_rvtools_file,
                    "file_name": p.name,
                    "size_kb": round(p.stat().st_size / 1024, 2),
                }
                session["selected_rvtools_file"] = selected_rvtools_file
                session["rvtools_file_info"] = rvtools_file_info
                # Reset server-side selection state for a newly selected source file.
                save_app_state(_default_app_state())
                clear_step4_snapshot()
                flash("RVTools export file selected successfully.", "success")

        elif action == "select_pricelist":
            chosen_price_file = str(request.form.get("price_list_file", "")).strip().replace("\\", "/")
            if not chosen_price_file:
                flash("Please select an OCI price list file.", "error")
            else:
                refreshed_lists = list_downloaded_price_lists()
                if chosen_price_file not in refreshed_lists:
                    flash("Selected OCI price list file is not available anymore.", "error")
                else:
                    session["selected_pricelist_file"] = chosen_price_file
                    flash("OCI price list file selected.", "success")

        downloaded_price_lists = list_downloaded_price_lists()
        selected_pricelist_file = str(session.get("selected_pricelist_file", "")).strip().replace("\\", "/")
        if selected_pricelist_file and selected_pricelist_file not in downloaded_price_lists:
            selected_pricelist_file = ""
            session.pop("selected_pricelist_file", None)
        if not selected_pricelist_file and downloaded_price_lists:
            selected_pricelist_file = downloaded_price_lists[0]
            session["selected_pricelist_file"] = selected_pricelist_file

        selected_pricelist_info = None
        if selected_pricelist_file:
            price_lookup_preview, selected_pricing_currency, source_file = load_price_lookup(selected_pricelist_file)
            if source_file:
                selected_pricelist_info = {
                    "file_path": source_file,
                    "currency": selected_pricing_currency or "Unknown",
                    "item_count": len(price_lookup_preview),
                }

    return render_template(
        "index.html",
        currencies=SUPPORTED_CURRENCIES,
        selected_currency=selected_currency,
        download_info=download_info,
        downloaded_price_lists=downloaded_price_lists,
        selected_pricelist_file=selected_pricelist_file,
        selected_pricelist_info=selected_pricelist_info,
        rvtools_files=rvtools_files,
        selected_rvtools_file=selected_rvtools_file,
        rvtools_file_info=rvtools_file_info,
    )


@app.route("/step3", methods=["GET", "POST"])
def step3() -> str:
    _cleanup_legacy_session_keys()

    selected_rvtools_file = str(session.get("selected_rvtools_file", ""))
    if not selected_rvtools_file:
        flash("Please complete Step 2 and select an RVTools export file first.", "error")
        return redirect(url_for("index"))

    try:
        all_vms, source_vinfo_csv = load_vms_from_vinfo(selected_rvtools_file)
    except Exception as exc:
        flash(f"Could not load RVTools vInfo data: {exc}", "error")
        return redirect(url_for("index"))

    vm_index = {vm["name"]: vm for vm in all_vms}
    app_state = load_app_state()
    selected_vm_names = app_state.get("selected_vm_names", [])
    if not isinstance(selected_vm_names, list):
        selected_vm_names = []
    selected_vm_names = [n for n in selected_vm_names if n in vm_index]

    if request.method == "POST":
        action = request.form.get("action", "")
        chosen_vm_names = request.form.getlist("vm_names")
        single_vm_name = (request.form.get("vm_name") or "").strip()
        if single_vm_name:
            chosen_vm_names = [single_vm_name]

        if action == "add":
            merged = list(dict.fromkeys(selected_vm_names + [n for n in chosen_vm_names if n in vm_index]))
            selected_vm_names = merged
        elif action == "remove":
            selected_vm_names = [n for n in selected_vm_names if n not in set(chosen_vm_names)]
        elif action == "remove_unsupported":
            supported_signatures = load_supported_os_signatures()
            if not supported_signatures:
                flash(
                    "Could not remove unsupported OS images: OCI-SupportedOS.txt is missing or empty.",
                    "error",
                )
            else:
                before_count = len(selected_vm_names)
                selected_vm_names = [
                    n
                    for n in selected_vm_names
                    if n in vm_index and is_oci_supported_os(str(vm_index[n].get("raw_os", "")), supported_signatures)
                ]
                removed_count = before_count - len(selected_vm_names)
                flash(f"Removed {removed_count} non-OCI-supported or 32-bit VM image(s).", "success")

        app_state["selected_vm_names"] = selected_vm_names
        save_app_state(app_state)

    selected_set = set(selected_vm_names)
    available_vms_all = [vm for vm in all_vms if vm["name"] not in selected_set]
    selected_vms_all = [vm_index[name] for name in selected_vm_names if name in vm_index]

    available_os_filter = (request.values.get("available_os_filter") or "ALL").strip()
    selected_os_filter = (request.values.get("selected_os_filter") or "ALL").strip()
    available_power_filter = (request.values.get("available_power_filter") or "ALL").strip()
    selected_power_filter = (request.values.get("selected_power_filter") or "ALL").strip()

    available_os_options = sorted({(vm.get("raw_os") or "").strip() for vm in available_vms_all if (vm.get("raw_os") or "").strip()})
    selected_os_options = sorted({(vm.get("raw_os") or "").strip() for vm in selected_vms_all if (vm.get("raw_os") or "").strip()})
    available_power_options = sorted({(vm.get("power_state") or "").strip() for vm in available_vms_all if (vm.get("power_state") or "").strip()})
    selected_power_options = sorted({(vm.get("power_state") or "").strip() for vm in selected_vms_all if (vm.get("power_state") or "").strip()})

    # If a filter value no longer exists after add/remove actions, reset to ALL
    # so UI selection and displayed rows stay in sync.
    if available_os_filter != "ALL" and available_os_filter not in available_os_options:
        available_os_filter = "ALL"
    if selected_os_filter != "ALL" and selected_os_filter not in selected_os_options:
        selected_os_filter = "ALL"
    if available_power_filter != "ALL" and available_power_filter not in available_power_options:
        available_power_filter = "ALL"
    if selected_power_filter != "ALL" and selected_power_filter not in selected_power_options:
        selected_power_filter = "ALL"

    def _matches_filters(vm: dict[str, Any], os_filter: str, power_filter: str) -> bool:
        vm_os = (vm.get("raw_os") or "").strip()
        vm_power = (vm.get("power_state") or "").strip()
        if os_filter != "ALL" and vm_os != os_filter:
            return False
        if power_filter != "ALL" and vm_power != power_filter:
            return False
        return True

    available_vms = (
        [vm for vm in available_vms_all if _matches_filters(vm, available_os_filter, available_power_filter)]
    )
    selected_vms = (
        [vm for vm in selected_vms_all if _matches_filters(vm, selected_os_filter, selected_power_filter)]
    )

    def _to_number(value: Any) -> float:
        try:
            return float(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

    available_mem_mb = int(sum(_to_number(vm.get("memory_mb")) for vm in available_vms))
    selected_mem_mb = int(sum(_to_number(vm.get("memory_mb")) for vm in selected_vms))
    available_summary = {
        "total_vms": len(available_vms),
        "total_cpus": int(sum(_to_number(vm.get("cpus")) for vm in available_vms)),
        "total_memory_mb": available_mem_mb,
        "total_memory_display": format_total_memory_gb_or_tb(available_mem_mb),
    }
    selected_summary = {
        "total_vms": len(selected_vms),
        "total_cpus": int(sum(_to_number(vm.get("cpus")) for vm in selected_vms)),
        "total_memory_mb": selected_mem_mb,
        "total_memory_display": format_total_memory_gb_or_tb(selected_mem_mb),
    }

    return render_template(
        "step3.html",
        selected_rvtools_file=selected_rvtools_file,
        source_vinfo_csv=source_vinfo_csv,
        available_vms=available_vms,
        selected_vms=selected_vms,
        available_summary=available_summary,
        selected_summary=selected_summary,
        available_os_filter=available_os_filter,
        selected_os_filter=selected_os_filter,
        available_power_filter=available_power_filter,
        selected_power_filter=selected_power_filter,
        available_os_options=available_os_options,
        selected_os_options=selected_os_options,
        available_power_options=available_power_options,
        selected_power_options=selected_power_options,
    )


@app.route("/step4", methods=["GET", "POST"])
def step4() -> str:
    _cleanup_legacy_session_keys()

    selected_rvtools_file = str(session.get("selected_rvtools_file", ""))
    if not selected_rvtools_file:
        flash("Please complete Step 2 and select an RVTools export file first.", "error")
        return redirect(url_for("index"))

    try:
        all_vms, source_vinfo_csv = load_vms_from_vinfo(selected_rvtools_file)
    except Exception as exc:
        flash(f"Could not load RVTools vInfo data: {exc}", "error")
        return redirect(url_for("index"))

    vm_index = {vm["name"]: vm for vm in all_vms}
    app_state = load_app_state()
    selected_vm_names = app_state.get("selected_vm_names", [])
    if not isinstance(selected_vm_names, list):
        selected_vm_names = []

    shape_options = load_oci_target_shapes()
    shape_pricing_map = load_oci_price_mapping_details()
    if shape_pricing_map:
        shape_options = [s for s in shape_options if s in shape_pricing_map] or list(shape_pricing_map.keys())

    selected_pricelist_file = str(session.get("selected_pricelist_file", "")).strip().replace("\\", "/")
    price_lookup, pricing_currency, source_pricelist_file = load_price_lookup(selected_pricelist_file or None)
    shape_price_rates: dict[str, dict[str, float]] = {}
    for shape_name, mapping in shape_pricing_map.items():
        ocpu_display = str(mapping.get("ocpu_display_name", "")).strip()
        memory_display = str(mapping.get("memory_display_name", "")).strip()
        shape_price_rates[shape_name] = {
            "ocpu_unit_price": float(price_lookup.get(ocpu_display, 0.0)),
            "memory_unit_price": float(price_lookup.get(memory_display, 0.0)),
        }

    block_storage_unit_price = float(
        price_lookup.get(
            "Storage - Block Volume - Storage",
            next(
                (
                    float(v)
                    for k, v in price_lookup.items()
                    if "block volume" in str(k).lower() and "storage" in str(k).lower() and "free" not in str(k).lower()
                ),
                0.0,
            ),
        )
    )
    block_perf_unit_price = float(
        price_lookup.get(
            "Storage - Block Volume - Performance Units",
            next(
                (
                    float(v)
                    for k, v in price_lookup.items()
                    if "block volume" in str(k).lower() and "performance units" in str(k).lower()
                ),
                0.0,
            ),
        )
    )
    windows_os_unit_price = float(price_lookup.get("Compute - Windows OS", 0.0))

    valid_shape_values = set(shape_options)
    vpu_options = list(range(10, 121, 10))
    valid_vpu_values = set(vpu_options)
    valid_burst_values = {"100%", "50%", "12.5%", "1:1"}
    burst_factor_map = {
        "100%": 1.0,
        "1:1": 1.0,
        "50%": 0.5,
        "12.5%": 0.125,
    }
    vm_shape_selection = app_state.get("step4_vm_shapes", {})
    if not isinstance(vm_shape_selection, dict):
        vm_shape_selection = {}
    vm_ocpu_selection = app_state.get("step4_vm_ocpus", {})
    if not isinstance(vm_ocpu_selection, dict):
        vm_ocpu_selection = {}
    vm_burst_selection = app_state.get("step4_vm_bursts", {})
    if not isinstance(vm_burst_selection, dict):
        vm_burst_selection = {}
    vm_vpu_selection = app_state.get("step4_vm_vpus", {})
    if not isinstance(vm_vpu_selection, dict):
        vm_vpu_selection = {}
    vm_os_license_selection = app_state.get("step4_vm_os_license", {})
    if not isinstance(vm_os_license_selection, dict):
        vm_os_license_selection = {}
    try:
        iaas_discount_pct = float(app_state.get("step4_iaas_discount_pct", 0.0))
    except (TypeError, ValueError):
        iaas_discount_pct = 0.0
    iaas_discount_pct = max(0.0, min(100.0, iaas_discount_pct))

    # Restore last saved Step 4 snapshot (includes selected + non-selected VMs).
    snapshot = load_step4_snapshot()
    snapshot_source = str(snapshot.get("source_vinfo_csv", ""))
    snapshot_settings = snapshot.get("vm_settings", {}) if isinstance(snapshot.get("vm_settings", {}), dict) else {}
    if snapshot_settings and snapshot_source == source_vinfo_csv:
        restored_selected: list[str] = []
        restored_shapes = dict(vm_shape_selection)
        restored_ocpus = dict(vm_ocpu_selection)
        restored_bursts = dict(vm_burst_selection)
        restored_vpus = dict(vm_vpu_selection)
        restored_license = dict(vm_os_license_selection)

        for vm_name, cfg in snapshot_settings.items():
            if vm_name not in vm_index or not isinstance(cfg, dict):
                continue

            if bool(cfg.get("selected", False)):
                restored_selected.append(vm_name)

            shape_val = str(cfg.get("oci_shape", "")).strip()
            if shape_val in valid_shape_values:
                restored_shapes[vm_name] = shape_val

            try:
                ocpu_val = int(cfg.get("ocpu", 0))
                if ocpu_val >= 1:
                    restored_ocpus[vm_name] = ocpu_val
            except (TypeError, ValueError):
                pass

            burst_val = str(cfg.get("burst", "100%")).strip()
            if burst_val == "1:1":
                burst_val = "100%"
            if burst_val in valid_burst_values:
                restored_bursts[vm_name] = burst_val

            try:
                vpu_val = int(cfg.get("vpu", 10))
                if vpu_val in valid_vpu_values:
                    restored_vpus[vm_name] = vpu_val
            except (TypeError, ValueError):
                pass

            license_val = str(cfg.get("os_license", "")).strip()
            if license_val in {"BYOL", "Lic Include"}:
                restored_license[vm_name] = license_val

        selected_vm_names = [n for n in restored_selected if n in vm_index]
        vm_shape_selection = restored_shapes
        vm_ocpu_selection = restored_ocpus
        vm_burst_selection = restored_bursts
        vm_vpu_selection = restored_vpus
        vm_os_license_selection = restored_license

        app_state["selected_vm_names"] = selected_vm_names
        app_state["step4_vm_shapes"] = vm_shape_selection
        app_state["step4_vm_ocpus"] = vm_ocpu_selection
        app_state["step4_vm_bursts"] = vm_burst_selection
        app_state["step4_vm_vpus"] = vm_vpu_selection
        app_state["step4_vm_os_license"] = vm_os_license_selection
        save_app_state(app_state)

    selected_vms = [vm_index[name] for name in selected_vm_names if name in vm_index]
    if not selected_vms:
        flash("No VMs selected yet. Please select VMs in Step 3 first.", "error")
        return redirect(url_for("step3"))

    export_requested = False

    if request.method == "POST":
        action = str(request.form.get("action", "save")).strip().lower()
        vm_names = request.form.getlist("vm_name")
        selected_shapes = request.form.getlist("oci_shape")
        selected_ocpus = request.form.getlist("vm_ocpu")
        selected_bursts = request.form.getlist("vm_burst")
        selected_vpus = request.form.getlist("vm_vpu")
        selected_os_license = request.form.getlist("vm_os_license")
        iaas_discount_raw = str(request.form.get("iaas_discount_pct", iaas_discount_pct)).strip()
        try:
            iaas_discount_pct = float(iaas_discount_raw)
        except (TypeError, ValueError):
            iaas_discount_pct = 0.0
        iaas_discount_pct = max(0.0, min(100.0, iaas_discount_pct))

        updated_shapes = dict(vm_shape_selection)
        updated_ocpus = dict(vm_ocpu_selection)
        updated_bursts = dict(vm_burst_selection)
        updated_vpus = dict(vm_vpu_selection)
        updated_os_license = dict(vm_os_license_selection)
        for vm_name, shape in zip(vm_names, selected_shapes):
            clean_vm = str(vm_name).strip()
            clean_shape = str(shape).strip()
            if clean_vm and clean_shape in valid_shape_values and clean_vm in vm_index:
                updated_shapes[clean_vm] = clean_shape

        for vm_name, ocpu_raw in zip(vm_names, selected_ocpus):
            clean_vm = str(vm_name).strip()
            try:
                ocpu_val = int(float(str(ocpu_raw).strip()))
            except (TypeError, ValueError):
                continue
            if clean_vm and clean_vm in vm_index:
                updated_ocpus[clean_vm] = max(1, ocpu_val)

        for vm_name, burst_raw in zip(vm_names, selected_bursts):
            clean_vm = str(vm_name).strip()
            burst_val = str(burst_raw).strip()
            if clean_vm and clean_vm in vm_index and burst_val in valid_burst_values:
                updated_bursts[clean_vm] = burst_val

        for vm_name, vpu_raw in zip(vm_names, selected_vpus):
            clean_vm = str(vm_name).strip()
            try:
                vpu_val = int(float(str(vpu_raw).strip()))
            except (TypeError, ValueError):
                continue
            if clean_vm and clean_vm in vm_index and vpu_val in valid_vpu_values:
                updated_vpus[clean_vm] = vpu_val

        valid_license_values = {"BYOL", "Lic Include"}
        for vm_name, license_raw in zip(vm_names, selected_os_license):
            clean_vm = str(vm_name).strip()
            raw_os = str(vm_index.get(clean_vm, {}).get("raw_os", "")).lower()
            if "windows server" not in raw_os:
                continue
            license_val = str(license_raw).strip()
            if clean_vm and clean_vm in vm_index and license_val in valid_license_values:
                updated_os_license[clean_vm] = license_val

        app_state["step4_vm_shapes"] = updated_shapes
        app_state["step4_vm_ocpus"] = updated_ocpus
        app_state["step4_vm_bursts"] = updated_bursts
        app_state["step4_vm_vpus"] = updated_vpus
        app_state["step4_vm_os_license"] = updated_os_license
        app_state["step4_iaas_discount_pct"] = iaas_discount_pct
        save_app_state(app_state)

        # Apply latest form selections to in-request variables so export can use them immediately.
        vm_shape_selection = updated_shapes
        vm_ocpu_selection = updated_ocpus
        vm_burst_selection = updated_bursts
        vm_vpu_selection = updated_vpus
        vm_os_license_selection = updated_os_license

        if action == "export_excel":
            export_requested = True
        else:
            # Persist snapshot for all VMs (selected + non-selected) with selected status.
            all_vm_settings: dict[str, dict[str, Any]] = {}

            def _to_number_local(value: Any) -> float:
                try:
                    return float(str(value).strip().replace(",", ""))
                except (TypeError, ValueError):
                    return 0.0

            for vm in all_vms:
                vm_name = str(vm.get("name") or "").strip()
                if not vm_name:
                    continue

                cpu_val = int(_to_number_local(vm.get("cpus")))
                default_ocpu = max(1, cpu_val // 2)

                shape_val = str(updated_shapes.get(vm_name, shape_options[0])).strip()
                if shape_val not in valid_shape_values:
                    shape_val = shape_options[0]

                try:
                    ocpu_val = int(updated_ocpus.get(vm_name, default_ocpu))
                except (TypeError, ValueError):
                    ocpu_val = default_ocpu
                ocpu_val = max(1, ocpu_val)

                burst_val = str(updated_bursts.get(vm_name, "100%")).strip()
                if burst_val == "1:1":
                    burst_val = "100%"
                if burst_val not in valid_burst_values:
                    burst_val = "100%"

                try:
                    vpu_val = int(updated_vpus.get(vm_name, 10))
                except (TypeError, ValueError):
                    vpu_val = 10
                if vpu_val not in valid_vpu_values:
                    vpu_val = 10

                raw_os = str(vm.get("raw_os") or "")
                is_windows_server = "windows server" in raw_os.lower()
                license_val = ""
                if is_windows_server:
                    stored_license = str(updated_os_license.get(vm_name, "BYOL")).strip()
                    license_val = stored_license if stored_license in {"BYOL", "Lic Include"} else "BYOL"

                all_vm_settings[vm_name] = {
                    "selected": vm_name in selected_vm_names,
                    "oci_shape": shape_val,
                    "ocpu": ocpu_val,
                    "burst": burst_val,
                    "vpu": vpu_val,
                    "os_license": license_val,
                }

            save_step4_snapshot(
                {
                    "saved_at": datetime.now().isoformat(),
                    "source_vinfo_csv": source_vinfo_csv,
                    "vm_settings": all_vm_settings,
                }
            )

            flash("Step 4 settings saved for all VMs (selected and non-selected).", "success")
            return redirect(url_for("step4"))

    def _to_number(value: Any) -> float:
        try:
            return float(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

    def _build_vm_cost_row(vm: dict[str, Any]) -> dict[str, Any]:
        vm_name = str(vm.get("name") or "").strip()
        cpu_val = int(_to_number(vm.get("cpus")))
        default_ocpu = max(1, cpu_val // 2)
        saved_ocpu = vm_ocpu_selection.get(vm_name, default_ocpu)
        try:
            effective_ocpu = max(1, int(saved_ocpu))
        except (TypeError, ValueError):
            effective_ocpu = default_ocpu

        saved_burst = str(vm_burst_selection.get(vm_name, "100%")).strip()
        if saved_burst == "1:1":
            saved_burst = "100%"
        burst = saved_burst if saved_burst in valid_burst_values else "100%"
        burst_factor = float(burst_factor_map.get(burst, 1.0))

        vpu_value = int(vm_vpu_selection.get(vm_name, 10)) if int(vm_vpu_selection.get(vm_name, 10)) in valid_vpu_values else 10
        selected_shape = vm_shape_selection.get(vm_name, shape_options[0])
        raw_os_value = (vm.get("raw_os") or "").strip()
        is_windows_server = "windows server" in raw_os_value.lower()
        os_license = ""
        if is_windows_server:
            saved_license = str(vm_os_license_selection.get(vm_name, "BYOL")).strip()
            os_license = saved_license if saved_license in {"BYOL", "Lic Include"} else "BYOL"

        shape_map = shape_pricing_map.get(selected_shape, {})
        ocpu_display = str(shape_map.get("ocpu_display_name", "")).strip()
        memory_display = str(shape_map.get("memory_display_name", "")).strip()

        ocpu_unit_price = float(price_lookup.get(ocpu_display, 0.0))
        memory_unit_price = float(price_lookup.get(memory_display, 0.0))
        block_storage_unit_price = float(
            price_lookup.get(
                "Storage - Block Volume - Storage",
                next(
                    (
                        float(v)
                        for k, v in price_lookup.items()
                        if "block volume" in str(k).lower() and "storage" in str(k).lower() and "free" not in str(k).lower()
                    ),
                    0.0,
                ),
            )
        )
        block_perf_unit_price = float(
            price_lookup.get(
                "Storage - Block Volume - Performance Units",
                next(
                    (
                        float(v)
                        for k, v in price_lookup.items()
                        if "block volume" in str(k).lower() and "performance units" in str(k).lower()
                    ),
                    0.0,
                ),
            )
        )

        memory_gb = int(math.ceil(int(_to_number(vm.get("memory_mb"))) / 1024.0))
        raw_provisioned_gb = int(math.ceil(int(_to_number(vm.get("provisioned_mib"))) / 1024.0))
        provisioned_gb = max(50, raw_provisioned_gb)

        cpu_monthly_cost = (effective_ocpu * ocpu_unit_price) * 730.0
        ram_monthly_cost = (memory_gb * memory_unit_price) * 730.0
        cpu_monthly_cost *= burst_factor
        cpu_ram_monthly_cost = cpu_monthly_cost + ram_monthly_cost
        storage_capacity_monthly_cost = provisioned_gb * block_storage_unit_price
        storage_performance_monthly_cost = provisioned_gb * vpu_value * block_perf_unit_price
        storage_monthly_cost = storage_capacity_monthly_cost + storage_performance_monthly_cost
        os_license_monthly_cost = (windows_os_unit_price * effective_ocpu * 730.0 * burst_factor) if os_license == "Lic Include" else 0.0
        discount_factor = max(0.0, min(1.0, 1.0 - (iaas_discount_pct / 100.0)))

        cpu_monthly_cost *= discount_factor
        ram_monthly_cost *= discount_factor
        cpu_ram_monthly_cost *= discount_factor
        storage_capacity_monthly_cost *= discount_factor
        storage_performance_monthly_cost *= discount_factor
        storage_monthly_cost *= discount_factor

        return {
            "vm_name": vm_name,
            "os_name": raw_os_value or "Unknown / Empty",
            "is_windows_server": is_windows_server,
            "os_license": os_license,
            "cpus": cpu_val,
            "ocpu": effective_ocpu,
            "burst": burst,
            "memory_mb": int(_to_number(vm.get("memory_mb"))),
            "provisioned_mib": int(_to_number(vm.get("provisioned_mib"))),
            "memory_gb": memory_gb,
            "raw_provisioned_gb": raw_provisioned_gb,
            "provisioned_gb": provisioned_gb,
            "vpu": vpu_value,
            "oci_shape": selected_shape,
            "ocpu_unit_price": ocpu_unit_price,
            "memory_unit_price": memory_unit_price,
            "cpu_ram_monthly_cost": cpu_ram_monthly_cost,
            "cpu_monthly_cost": cpu_monthly_cost,
            "ram_monthly_cost": ram_monthly_cost,
            "storage_capacity_monthly_cost": storage_capacity_monthly_cost,
            "storage_performance_monthly_cost": storage_performance_monthly_cost,
            "storage_monthly_cost": storage_monthly_cost,
            "os_license_monthly_cost": os_license_monthly_cost,
        }

    vm_rows: list[dict[str, Any]] = [_build_vm_cost_row(vm) for vm in selected_vms]
    selected_vm_set = set(selected_vm_names)
    non_selected_vms = [vm for vm in all_vms if str(vm.get("name") or "") not in selected_vm_set]
    non_selected_vm_rows: list[dict[str, Any]] = [_build_vm_cost_row(vm) for vm in non_selected_vms]

    vm_rows.sort(key=lambda r: str(r["vm_name"]).lower())
    non_selected_vm_rows.sort(key=lambda r: str(r["vm_name"]).lower())

    overall = {
        "vm_count": len(vm_rows),
        "total_cpus": sum(int(r["cpus"]) for r in vm_rows),
        "total_memory_mb": sum(int(r["memory_mb"]) for r in vm_rows),
        "total_memory_gb": sum(int(r["memory_gb"]) for r in vm_rows),
        "total_provisioned_mib": sum(int(r["provisioned_mib"]) for r in vm_rows),
        "total_provisioned_gb": sum(int(r["provisioned_gb"]) for r in vm_rows),
        "total_vpus": sum(int(r["vpu"]) for r in vm_rows),
        "total_license_included_vms": sum(1 for r in vm_rows if str(r.get("os_license", "")) == "Lic Include"),
        "total_cpu_monthly_cost": sum(float(r["cpu_monthly_cost"]) for r in vm_rows),
        "total_ram_monthly_cost": sum(float(r["ram_monthly_cost"]) for r in vm_rows),
        "total_storage_capacity_monthly_cost": sum(float(r["storage_capacity_monthly_cost"]) for r in vm_rows),
        "total_storage_performance_monthly_cost": sum(float(r["storage_performance_monthly_cost"]) for r in vm_rows),
        "total_cpu_ram_monthly_cost": sum(float(r["cpu_ram_monthly_cost"]) for r in vm_rows),
        "total_storage_monthly_cost": sum(float(r["storage_monthly_cost"]) for r in vm_rows),
        "total_os_license_monthly_cost": sum(float(r["os_license_monthly_cost"]) for r in vm_rows),
    }
    overall["total_monthly_cost"] = (
        float(overall["total_cpu_ram_monthly_cost"])
        + float(overall["total_storage_monthly_cost"])
        + float(overall["total_os_license_monthly_cost"])
    )

    if export_requested:
        headers = [
            "VM Name",
            "OS (Full version)",
            "OS License",
            "CPUs",
            "OCPU",
            "Burst",
            "Memory (MB)",
            "Storage (GB)",
            "VPU",
            "OCI Target Shape",
            "CPU Monthly Cost",
            "RAM Monthly Cost",
            "CPU/RAM Monthly Cost",
            "Storage Capacity Monthly Cost",
            "VPU Monthly Cost",
            "Storage Monthly Cost",
            "OS License Monthly Cost",
            "Total Monthly Cost",
        ]
        discount_factor = max(0.0, min(1.0, 1.0 - (iaas_discount_pct / 100.0)))
        pricelist_rows: list[list[Any]] = [
            [
                str(shape_name),
                float((shape_price_rates.get(shape_name, {}) or {}).get("ocpu_unit_price", 0.0)),
                float((shape_price_rates.get(shape_name, {}) or {}).get("memory_unit_price", 0.0)),
            ]
            for shape_name in shape_options
        ]
        shape_lookup_last_row = max(2, 1 + len(pricelist_rows))
        shape_lookup_range = f"PriceList!$A$2:$C${shape_lookup_last_row}"

        def _burst_to_factor(value: Any) -> float:
            v = str(value or "").strip()
            if v in {"100%", "1:1"}:
                return 1.0
            if v == "50%":
                return 0.5
            if v == "12.5%":
                return 0.125
            return 1.0

        currency_format_map = {
            "EUR": "€#,##0.00",
            "USD": "$#,##0.00",
            "GBP": "£#,##0.00",
            "JPY": "¥#,##0.00",
            "CHF": '"CHF "#,##0.00',
            "AUD": '"A$"#,##0.00',
            "CAD": '"C$"#,##0.00',
            "SGD": '"S$"#,##0.00',
            "SEK": '"kr "#,##0.00',
            "NOK": '"kr "#,##0.00',
            "DKK": '"kr "#,##0.00',
        }
        currency_fmt_code = currency_format_map.get(
            str(pricing_currency or "USD").upper(),
            f'"{str(pricing_currency or "USD").upper()} "#,##0.00',
        )

        def _export_line_from_row(r: dict[str, Any], row_idx: int) -> list[Any]:
            burst_factor = _burst_to_factor(r.get("burst", "100%"))
            cpu_formula = (
                f"=E{row_idx}*VLOOKUP(J{row_idx},{shape_lookup_range},2,FALSE)"
                f"*730*F{row_idx}*PriceList!$F$5"
            )
            ram_formula = (
                f"=ROUNDUP(G{row_idx}/1024,0)*VLOOKUP(J{row_idx},{shape_lookup_range},3,FALSE)"
                f"*730*PriceList!$F$5"
            )
            cpu_ram_formula = f"=K{row_idx}+L{row_idx}"
            storage_capacity_formula = f"=H{row_idx}*PriceList!$F$2*PriceList!$F$5"
            vpu_formula = f"=H{row_idx}*I{row_idx}*PriceList!$F$3*PriceList!$F$5"
            storage_formula = f"=N{row_idx}+O{row_idx}"
            os_license_formula = (
                f"=IF(C{row_idx}=\"Lic Include\",E{row_idx}*PriceList!$F$4*730*F{row_idx},0)"
            )
            total_formula = f"=M{row_idx}+P{row_idx}+Q{row_idx}"

            return [
                r.get("vm_name", ""),
                r.get("os_name", ""),
                r.get("os_license", ""),
                r.get("cpus", 0),
                r.get("ocpu", 0),
                burst_factor,
                r.get("memory_mb", 0),
                r.get("provisioned_gb", 0),
                r.get("vpu", 0),
                r.get("oci_shape", ""),
                cpu_formula,
                ram_formula,
                cpu_ram_formula,
                storage_capacity_formula,
                vpu_formula,
                storage_formula,
                os_license_formula,
                total_formula,
            ]

        data_rows_selected: list[list[Any]] = [
            _export_line_from_row(r, idx) for idx, r in enumerate(vm_rows, start=2)
        ]
        data_rows_non_selected: list[list[Any]] = [
            _export_line_from_row(r, idx) for idx, r in enumerate(non_selected_vm_rows, start=2)
        ]

        def _col_ref(col_idx: int) -> str:
            col_idx += 1
            letters = ""
            while col_idx:
                col_idx, rem = divmod(col_idx - 1, 26)
                letters = chr(65 + rem) + letters
            return letters

        def _cell_xml(value: Any, row_idx: int, col_idx: int, style_idx: int | None = None) -> str:
            ref = f"{_col_ref(col_idx)}{row_idx}"
            style_attr = f' s="{style_idx}"' if style_idx is not None else ""
            if isinstance(value, (int, float)):
                return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'

            text = str(value)
            if text.startswith("="):
                formula = xml_escape(text[1:])
                return f'<c r="{ref}"{style_attr}><f>{formula}</f></c>'
            try:
                numeric_text = text.replace(",", "")
                if numeric_text and all(ch in "0123456789.-" for ch in numeric_text):
                    float(numeric_text)
                    return f'<c r="{ref}"{style_attr}><v>{numeric_text}</v></c>'
            except Exception:
                pass

            safe = xml_escape(text)
            return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t>{safe}</t></is></c>'

        def _sheet_xml_for_rows(
            all_rows: list[list[Any]],
            currency_columns: set[int] | None = None,
            percent_columns: set[int] | None = None,
            header_rows: int = 1,
        ) -> str:
            sheet_rows_xml: list[str] = []
            currency_columns = currency_columns or set()
            percent_columns = percent_columns or set()
            for i, row in enumerate(all_rows, start=1):
                row_cells: list[str] = []
                for c, value in enumerate(row):
                    style_idx = None
                    if i > header_rows and c in currency_columns:
                        style_idx = 1
                    elif i > header_rows and c in percent_columns:
                        style_idx = 2
                    row_cells.append(_cell_xml(value, i, c, style_idx=style_idx))
                cells = "".join(row_cells)
                sheet_rows_xml.append(f'<row r="{i}">{cells}</row>')
            return (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<sheetData>{''.join(sheet_rows_xml)}</sheetData>"
                '</worksheet>'
            )

        # Burst column F should display as percentage; cost columns K..R as currency.
        vm_burst_percent_cols = {5}
        vm_cost_currency_cols = set(range(10, 18))
        sheet_selected_xml = _sheet_xml_for_rows(
            [headers] + data_rows_selected,
            currency_columns=vm_cost_currency_cols,
            percent_columns=vm_burst_percent_cols,
        )
        sheet_non_selected_xml = _sheet_xml_for_rows(
            [headers] + data_rows_non_selected,
            currency_columns=vm_cost_currency_cols,
            percent_columns=vm_burst_percent_cols,
        )

        # Third sheet with price list/rates, referenced by formulas in both VM sheets.
        max_pricelist_rows = max(len(pricelist_rows), 4)
        pricelist_sheet_rows: list[list[Any]] = [["OCI Target Shape", "OCPU Unit Price", "Memory Unit Price", "Currency", "Parameter", "Value", "Currency"]]
        for idx in range(max_pricelist_rows):
            row: list[Any] = ["", "", "", "", "", "", ""]
            if idx < len(pricelist_rows):
                row[0] = pricelist_rows[idx][0]
                row[1] = pricelist_rows[idx][1]
                row[2] = pricelist_rows[idx][2]
                row[3] = pricing_currency or "USD"

            # Keep these values anchored to F2..F5 for formula references.
            if idx == 0:
                row[4] = "Block Storage Unit Price"
                row[5] = block_storage_unit_price
                row[6] = pricing_currency or "USD"
            elif idx == 1:
                row[4] = "Block Performance Unit Price"
                row[5] = block_perf_unit_price
                row[6] = pricing_currency or "USD"
            elif idx == 2:
                row[4] = "Windows OS Unit Price"
                row[5] = windows_os_unit_price
                row[6] = pricing_currency or "USD"
            elif idx == 3:
                row[4] = "IaaS Discount Factor"
                row[5] = discount_factor

            pricelist_sheet_rows.append(row)

        sheet_pricelist_xml = _sheet_xml_for_rows(pricelist_sheet_rows)

        styles_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<numFmts count="2">'
            f'<numFmt numFmtId="164" formatCode="{xml_escape(currency_fmt_code)}"/>'
            '<numFmt numFmtId="165" formatCode="0.0%"/>'
            '</numFmts>'
            '<fonts count="1">'
            '<font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/></font>'
            '</fonts>'
            '<fills count="2">'
            '<fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            '</fills>'
            '<borders count="1">'
            '<border><left/><right/><top/><bottom/><diagonal/></border>'
            '</borders>'
            '<cellStyleXfs count="1">'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
            '</cellStyleXfs>'
            '<cellXfs count="3">'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
            '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
            '</cellXfs>'
            '<cellStyles count="1">'
            '<cellStyle name="Normal" xfId="0" builtinId="0"/>'
            '</cellStyles>'
            '</styleSheet>'
        )

        workbook_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets>'
            '<sheet name="Selected VMs" sheetId="1" r:id="rId1"/>'
            '<sheet name="Non-selected VMs" sheetId="2" r:id="rId2"/>'
            '<sheet name="PriceList" sheetId="3" r:id="rId3"/>'
            '</sheets>'
            '</workbook>'
        )

        workbook_rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet2.xml"/>'
            '<Relationship Id="rId3" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet3.xml"/>'
            '<Relationship Id="rId4" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
            '</Relationships>'
        )

        root_rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            '</Relationships>'
        )

        content_types_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/worksheets/sheet2.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/worksheets/sheet3.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>'
        )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types_xml)
            zf.writestr("_rels/.rels", root_rels_xml)
            zf.writestr("xl/workbook.xml", workbook_xml)
            zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
            zf.writestr("xl/styles.xml", styles_xml)
            zf.writestr("xl/worksheets/sheet1.xml", sheet_selected_xml)
            zf.writestr("xl/worksheets/sheet2.xml", sheet_non_selected_xml)
            zf.writestr("xl/worksheets/sheet3.xml", sheet_pricelist_xml)

        filename = f"step4_vm_costing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return Response(
            buffer.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return render_template(
        "step4.html",
        selected_rvtools_file=selected_rvtools_file,
        source_vinfo_csv=source_vinfo_csv,
        vm_rows=vm_rows,
        overall=overall,
        shape_options=shape_options,
        vpu_options=vpu_options,
        pricing_currency=pricing_currency,
        source_pricelist_file=source_pricelist_file,
        shape_price_rates=shape_price_rates,
        block_storage_unit_price=block_storage_unit_price,
        block_perf_unit_price=block_perf_unit_price,
        windows_os_unit_price=windows_os_unit_price,
        iaas_discount_pct=iaas_discount_pct,
    )


if __name__ == "__main__":
    app.run(debug=True)
