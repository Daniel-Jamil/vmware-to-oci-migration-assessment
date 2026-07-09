from __future__ import annotations

import copy
import json
import os
import csv
import hashlib
import math
import io
import re
import shutil
import subprocess
import sys
import threading
import zipfile
import ssl
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import time

from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from assessment_readiness import build_assessment_readiness
from assessment_portability import (
    MAX_PACKAGE_BYTES,
    PortableAssessmentError,
    build_portable_package,
    dumps_portable_package,
    validate_portable_package,
)


def _first_env(*names: str) -> str | None:
    for name in names:
        raw_value = os.environ.get(name)
        if raw_value is not None:
            return raw_value
    return None


def _env_int(*names: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    raw_value = _first_env(*names)
    if raw_value is None:
        return default
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_bool(*names: str, default: bool = False) -> bool:
    raw_value = _first_env(*names)
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_internal_return_path(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    target = value.strip()
    if (
        not target
        or not target.startswith("/")
        or target.startswith("//")
        or any(char in target for char in "\r\n\t")
    ):
        return ""
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return ""
    return target


def _safe_internal_referrer_path(value: Any, host_url: str) -> str:
    if not isinstance(value, str):
        return ""
    target = value.strip()
    if not target or any(char in target for char in "\r\n\t"):
        return ""
    parts = urlsplit(target)
    if not parts.scheme and not parts.netloc:
        return _safe_internal_return_path(target)
    host_parts = urlsplit(host_url)
    if parts.scheme not in {"http", "https"} or parts.netloc != host_parts.netloc:
        return ""
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return _safe_internal_return_path(path)


app = Flask(__name__)
app.config["SECRET_KEY"] = (
    os.environ.get("MIGRATION_ASSESSMENT_SECRET_KEY")
    or os.environ.get("VMW2OCI_SECRET_KEY")
    or os.environ.get("FLASK_SECRET_KEY")
    or uuid4().hex
)
app.config["MAX_CONTENT_LENGTH"] = _env_int(
    "MIGRATION_ASSESSMENT_MAX_UPLOAD_MB",
    "VMW2OCI_MAX_UPLOAD_MB",
    default=250,
    min_value=1,
) * 1024 * 1024
# Step 4 can submit thousands of small per-VM form fields for large inventories.
app.config["MAX_FORM_MEMORY_SIZE"] = _env_int(
    "MIGRATION_ASSESSMENT_MAX_FORM_MB",
    "VMW2OCI_MAX_FORM_MB",
    default=128,
    min_value=1,
) * 1024 * 1024
app.config["MAX_FORM_PARTS"] = _env_int(
    "MIGRATION_ASSESSMENT_MAX_FORM_PARTS",
    "VMW2OCI_MAX_FORM_PARTS",
    default=50000,
    min_value=1000,
)
PORTABLE_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
MAX_PORTABLE_REQUEST_BYTES = MAX_PACKAGE_BYTES + PORTABLE_MULTIPART_OVERHEAD_BYTES
APP_INSTANCE_ID = uuid4().hex
_PREFERENCES_LOCK = threading.RLock()


@app.errorhandler(RequestEntityTooLarge)
def request_entity_too_large(_: RequestEntityTooLarge) -> Any:
    flash(
        "The submitted form is larger than the current local limit. "
        "For very large inventories, increase MIGRATION_ASSESSMENT_MAX_FORM_MB or "
        "MIGRATION_ASSESSMENT_MAX_FORM_PARTS and restart the app.",
        "error",
    )
    if request.path.startswith(("/step4", "/scenario", "/step5")):
        return redirect(step4_tab_redirect("native")), 303
    return redirect(url_for("index")), 303


@app.before_request
def reset_session_for_new_app_start() -> None:
    """Start each freshly launched app process with a clean browser session."""
    if session.get("_app_instance_id") != APP_INSTANCE_ID:
        session.clear()
        session["_app_instance_id"] = APP_INSTANCE_ID


@app.after_request
def add_html_no_cache_headers(response: Any) -> Any:
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


OCI_PRODUCTS_API_BASE = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"
DOWNLOADS_DIR = Path("downloads")
RVTOOLS_DIR = Path("rvtools")
EXPORTS_DIR = DOWNLOADS_DIR / "exports"

# Currencies exposed by the local assessment UI.
SUPPORTED_CURRENCIES = [
    "USD",
    "EUR",
    "GBP",
    "CHF",
    "SEK",
    "NOK",
    "DKK",
]

SUPPORTED_RVTOOLS_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}
OS_MAPPING_CONFIG_PATH = Path("config/os_mapping.json")
OCI_SUPPORTED_OS_PATH = Path("OCI-SupportedOS.txt")
OCI_PRICE_MAPPING_PATH = Path("OCI-PriceMapping")
OCVS_TERM_DISCOUNTS_PATH = Path("config/ocvs_term_discounts.json")
APP_STATE_DIR = Path("downloads/app_state")
SAVED_ASSESSMENT_SCHEMA_VERSION = 1
IMPORTED_INVENTORY_FORMAT = "vmware_to_oci_normalized_inventory"
IMPORTED_INVENTORY_SCHEMA_VERSION = 1
PRICE_LIST_DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_VISIBLE_PRICE_LISTS = 10
NATIVE_VM_INPUT_ROW_LIMIT = 50
NATIVE_PAGE_SIZE_OPTIONS = (25, 50, 100)
NATIVE_SUPPORT_FILTERS = {"all", "supported", "remediation", "review"}
NATIVE_SEARCH_MAX_LENGTH = 200
STEP4_UNSAVED_READINESS_SESSION_KEY = "_step4_unsaved_scenario_changes"
STEP4_ALLOWED_ACTIONS = {"save", "export_excel"}
STEP4_ACTIVE_SCENARIOS = {"native", "ocvs", "hybrid", "price"}
RESULT_RECOMMENDATION_VALUES = {"", "native", "ocvs", "hybrid"}
RESULT_RECOMMENDATION_FIELDS = {
    "action",
    "recommendation",
    "recommendation_rationale",
}
STEP4_SINGLE_VALUE_FIELDS = {
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
    "hybrid_ocvs_profile",
    "hybrid_ocvs_commitment_term",
    "hybrid_ocvs_vcpu_per_ocpu",
    "hybrid_ocvs_cpu_headroom_pct",
    "hybrid_ocvs_memory_headroom_pct",
    "hybrid_ocvs_storage_headroom_pct",
    "hybrid_ocvs_dense_vsan_usable_pct",
    "hybrid_ocvs_standard_storage_vpu",
    "hybrid_ocvs_dr_nodes",
    "hybrid_vmware_license_price_per_core_yearly",
}

HOURS_PER_MONTH = 730.0
MIN_BLOCK_VOLUME_GB = 50
VPU_OPTIONS = list(range(10, 121, 10))
VALID_BURST_VALUES = {"100%", "50%", "12.5%", "1:1"}
VALID_OCVS_DR_NODE_COUNTS = {0, 1, 2}
OCVS_COMMITMENT_TERMS = {"payg", "1_year", "3_year"}
OCVS_COMMITMENT_LABELS = {
    "payg": "Pay as you go",
    "1_year": "1-Year",
    "3_year": "3-Year",
}
BURST_FACTOR_MAP = {
    "100%": 1.0,
    "1:1": 1.0,
    "50%": 0.5,
    "12.5%": 0.125,
}
OS_LICENSE_VALUES = {"BYOL", "Lic Include"}
HYBRID_PLACEMENT_VALUES = {"native", "ocvs", "review"}
HYBRID_PLACEMENT_LABELS = {
    "native": "OCI Native",
    "ocvs": "OCVS",
    "review": "Review (priced as OCVS)",
}
HYBRID_PLACEMENT_OPTIONS = [
    {"value": "native", "label": "OCI Native"},
    {"value": "ocvs", "label": "OCVS"},
    {"value": "review", "label": "Review (priced as OCVS)"},
]

OCVS_DEFAULT_SIZING_POLICY = {
    "vcpu_per_ocpu": 4.0,
    "cpu_headroom_pct": 20.0,
    "memory_headroom_pct": 20.0,
    "storage_headroom_pct": 25.0,
    "dense_vsan_usable_pct": 50.0,
    "standard_storage_vpu": 10,
}

OCVS_DEFAULT_TERM_DISCOUNTS = {
    "BM.DenseIO2.52": {"1_year": 35.0, "3_year": 45.0},
    "BM.DenseIO.E4.128": {"1_year": 35.0, "3_year": 50.0},
    "BM.Standard3.64": {"1_year": 30.0, "3_year": 40.0},
    "BM.Standard2.52": {"1_year": 35.0, "3_year": 45.0},
    "BM.Standard.E4.128": {"1_year": 35.0, "3_year": 45.0},
    "BM.GPU.A10.4": {"1_year": 35.0, "3_year": 45.0},
    "BM.Standard.E5.192": {"1_year": 35.0, "3_year": 50.0},
    "BM.DenseIO.E5.128": {"1_year": 35.0, "3_year": 50.0},
    "BM.Optimized3.36": {"1_year": 10.0, "3_year": 50.0},
}

OCVS_HOST_PROFILES = [
    {
        "shape": "BM.DenseIO2.52",
        "label": "OCVS DenseIO2",
        "host_type": "Dense",
        "ocpus": 52,
        "memory_gb": 768,
        "nvme_tb": 51.2,
        "min_hosts": 3,
        "max_hosts": 64,
        "ocpu_display_name": "Compute - Virtual Machine Dense I/O - X7",
        "memory_display_name": "",
        "nvme_display_name": "",
    },
    {
        "shape": "BM.DenseIO.E4.128",
        "label": "OCVS Dense E4",
        "host_type": "Dense",
        "ocpus": 128,
        "memory_gb": 2048,
        "nvme_tb": 54.4,
        "min_hosts": 3,
        "max_hosts": 64,
        "ocpu_display_name": "Compute - Dense I/O - E4 - OCPU",
        "memory_display_name": "Compute - Dense I/O - E4 - Memory",
        "nvme_display_name": "Compute - Dense I/O - E4 - NVMe",
    },
    {
        "shape": "BM.DenseIO.E5.128",
        "label": "OCVS Dense E5",
        "host_type": "Dense",
        "ocpus": 128,
        "memory_gb": 1536,
        "nvme_tb": 81.6,
        "min_hosts": 3,
        "max_hosts": 64,
        "ocpu_display_name": "Oracle Cloud Infrastructure - Compute - Dense I/O - E5 OCPU",
        "memory_display_name": "Oracle Cloud Infrastructure - Compute - Dense I/O - E5 Memory",
        "nvme_display_name": "Oracle Cloud Infrastructure - Compute - Dense I/O - E5 NVMe",
    },
    {
        "shape": "BM.Standard2.52",
        "label": "OCVS Standard2",
        "host_type": "Standard",
        "ocpus": 52,
        "memory_gb": 768,
        "nvme_tb": 0.0,
        "min_hosts": 3,
        "max_hosts": 32,
        "ocpu_display_name": "Compute - Virtual Machine Standard - X7",
        "memory_display_name": "",
        "nvme_display_name": "",
    },
    {
        "shape": "BM.Standard3.64",
        "label": "OCVS Standard3",
        "host_type": "Standard",
        "ocpus": 64,
        "memory_gb": 1024,
        "nvme_tb": 0.0,
        "min_hosts": 3,
        "max_hosts": 32,
        "ocpu_display_name": "Compute - Standard - X9 - OCPU",
        "memory_display_name": "Compute - Standard - X9 - Memory",
        "nvme_display_name": "",
    },
    {
        "shape": "BM.Optimized3.36",
        "label": "OCVS Optimized3",
        "host_type": "Standard",
        "ocpus": 36,
        "memory_gb": 512,
        "nvme_tb": 0.0,
        "min_hosts": 3,
        "max_hosts": 32,
        "ocpu_display_name": "Compute - Optimized - X9 - OCPU",
        "memory_display_name": "Compute - Optimized - X9 - Memory",
        "nvme_display_name": "",
    },
    {
        "shape": "BM.Standard.E4.128",
        "label": "OCVS Standard E4",
        "host_type": "Standard",
        "ocpus": 128,
        "memory_gb": 2048,
        "nvme_tb": 0.0,
        "min_hosts": 3,
        "max_hosts": 32,
        "ocpu_display_name": "Compute - Standard - E4 - OCPU",
        "memory_display_name": "Compute - Standard - E4  - Memory",
        "nvme_display_name": "",
    },
    {
        "shape": "BM.Standard.E5.192",
        "label": "OCVS Standard E5",
        "host_type": "Standard",
        "ocpus": 192,
        "memory_gb": 2304,
        "nvme_tb": 0.0,
        "min_hosts": 3,
        "max_hosts": 32,
        "ocpu_display_name": "Compute - Standard - E5 - OCPU",
        "memory_display_name": "Compute - Standard - E5 - Memory",
        "nvme_display_name": "",
    },
]

def _cleanup_legacy_session_keys() -> None:
    """Remove large legacy client-side session keys (cookie bloat guard)."""
    session.pop("selected_vm_names", None)
    session.pop("step4_os_shapes", None)


def normalize_customer_name(value: Any) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    return clean[:120]


def customer_file_slug(customer_name: str) -> str:
    clean = normalize_customer_name(customer_name).lower()
    slug = re.sub(r"[^a-z0-9]+", "_", clean).strip("_")
    return (slug[:64].strip("_") or "customer")


def build_export_filename(
    customer_name: str,
    artifact_name: str,
    extension: str,
    timestamp: str | None = None,
) -> str:
    artifact_slug = re.sub(r"[^a-z0-9]+", "_", str(artifact_name or "export").lower()).strip("_")
    artifact_slug = artifact_slug or "export"
    ext = str(extension or "").lstrip(".") or "dat"
    timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{customer_file_slug(customer_name)}_{artifact_slug}_{timestamp}.{ext}"


def _default_app_state() -> dict[str, Any]:
    return {
        "selected_vm_names": [],
        "acknowledged_warning_ids": [],
        "assessor_recommendation": "",
        "assessor_recommendation_rationale": "",
        "step4_os_shapes": {},
        "step4_vm_shapes": {},
        "step4_vm_ocpus": {},
        "step4_vm_bursts": {},
        "step4_vm_vpus": {},
        "step4_vm_os_license": {},
        "step4_hybrid_placements": {},
        "step4_iaas_discount_pct": 0.0,
        "step4_ocvs_profile": "best_fit",
        "step4_ocvs_policy": dict(OCVS_DEFAULT_SIZING_POLICY),
        "step4_ocvs_commitment_term": "payg",
        "step4_vmware_license_price_per_core_yearly": 0.0,
        "step4_ocvs_dr_nodes": 0,
        "step4_hybrid_ocvs_customized": False,
        "step4_hybrid_ocvs_profile": "best_fit",
        "step4_hybrid_ocvs_policy": dict(OCVS_DEFAULT_SIZING_POLICY),
        "step4_hybrid_ocvs_commitment_term": "payg",
        "step4_hybrid_vmware_license_price_per_core_yearly": 0.0,
        "step4_hybrid_ocvs_dr_nodes": 0,
        "step4_last_updated_at": "",
    }


def normalize_ocvs_profile(value: Any) -> str:
    selected = str(value or "best_fit").strip()
    valid_shapes = {str(profile.get("shape", "")).strip() for profile in OCVS_HOST_PROFILES}
    return selected if selected == "best_fit" or selected in valid_shapes else "best_fit"


def normalize_ocvs_commitment_term(value: Any) -> str:
    selected = str(value or "payg").strip().lower().replace("-", "_")
    aliases = {
        "pay_as_you_go": "payg",
        "paygo": "payg",
        "payg": "payg",
        "1yr": "1_year",
        "1_year": "1_year",
        "one_year": "1_year",
        "3yr": "3_year",
        "3_year": "3_year",
        "three_year": "3_year",
    }
    selected = aliases.get(selected, selected)
    return selected if selected in OCVS_COMMITMENT_TERMS else "payg"


def normalize_ocvs_dr_nodes(value: Any) -> int:
    try:
        numeric = float(str(value).strip())
        parsed = int(numeric) if math.isfinite(numeric) and numeric.is_integer() else 0
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed in VALID_OCVS_DR_NODE_COUNTS else 0


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        fallback = float(default)
    except (TypeError, ValueError):
        fallback = minimum
    if not math.isfinite(fallback):
        fallback = minimum
    fallback = max(minimum, min(maximum, fallback))
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        parsed = fallback
    if not math.isfinite(parsed):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        fallback_numeric = float(default)
        fallback = int(fallback_numeric) if math.isfinite(fallback_numeric) else minimum
    except (TypeError, ValueError, OverflowError):
        fallback = minimum
    fallback = max(minimum, min(maximum, fallback))
    try:
        numeric = float(str(value).strip())
        parsed = int(numeric) if math.isfinite(numeric) and numeric.is_integer() else fallback
    except (TypeError, ValueError, OverflowError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def normalize_ocvs_policy(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    default = OCVS_DEFAULT_SIZING_POLICY
    return {
        "vcpu_per_ocpu": _bounded_float(raw.get("vcpu_per_ocpu"), float(default["vcpu_per_ocpu"]), 1.0, 16.0),
        "cpu_headroom_pct": _bounded_float(raw.get("cpu_headroom_pct"), float(default["cpu_headroom_pct"]), 0.0, 90.0),
        "memory_headroom_pct": _bounded_float(raw.get("memory_headroom_pct"), float(default["memory_headroom_pct"]), 0.0, 90.0),
        "storage_headroom_pct": _bounded_float(raw.get("storage_headroom_pct"), float(default["storage_headroom_pct"]), 0.0, 90.0),
        "dense_vsan_usable_pct": _bounded_float(raw.get("dense_vsan_usable_pct"), float(default["dense_vsan_usable_pct"]), 10.0, 95.0),
        "standard_storage_vpu": _bounded_int(raw.get("standard_storage_vpu"), int(default["standard_storage_vpu"]), 10, 120),
    }


def effective_hybrid_ocvs_assumptions(
    app_state: dict[str, Any],
    *,
    ocvs_profile_choice: str,
    ocvs_policy: dict[str, Any],
    ocvs_commitment_term: str,
    vmware_license_price_per_core_yearly: float,
    ocvs_dr_nodes: int,
) -> dict[str, Any]:
    customized = app_state.get("step4_hybrid_ocvs_customized") is True
    if not customized:
        return {
            "customized": False,
            "profile_choice": normalize_ocvs_profile(ocvs_profile_choice),
            "policy": normalize_ocvs_policy(ocvs_policy),
            "commitment_term": normalize_ocvs_commitment_term(ocvs_commitment_term),
            "vmware_license_price_per_core_yearly": _bounded_float(
                vmware_license_price_per_core_yearly,
                0.0,
                0.0,
                1_000_000.0,
            ),
            "dr_nodes": normalize_ocvs_dr_nodes(ocvs_dr_nodes),
        }

    return {
        "customized": True,
        "profile_choice": normalize_ocvs_profile(
            app_state.get("step4_hybrid_ocvs_profile", ocvs_profile_choice)
        ),
        "policy": normalize_ocvs_policy(
            app_state.get("step4_hybrid_ocvs_policy", ocvs_policy)
        ),
        "commitment_term": normalize_ocvs_commitment_term(
            app_state.get("step4_hybrid_ocvs_commitment_term", ocvs_commitment_term)
        ),
        "vmware_license_price_per_core_yearly": _bounded_float(
            app_state.get("step4_hybrid_vmware_license_price_per_core_yearly"),
            vmware_license_price_per_core_yearly,
            0.0,
            1_000_000.0,
        ),
        "dr_nodes": normalize_ocvs_dr_nodes(
            app_state.get("step4_hybrid_ocvs_dr_nodes", ocvs_dr_nodes)
        ),
    }


def load_ocvs_term_discounts() -> dict[str, dict[str, float]]:
    discounts: dict[str, dict[str, float]] = {
        shape: {term: float(value) for term, value in terms.items()}
        for shape, terms in OCVS_DEFAULT_TERM_DISCOUNTS.items()
    }
    if not OCVS_TERM_DISCOUNTS_PATH.exists():
        return discounts

    try:
        loaded = json.loads(OCVS_TERM_DISCOUNTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return discounts
    if not isinstance(loaded, dict):
        return discounts

    for shape, term_values in loaded.items():
        if not isinstance(term_values, dict):
            continue
        clean_shape = str(shape or "").strip()
        if not clean_shape:
            continue
        shape_discounts = discounts.setdefault(clean_shape, {})
        for term, raw_pct in term_values.items():
            clean_term = normalize_ocvs_commitment_term(term)
            if clean_term == "payg":
                continue
            shape_discounts[clean_term] = _bounded_float(raw_pct, 0.0, 0.0, 100.0)
    return discounts


def ocvs_term_discount_pct(shape: Any, commitment_term: Any) -> float:
    term = normalize_ocvs_commitment_term(commitment_term)
    if term == "payg":
        return 0.0
    shape_key = str(shape or "").strip()
    return float(load_ocvs_term_discounts().get(shape_key, {}).get(term, 0.0) or 0.0)


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


def _preferences_file_path() -> Path:
    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return APP_STATE_DIR / "preferences.json"


def _write_json_atomically(file_path: Path, payload: Any) -> None:
    temporary_file = file_path.with_name(f".{file_path.name}.{uuid4().hex}.tmp")
    try:
        temporary_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary_file, file_path)
    finally:
        try:
            temporary_file.unlink(missing_ok=True)
        except OSError:
            app.logger.exception("Temporary JSON cleanup failed")


def _write_new_json_atomically(file_path: Path, payload: Any) -> None:
    """Publish a complete JSON file without replacing an existing path."""
    temporary_file = file_path.with_name(f".{file_path.name}.{uuid4().hex}.tmp")
    try:
        temporary_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.link(temporary_file, file_path)
    finally:
        try:
            temporary_file.unlink(missing_ok=True)
        except OSError:
            app.logger.exception("Temporary exclusive JSON cleanup failed")


def _read_optional_file_bytes(file_path: Path) -> tuple[bool, bytes]:
    if not file_path.exists():
        return False, b""
    return True, file_path.read_bytes()


def _restore_optional_file_bytes(
    file_path: Path,
    prior_value: tuple[bool, bytes],
) -> None:
    existed, payload = prior_value
    if not existed:
        file_path.unlink(missing_ok=True)
        return
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = file_path.with_name(f".{file_path.name}.{uuid4().hex}.tmp")
    try:
        temporary_file.write_bytes(payload)
        os.replace(temporary_file, file_path)
    finally:
        temporary_file.unlink(missing_ok=True)


def _read_preferences_snapshot() -> tuple[bool, bytes]:
    with _PREFERENCES_LOCK:
        return _read_optional_file_bytes(_preferences_file_path())


def _preferences_from_snapshot(snapshot: tuple[bool, bytes]) -> dict[str, Any]:
    existed, payload = snapshot
    if not existed:
        return {}
    try:
        loaded = json.loads(payload.decode("utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _preferences_snapshot(preferences: dict[str, Any]) -> tuple[bool, bytes]:
    return True, json.dumps(preferences, indent=2).encode("utf-8")


def _compare_and_swap_preferences(
    expected: tuple[bool, bytes],
    desired: tuple[bool, bytes],
) -> bool:
    preferences_file = _preferences_file_path()
    with _PREFERENCES_LOCK:
        if _read_optional_file_bytes(preferences_file) != expected:
            return False
        _restore_optional_file_bytes(preferences_file, desired)
        return True


def _restore_preferences_if_current(
    prior: tuple[bool, bytes],
    written: tuple[bool, bytes],
) -> bool:
    preferences_file = _preferences_file_path()
    with _PREFERENCES_LOCK:
        if _read_optional_file_bytes(preferences_file) != written:
            return False
        _restore_optional_file_bytes(preferences_file, prior)
        return True


def _update_preference_keys(
    set_values: dict[str, Any],
    remove_keys: set[str],
) -> tuple[tuple[bool, bytes], tuple[bool, bytes]] | None:
    for _ in range(10):
        expected = _read_preferences_snapshot()
        current = _preferences_from_snapshot(expected)
        updated = copy.deepcopy(current)
        updated.update(set_values)
        for key in remove_keys:
            updated.pop(key, None)
        if updated == current:
            return None
        desired = _preferences_snapshot(updated)
        try:
            if not _compare_and_swap_preferences(expected, desired):
                continue
        except Exception:
            _restore_preferences_if_current(expected, desired)
            raise
        return expected, desired
    raise OSError("Preferences changed repeatedly while applying an update.")


def load_preferences() -> dict[str, Any]:
    return _preferences_from_snapshot(_read_preferences_snapshot())


def save_preferences(preferences: dict[str, Any]) -> None:
    with _PREFERENCES_LOCK:
        _write_json_atomically(_preferences_file_path(), preferences)


def remember_price_list_selection(file_path: str, currency: str = "") -> None:
    clean_file = str(file_path or "").strip().replace("\\", "/")
    if not clean_file:
        return
    values = {"last_selected_pricelist_file": clean_file}
    if currency:
        values["last_selected_currency"] = str(currency).upper().strip()
    _update_preference_keys(values, set())


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
    _write_json_atomically(_step4_snapshot_file_path(), snapshot)


def clear_step4_snapshot() -> None:
    """Remove Step 4 snapshot for current session if it exists."""
    snapshot_file = _step4_snapshot_file_path()
    try:
        if snapshot_file.exists():
            snapshot_file.unlink()
    except Exception:
        pass


def normalize_app_state(value: Any) -> dict[str, Any]:
    loaded = value if isinstance(value, dict) else {}
    default = _default_app_state()
    default.update(loaded)
    if not isinstance(default.get("selected_vm_names"), list):
        default["selected_vm_names"] = []
    warning_ids = default.get("acknowledged_warning_ids")
    normalized_warning_ids: list[str] = []
    seen_warning_ids: set[str] = set()
    if isinstance(warning_ids, list):
        for warning_id in warning_ids:
            if (
                isinstance(warning_id, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", warning_id)
                and warning_id not in seen_warning_ids
            ):
                normalized_warning_ids.append(warning_id)
                seen_warning_ids.add(warning_id)
    default["acknowledged_warning_ids"] = normalized_warning_ids
    recommendation = default.get("assessor_recommendation")
    default["assessor_recommendation"] = (
        recommendation
        if isinstance(recommendation, str) and recommendation in {"", "native", "ocvs", "hybrid"}
        else ""
    )
    rationale = default.get("assessor_recommendation_rationale")
    default["assessor_recommendation_rationale"] = (
        rationale.replace("\r\n", "\n").replace("\r", "\n").strip()[:4000].strip()
        if isinstance(rationale, str)
        else ""
    )
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
    if not isinstance(default.get("step4_hybrid_placements"), dict):
        default["step4_hybrid_placements"] = {}
    else:
        default["step4_hybrid_placements"] = {
            str(vm_name): normalize_hybrid_placement(value, "ocvs")
            for vm_name, value in default["step4_hybrid_placements"].items()
        }
    default["step4_iaas_discount_pct"] = _bounded_float(
        default.get("step4_iaas_discount_pct"),
        0.0,
        0.0,
        100.0,
    )
    default["step4_ocvs_profile"] = normalize_ocvs_profile(default.get("step4_ocvs_profile", "best_fit"))
    default["step4_ocvs_policy"] = normalize_ocvs_policy(default.get("step4_ocvs_policy", {}))
    default["step4_ocvs_commitment_term"] = normalize_ocvs_commitment_term(
        default.get("step4_ocvs_commitment_term", "payg")
    )
    default["step4_vmware_license_price_per_core_yearly"] = _bounded_float(
        default.get("step4_vmware_license_price_per_core_yearly"),
        0.0,
        0.0,
        1_000_000.0,
    )
    default["step4_ocvs_dr_nodes"] = normalize_ocvs_dr_nodes(default.get("step4_ocvs_dr_nodes", 0))
    default["step4_hybrid_ocvs_customized"] = (
        default.get("step4_hybrid_ocvs_customized") is True
    )
    default["step4_hybrid_ocvs_profile"] = normalize_ocvs_profile(
        default.get("step4_hybrid_ocvs_profile", default.get("step4_ocvs_profile", "best_fit"))
    )
    default["step4_hybrid_ocvs_policy"] = normalize_ocvs_policy(
        default.get("step4_hybrid_ocvs_policy", default.get("step4_ocvs_policy", {}))
    )
    default["step4_hybrid_ocvs_commitment_term"] = normalize_ocvs_commitment_term(
        default.get(
            "step4_hybrid_ocvs_commitment_term",
            default.get("step4_ocvs_commitment_term", "payg"),
        )
    )
    default["step4_hybrid_vmware_license_price_per_core_yearly"] = _bounded_float(
        default.get(
            "step4_hybrid_vmware_license_price_per_core_yearly",
            default.get("step4_vmware_license_price_per_core_yearly", 0.0),
        ),
        default.get("step4_vmware_license_price_per_core_yearly", 0.0),
        0.0,
        1_000_000.0,
    )
    default["step4_hybrid_ocvs_dr_nodes"] = normalize_ocvs_dr_nodes(
        default.get("step4_hybrid_ocvs_dr_nodes", default.get("step4_ocvs_dr_nodes", 0))
    )
    default.pop("step4_vmware_license_discount_pct", None)
    return default


def load_app_state() -> dict[str, Any]:
    """Load server-side app state so large selections don't live in cookies."""
    state_file = _state_file_path()
    if not state_file.exists():
        return normalize_app_state({})

    try:
        loaded = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        loaded = {}
    return normalize_app_state(loaded)


def save_app_state(state: dict[str, Any]) -> None:
    _write_json_atomically(_state_file_path(), state)


def _saved_assessments_dir() -> Path:
    if APP_STATE_DIR.is_symlink():
        raise PortableAssessmentError("The saved assessment root cannot be a symlink.")
    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    saved_dir = APP_STATE_DIR / "saved_assessments"
    if saved_dir.is_symlink():
        raise PortableAssessmentError("The saved assessment root cannot be a symlink.")
    saved_dir.mkdir(parents=True, exist_ok=True)
    if saved_dir.resolve().parent != APP_STATE_DIR.resolve():
        raise PortableAssessmentError("The saved assessment root is outside local storage.")
    return saved_dir


def normalize_assessment_name(value: Any) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    return clean[:120]


def normalize_assessment_notes(value: Any) -> str:
    clean = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return clean[:2000]


def _assessment_slug(value: Any) -> str:
    base = normalize_assessment_name(value).lower()
    slug = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    return (slug[:64].strip("_") or "assessment")


def _new_assessment_id(name: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{_assessment_slug(name)}_{timestamp}_{uuid4().hex[:8]}"


def _clean_assessment_id(value: Any) -> str:
    clean = str(value or "").strip()
    if not re.fullmatch(r"[a-z0-9_]{1,160}", clean):
        return ""
    return clean


def _saved_assessment_file_path(assessment_id: Any) -> Path | None:
    clean_id = _clean_assessment_id(assessment_id)
    if not clean_id:
        return None
    return _saved_assessments_dir() / f"{clean_id}.json"


def _assessment_default_name() -> str:
    customer_name = normalize_customer_name(session.get("customer_name", ""))
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if customer_name:
        return f"{customer_name} - {timestamp}"
    return f"Assessment - {timestamp}"


def _format_assessment_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Not saved"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw[:16]
    return parsed.strftime("%Y-%m-%d %H:%M")


def _read_saved_assessment(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def list_saved_assessments() -> list[dict[str, Any]]:
    saved_dir = _saved_assessments_dir()
    assessments: list[dict[str, Any]] = []
    for path in sorted(saved_dir.glob("*.json")):
        snapshot = _read_saved_assessment(path)
        assessment_id = _clean_assessment_id(snapshot.get("id") or path.stem)
        if not snapshot or not assessment_id:
            continue
        app_state = snapshot.get("app_state") if isinstance(snapshot.get("app_state"), dict) else {}
        import_summary = (
            snapshot.get("rvtools_import_summary") if isinstance(snapshot.get("rvtools_import_summary"), dict) else {}
        )
        selected_names = app_state.get("selected_vm_names") if isinstance(app_state.get("selected_vm_names"), list) else []
        try:
            vm_count = int(import_summary.get("vm_count") or len(selected_names) or 0)
        except (TypeError, ValueError):
            vm_count = len(selected_names)
        updated_at = str(snapshot.get("updated_at") or snapshot.get("saved_at") or "")
        assessments.append(
            {
                "id": assessment_id,
                "name": normalize_assessment_name(snapshot.get("name")) or path.stem,
                "notes": normalize_assessment_notes(snapshot.get("notes")),
                "customer_name": normalize_customer_name(snapshot.get("customer_name", "")),
                "saved_at": str(snapshot.get("saved_at") or ""),
                "updated_at": updated_at,
                "updated_at_display": _format_assessment_timestamp(updated_at),
                "selected_currency": str(snapshot.get("selected_currency") or "").upper().strip(),
                "selected_pricelist_file": str(snapshot.get("selected_pricelist_file") or ""),
                "selected_rvtools_file": str(snapshot.get("selected_rvtools_file") or ""),
                "vm_count": vm_count,
            }
        )
    return sorted(assessments, key=lambda item: str(item.get("updated_at") or ""), reverse=True)


def build_saved_assessment_snapshot(name: Any, notes: Any, assessment_id: str = "") -> dict[str, Any]:
    assessment_name = normalize_assessment_name(name) or _assessment_default_name()
    assessment_notes = normalize_assessment_notes(notes)
    clean_id = _clean_assessment_id(assessment_id)
    now = datetime.now().isoformat(timespec="seconds")
    saved_at = now
    if clean_id:
        existing_path = _saved_assessment_file_path(clean_id)
        if existing_path and existing_path.exists():
            existing = _read_saved_assessment(existing_path)
            saved_at = str(existing.get("saved_at") or now)
    else:
        clean_id = _new_assessment_id(assessment_name)

    return {
        "schema_version": SAVED_ASSESSMENT_SCHEMA_VERSION,
        "id": clean_id,
        "name": assessment_name,
        "notes": assessment_notes,
        "saved_at": saved_at,
        "updated_at": now,
        "customer_name": normalize_customer_name(session.get("customer_name", "")),
        "selected_currency": str(session.get("selected_currency", "") or "").upper().strip(),
        "selected_pricelist_file": str(session.get("selected_pricelist_file", "") or "").strip().replace("\\", "/"),
        "selected_rvtools_file": str(session.get("selected_rvtools_file", "") or "").strip().replace("\\", "/"),
        "rvtools_file_info": session.get("rvtools_file_info") if isinstance(session.get("rvtools_file_info"), dict) else {},
        "rvtools_import_summary": (
            session.get("rvtools_import_summary") if isinstance(session.get("rvtools_import_summary"), dict) else {}
        ),
        "app_state": load_app_state(),
        "step4_snapshot": load_step4_snapshot(),
        "last_export_file": str(session.get("last_export_file", "") or ""),
    }


def save_current_assessment(name: Any, notes: Any) -> dict[str, Any]:
    prior_session = copy.deepcopy(dict(session))
    try:
        active_id = _clean_assessment_id(session.get("active_assessment_id", ""))
        snapshot = build_saved_assessment_snapshot(name, notes, active_id)
        assessment_id = str(snapshot["id"])
        file_path = _saved_assessment_file_path(assessment_id)
        if file_path is None:
            raise ValueError("Assessment id is not valid.")
        _write_json_atomically(file_path, snapshot)
    except Exception:
        session.clear()
        session.update(prior_session)
        raise
    session["active_assessment_id"] = assessment_id
    session["active_assessment_name"] = snapshot["name"]
    session["active_assessment_notes"] = snapshot["notes"]
    return snapshot


def stage_saved_assessment_load(
    snapshot: dict[str, Any],
    file_path: Path,
    prior_session: dict[str, Any],
    prior_preferences: dict[str, Any],
) -> dict[str, Any]:
    warnings: list[str] = []
    staged_session = copy.deepcopy(prior_session)
    assessment_name = normalize_assessment_name(snapshot.get("name")) or file_path.stem
    assessment_notes = normalize_assessment_notes(snapshot.get("notes"))
    staged_session["active_assessment_id"] = _clean_assessment_id(snapshot.get("id") or file_path.stem)
    staged_session["active_assessment_name"] = assessment_name
    staged_session["active_assessment_notes"] = assessment_notes

    customer_name = normalize_customer_name(snapshot.get("customer_name", ""))
    if customer_name:
        staged_session["customer_name"] = customer_name
    else:
        staged_session.pop("customer_name", None)

    price_file = str(snapshot.get("selected_pricelist_file") or "").strip().replace("\\", "/")
    selected_currency = str(snapshot.get("selected_currency") or "").upper().strip()
    staged_session.pop("selected_pricelist_file", None)
    if selected_currency:
        staged_session["selected_currency"] = selected_currency

    staged_preferences = copy.deepcopy(prior_preferences)
    apply_price_preference = False
    if price_file:
        if price_file in list_downloaded_price_lists():
            staged_session["selected_pricelist_file"] = price_file
            staged_preferences["last_selected_pricelist_file"] = price_file
            if selected_currency:
                staged_preferences["last_selected_currency"] = selected_currency
            apply_price_preference = True
        else:
            warnings.append("Saved OCI price list is missing. Select or download a current price list before pricing.")

    selected_path = str(snapshot.get("selected_rvtools_file") or "").strip().replace("\\", "/")
    staged_session.pop("selected_rvtools_file", None)
    staged_session.pop("rvtools_file_info", None)
    staged_session.pop("rvtools_import_summary", None)
    staged_session.pop("rvtools_rejected_info", None)
    if selected_path:
        inventory_path = Path(selected_path)
        if not inventory_path.exists():
            warnings.append("Saved inventory file is missing. Re-select or recreate the inventory source before continuing.")
        else:
            try:
                vm_rows, source = load_vms_from_vinfo(selected_path)
                inventory_info = build_source_file_info(selected_path)
                inventory_summary = build_inventory_import_summary(vm_rows, source)
            except Exception:
                app.logger.exception("Saved assessment inventory staging failed")
                warnings.append("Saved inventory file could not be loaded. Re-select or recreate the inventory source.")
            else:
                staged_session["selected_rvtools_file"] = selected_path
                staged_session["rvtools_file_info"] = {
                    "file_path": selected_path,
                    "file_name": inventory_path.name,
                    "size_kb": inventory_info.get("size_kb", ""),
                }
                staged_session["rvtools_import_summary"] = inventory_summary

    last_export_file = str(snapshot.get("last_export_file") or "")
    if last_export_file:
        staged_session["last_export_file"] = last_export_file
    else:
        staged_session.pop("last_export_file", None)

    step4_snapshot = snapshot.get("step4_snapshot")
    return {
        "name": assessment_name,
        "warnings": warnings,
        "session": staged_session,
        "app_state": normalize_app_state(snapshot.get("app_state")),
        "step4_snapshot": copy.deepcopy(step4_snapshot) if isinstance(step4_snapshot, dict) else {},
        "preferences": staged_preferences,
        "apply_price_preference": apply_price_preference,
    }


def load_saved_assessment(
    assessment_id: Any,
    *,
    apply_preferences: bool = True,
) -> dict[str, Any]:
    file_path = _saved_assessment_file_path(assessment_id)
    if file_path is None or not file_path.exists():
        return {"ok": False, "message": "Saved assessment was not found.", "warnings": []}

    snapshot = _read_saved_assessment(file_path)
    if not snapshot:
        return {"ok": False, "message": "Saved assessment could not be read.", "warnings": []}

    prior_session = copy.deepcopy(dict(session))
    prior_app_state = load_app_state()
    prior_step4_snapshot = load_step4_snapshot()
    prior_preferences = load_preferences()

    try:
        staged = stage_saved_assessment_load(
            snapshot,
            file_path,
            prior_session,
            prior_preferences,
        )
        save_app_state(staged["app_state"])
        save_step4_snapshot(staged["step4_snapshot"])
        session.clear()
        session.update(copy.deepcopy(staged["session"]))
        if apply_preferences and staged["apply_price_preference"]:
            save_preferences(staged["preferences"])
    except Exception:
        app.logger.exception("Saved assessment transactional load failed")
        try:
            save_app_state(prior_app_state)
        except Exception:
            app.logger.exception("Saved assessment app state rollback failed")
        try:
            save_step4_snapshot(prior_step4_snapshot)
        except Exception:
            app.logger.exception("Saved assessment Step 4 rollback failed")
        session.clear()
        session.update(copy.deepcopy(prior_session))
        return {"ok": False, "message": "Saved assessment could not be loaded.", "warnings": []}

    return {
        "ok": True,
        "message": "Assessment loaded.",
        "name": staged["name"],
        "warnings": staged["warnings"],
    }


def delete_saved_assessment(assessment_id: Any) -> dict[str, Any]:
    file_path = _saved_assessment_file_path(assessment_id)
    if file_path is None or not file_path.exists():
        return {"ok": False, "message": "Saved assessment was not found."}
    snapshot = _read_saved_assessment(file_path)
    assessment_name = normalize_assessment_name(snapshot.get("name")) or file_path.stem
    try:
        file_path.unlink()
    except OSError:
        app.logger.exception("Saved assessment file deletion failed")
        return {"ok": False, "message": "Saved assessment could not be deleted."}
    if session.get("active_assessment_id") == file_path.stem:
        session.pop("active_assessment_id", None)
    return {"ok": True, "message": "Assessment deleted.", "name": assessment_name}


def reset_active_assessment_state() -> None:
    """Clear the active workspace while keeping the selected OCI pricing context."""
    keys_to_clear = (
        "customer_name",
        "selected_rvtools_file",
        "rvtools_file_info",
        "rvtools_import_summary",
        "rvtools_rejected_info",
        "active_assessment_id",
        "active_assessment_name",
        "active_assessment_notes",
        "last_export_file",
        "selected_vm_names",
        "step4_os_shapes",
    )
    for key in keys_to_clear:
        session.pop(key, None)
    save_app_state(_default_app_state())
    clear_step4_snapshot()


def build_current_portable_assessment(
    assessment_name: Any,
    assessment_notes: Any,
) -> tuple[dict[str, Any], str]:
    """Build a path-free package from the current server-side workspace."""
    active_id = _clean_assessment_id(session.get("active_assessment_id", ""))
    snapshot = build_saved_assessment_snapshot(
        assessment_name,
        assessment_notes,
        active_id,
    )

    selected_inventory = str(snapshot.get("selected_rvtools_file") or "").strip()
    inventory_rows: list[dict[str, Any]] = []
    inventory_source = ""
    if selected_inventory:
        try:
            inventory_rows, inventory_source = load_vms_from_vinfo(selected_inventory)
        except Exception as exc:
            raise PortableAssessmentError(
                "The selected inventory could not be read for portable export."
            ) from exc

    selected_pricing = str(snapshot.get("selected_pricelist_file") or "").strip()
    pricing_document: dict[str, Any] = {}
    if selected_pricing:
        try:
            loaded_pricing = json.loads(Path(selected_pricing).read_text(encoding="utf-8"))
        except Exception as exc:
            raise PortableAssessmentError(
                "The selected OCI price list could not be read for portable export."
            ) from exc
        if not isinstance(loaded_pricing, dict):
            raise PortableAssessmentError(
                "The selected OCI price list is not a valid JSON object."
            )
        pricing_document = loaded_pricing

    exported_at = datetime.now(timezone.utc)
    package = build_portable_package(
        snapshot,
        {
            "source_file_name": secure_filename(Path(selected_inventory).name)
            if selected_inventory
            else "",
            "source_label": (
                inventory_source.rsplit("::", 1)[-1]
                if "::" in inventory_source
                else ("Normalized VM inventory" if inventory_rows else "")
            ),
            "import_summary": (
                build_inventory_import_summary(inventory_rows, "Portable inventory")
                if inventory_rows
                else {}
            ),
            "rows": inventory_rows,
        },
        {
            "currency": str(snapshot.get("selected_currency") or "").upper().strip(),
            "source_file_name": secure_filename(Path(selected_pricing).name)
            if selected_pricing
            else "",
            "document": pricing_document,
        },
        exported_at=exported_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        source={
            "assessment_id": active_id,
            "application_schema_version": SAVED_ASSESSMENT_SCHEMA_VERSION,
        },
    )
    filename = build_export_filename(
        str(package["assessment"].get("name") or "assessment"),
        "portable_assessment",
        "json",
        exported_at.strftime("%Y%m%d_%H%M%S"),
    )
    return package, filename


def _next_imported_assessment_name(value: Any) -> str:
    base_name = normalize_assessment_name(value) or "Imported assessment"
    existing_names = {
        str(item.get("name") or "").strip().casefold()
        for item in list_saved_assessments()
    }
    if base_name.casefold() not in existing_names:
        return base_name
    suffix_index = 2
    while True:
        suffix = f" (Imported {suffix_index})"
        candidate = f"{base_name[: max(1, 120 - len(suffix))].rstrip()}{suffix}"
        if candidate.casefold() not in existing_names:
            return candidate
        suffix_index += 1


def _allocate_imported_assessment_paths(
    assessment_name: str,
) -> tuple[str, Path, Path]:
    imported_root = DOWNLOADS_DIR / "imported_assessments"
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    if DOWNLOADS_DIR.is_symlink() or imported_root.is_symlink():
        raise PortableAssessmentError("The imported assessment root cannot be a symlink.")
    if imported_root.exists() and not imported_root.is_dir():
        raise PortableAssessmentError("The imported assessment root is not a directory.")
    if imported_root.exists() and imported_root.resolve().parent != DOWNLOADS_DIR.resolve():
        raise PortableAssessmentError("The imported assessment root is outside local storage.")
    for _attempt in range(1000):
        assessment_id = _new_assessment_id(assessment_name)
        snapshot_file = _saved_assessment_file_path(assessment_id)
        if snapshot_file is None:
            continue
        import_dir = imported_root / assessment_id
        if not snapshot_file.exists() and not import_dir.exists():
            return assessment_id, import_dir, snapshot_file
    raise PortableAssessmentError(
        "A unique local assessment id could not be allocated for this import."
    )


def _write_imported_inventory(
    file_path: Path,
    inventory: dict[str, Any],
) -> None:
    _write_json_atomically(
        file_path,
        {
            "format": IMPORTED_INVENTORY_FORMAT,
            "schema_version": IMPORTED_INVENTORY_SCHEMA_VERSION,
            "inventory": inventory,
        },
    )


def _expected_portable_price_lookup(
    document: dict[str, Any],
) -> tuple[dict[str, float], str]:
    lookup: dict[str, float] = {}
    currency = ""
    for item in document.get("items", []):
        localizations = item["currencyCodeLocalizations"]
        localization = localizations[0]
        prices = localization["prices"]
        selected_price = next(
            (
                price
                for price in prices
                if str(price["model"]).upper() == "PAY_AS_YOU_GO"
            ),
            prices[0],
        )
        lookup[str(item["displayName"])] = float(selected_price["value"])
        if not currency:
            currency = str(localization["currencyCode"])
    return lookup, currency


def import_portable_assessment(package: Any) -> dict[str, Any]:
    """Materialize and load a validated package as a new local assessment."""
    validated = validate_portable_package(package)
    assessment = validated["assessment"]
    inventory = validated["inventory"]
    pricing = validated["pricing"]
    imported_name = _next_imported_assessment_name(assessment.get("name"))
    assessment_id, import_dir, snapshot_file = _allocate_imported_assessment_paths(
        imported_name
    )
    inventory_file = import_dir / "normalized_inventory.json"
    pricing_document = dict(pricing.get("document") or {})
    has_pricing = bool(pricing_document.get("items"))
    expected_price_lookup, expected_currency = _expected_portable_price_lookup(
        pricing_document
    )
    currency = expected_currency if has_pricing else ""
    pricing_file = import_dir / (
        f"oci_pricing_{currency or 'imported'}_portable.json"
    )
    prior_session = copy.deepcopy(dict(session))
    prior_state_id = str(prior_session.get("state_id") or "").strip()
    prior_state_file = (
        APP_STATE_DIR / f"{prior_state_id}.json" if prior_state_id else None
    )
    prior_step4_file = (
        APP_STATE_DIR / f"{prior_state_id}_step4_snapshot.json"
        if prior_state_id
        else None
    )
    prior_persistence = {
        path: _read_optional_file_bytes(path)
        for path in (prior_state_file, prior_step4_file)
        if path is not None
    }

    rows = list(inventory.get("rows") or [])
    now = datetime.now().isoformat(timespec="seconds")
    created_import_dir = False
    created_snapshot = False
    preference_write: tuple[tuple[bool, bytes], tuple[bool, bytes]] | None = None
    try:
        import_dir.mkdir(parents=True, exist_ok=False)
        created_import_dir = True
        _write_imported_inventory(inventory_file, inventory)
        if has_pricing:
            _write_json_atomically(pricing_file, pricing_document)
            pricing_path = str(pricing_file).replace("\\", "/")
            loaded_lookup, loaded_currency, loaded_source = load_price_lookup(
                pricing_path
            )
            if (
                not loaded_lookup
                or loaded_lookup != expected_price_lookup
                or loaded_currency.upper().strip() != currency
                or loaded_source != pricing_path
            ):
                raise PortableAssessmentError(
                    "The imported pricing could not be reconstructed exactly."
                )
        else:
            pricing_path = ""

        generated_rows: list[dict[str, Any]] = []
        generated_source = ""
        if rows:
            generated_rows, generated_source = load_vms_from_vinfo(
                str(inventory_file).replace("\\", "/")
            )
            if generated_rows != rows:
                raise PortableAssessmentError(
                    "The imported inventory could not be reconstructed exactly."
                )

        step4_snapshot = copy.deepcopy(assessment.get("step4_snapshot") or {})
        if rows:
            step4_snapshot["source_vinfo_csv"] = str(inventory_file).replace(
                "\\", "/"
            )
        else:
            step4_snapshot.pop("source_vinfo_csv", None)

        inventory_path = str(inventory_file).replace("\\", "/") if rows else ""
        snapshot = {
            "schema_version": SAVED_ASSESSMENT_SCHEMA_VERSION,
            "id": assessment_id,
            "name": imported_name,
            "notes": normalize_assessment_notes(assessment.get("notes")),
            "saved_at": now,
            "updated_at": now,
            "customer_name": normalize_customer_name(
                assessment.get("customer_name", "")
            ),
            "selected_currency": currency,
            "selected_pricelist_file": pricing_path,
            "selected_rvtools_file": inventory_path,
            "rvtools_file_info": (
                build_source_file_info(inventory_path) if inventory_path else {}
            ),
            "rvtools_import_summary": (
                build_inventory_import_summary(generated_rows, generated_source)
                if generated_rows
                else {}
            ),
            "app_state": normalize_app_state(assessment.get("app_state")),
            "step4_snapshot": step4_snapshot,
            "last_export_file": "",
        }
        _write_new_json_atomically(snapshot_file, snapshot)
        created_snapshot = True
        load_result = load_saved_assessment(
            assessment_id,
            apply_preferences=False,
        )
        if not load_result.get("ok"):
            raise PortableAssessmentError(
                "The imported assessment could not be loaded after reconstruction."
            )
        if has_pricing:
            preference_write = _update_preference_keys(
                {
                    "last_selected_pricelist_file": pricing_path,
                    "last_selected_currency": currency,
                },
                set(),
            )
        else:
            session.pop("selected_pricelist_file", None)
            session.pop("selected_currency", None)
            preference_write = _update_preference_keys(
                {},
                {
                    "last_selected_pricelist_file",
                    "last_selected_currency",
                },
            )
    except Exception:
        failed_state_id = str(session.get("state_id") or "").strip()
        if preference_write is not None:
            try:
                _restore_preferences_if_current(*preference_write)
            except OSError:
                app.logger.exception("Imported assessment preference rollback failed")
        try:
            for path, prior_value in prior_persistence.items():
                _restore_optional_file_bytes(path, prior_value)
            if failed_state_id and failed_state_id != prior_state_id:
                (APP_STATE_DIR / f"{failed_state_id}.json").unlink(
                    missing_ok=True
                )
                (APP_STATE_DIR / f"{failed_state_id}_step4_snapshot.json").unlink(
                    missing_ok=True
                )
        except OSError:
            app.logger.exception("Imported assessment persistence rollback failed")
        session.clear()
        session.update(copy.deepcopy(prior_session))
        if created_snapshot:
            try:
                snapshot_file.unlink(missing_ok=True)
            except OSError:
                app.logger.exception("Imported assessment snapshot cleanup failed")
        if created_import_dir:
            try:
                shutil.rmtree(import_dir, ignore_errors=True)
            except OSError:
                app.logger.exception("Imported assessment artifact cleanup failed")
        raise

    return {
        "ok": True,
        "id": assessment_id,
        "name": imported_name,
        "vm_count": len(rows),
        "currency": currency,
        "warnings": list(load_result.get("warnings") or []),
    }


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
    imported_root = DOWNLOADS_DIR / "imported_assessments"
    if not imported_root.is_symlink():
        files.extend(imported_root.glob("*/oci_pricing_*.json"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p).replace("\\", "/") for p in files]


def build_source_file_info(path_text: Any) -> dict[str, Any]:
    clean_path = str(path_text or "").strip().replace("\\", "/")
    path = Path(clean_path) if clean_path else None
    info: dict[str, Any] = {
        "file_path": clean_path,
        "file_name": path.name if path else "",
        "size_kb": "",
        "updated_at": "",
    }
    if path is None:
        return info
    try:
        file_stat = path.stat()
    except OSError:
        return info
    info["size_kb"] = round(file_stat.st_size / 1024, 2)
    info["updated_at"] = datetime.fromtimestamp(file_stat.st_mtime).strftime("%Y-%m-%d %H:%M")
    return info


def catalog_token_for_path(path_text: Any) -> str:
    normalized_path = str(path_text or "").strip().replace("\\", "/")
    digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:24]
    return f"catalog-{digest}"


def build_catalog_choices(paths: list[str], source_kind: str) -> list[dict[str, str]]:
    choices: list[dict[str, str]] = []
    for index, path_text in enumerate(paths):
        source_info = build_source_file_info(path_text)
        updated_at = str(source_info.get("updated_at") or "Date unavailable")
        display_index = index + 1
        if source_kind == "pricing":
            label = f"Saved price list {display_index} - {updated_at}"
        else:
            size_kb = source_info.get("size_kb")
            size_label = f"{size_kb} KB" if size_kb != "" else "Size unavailable"
            label = f"Saved inventory {display_index} - {updated_at} - {size_label}"
        choices.append(
            {
                "token": catalog_token_for_path(path_text),
                "file_name": str(source_info.get("file_name") or ""),
                "file_path": str(source_info.get("file_path") or ""),
                "label": label,
            }
        )
    return choices


def resolve_catalog_selection(submitted_value: Any, paths: list[str]) -> str:
    clean_value = str(submitted_value or "").strip().replace("\\", "/")
    if not clean_value:
        return ""
    normalized_paths = [str(path_text).strip().replace("\\", "/") for path_text in paths]
    if clean_value in normalized_paths:
        return clean_value
    if clean_value.startswith("catalog-"):
        if not re.fullmatch(r"catalog-[0-9a-f]{24}", clean_value):
            return ""
        matches = [path_text for path_text in normalized_paths if catalog_token_for_path(path_text) == clean_value]
        return matches[0] if len(matches) == 1 else ""
    matches = [path_text for path_text in normalized_paths if Path(path_text).name == clean_value]
    return matches[0] if len(matches) == 1 else ""


def find_downloaded_price_list_for_currency(currency_code: str) -> str:
    """Return newest downloaded OCI price list that matches the requested currency."""
    wanted = str(currency_code or "").upper().strip()
    if not wanted:
        return ""

    for file_path in list_downloaded_price_lists():
        path = Path(file_path)
        name_parts = path.stem.split("_")
        if len(name_parts) >= 3 and name_parts[2].upper() == wanted:
            return str(path).replace("\\", "/")

        _, loaded_currency, source_file = load_price_lookup(file_path)
        if loaded_currency.upper() == wanted:
            return source_file or str(path).replace("\\", "/")

    return ""


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


def _to_number(value: Any) -> float:
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _find_price_by_terms(
    price_lookup: dict[str, float],
    exact_name: str,
    required_terms: tuple[str, ...],
    excluded_terms: tuple[str, ...] = (),
) -> float:
    """Find an OCI unit price by exact display name, then by stable name terms."""
    if exact_name in price_lookup:
        return float(price_lookup.get(exact_name, 0.0))

    for display_name, value in price_lookup.items():
        normalized = str(display_name).lower()
        if all(term in normalized for term in required_terms) and not any(
            term in normalized for term in excluded_terms
        ):
            return float(value)
    return 0.0


def resolve_pricing_unit_prices(price_lookup: dict[str, float]) -> dict[str, float]:
    """Resolve shared unit prices used by native VM and OCVS costing."""
    return {
        "block_storage_unit_price": _find_price_by_terms(
            price_lookup,
            "Storage - Block Volume - Storage",
            ("block volume", "storage"),
            ("free",),
        ),
        "block_perf_unit_price": _find_price_by_terms(
            price_lookup,
            "Storage - Block Volume - Performance Units",
            ("block volume", "performance units"),
        ),
        "windows_os_unit_price": float(price_lookup.get("Compute - Windows OS", 0.0)),
    }


def normalize_burst_value(value: Any) -> str:
    burst = str(value or "100%").strip()
    if burst == "1:1":
        return "100%"
    return burst if burst in VALID_BURST_VALUES else "100%"


def build_vm_cost_row(
    vm: dict[str, Any],
    *,
    shape_options: list[str],
    shape_pricing_map: dict[str, dict[str, str]],
    price_lookup: dict[str, float],
    block_storage_unit_price: float,
    block_perf_unit_price: float,
    windows_os_unit_price: float,
    iaas_discount_pct: float,
    vm_shape_selection: dict[str, Any],
    vm_ocpu_selection: dict[str, Any],
    vm_burst_selection: dict[str, Any],
    vm_vpu_selection: dict[str, Any],
    vm_os_license_selection: dict[str, Any],
    valid_shape_values: set[str] | None = None,
    valid_vpu_values: set[int] | None = None,
) -> dict[str, Any]:
    vm_name = str(vm.get("name") or "").strip()
    cpu_val = int(_to_number(vm.get("cpus")))
    default_ocpu = max(1, cpu_val // 2)

    try:
        effective_ocpu = max(1, int(vm_ocpu_selection.get(vm_name, default_ocpu)))
    except (TypeError, ValueError):
        effective_ocpu = default_ocpu

    burst = normalize_burst_value(vm_burst_selection.get(vm_name, "100%"))
    burst_factor = float(BURST_FACTOR_MAP.get(burst, 1.0))

    valid_shape_values = valid_shape_values or set(shape_options)
    fallback_shape = shape_options[0] if shape_options else ""
    selected_shape = str(vm_shape_selection.get(vm_name, fallback_shape)).strip()
    if selected_shape not in valid_shape_values:
        selected_shape = fallback_shape

    valid_vpu_values = valid_vpu_values or set(VPU_OPTIONS)
    try:
        saved_vpu = int(vm_vpu_selection.get(vm_name, 10))
    except (TypeError, ValueError):
        saved_vpu = 10
    vpu_value = saved_vpu if saved_vpu in valid_vpu_values else 10

    raw_os_value = str(vm.get("raw_os") or "").strip()
    is_windows_server = "windows server" in raw_os_value.lower()
    os_license = ""
    if is_windows_server:
        saved_license = str(vm_os_license_selection.get(vm_name, "BYOL")).strip()
        os_license = saved_license if saved_license in OS_LICENSE_VALUES else "BYOL"

    shape_map = shape_pricing_map.get(selected_shape, {})
    ocpu_display = str(shape_map.get("ocpu_display_name", "")).strip()
    memory_display = str(shape_map.get("memory_display_name", "")).strip()
    ocpu_unit_price = float(price_lookup.get(ocpu_display, 0.0))
    memory_unit_price = float(price_lookup.get(memory_display, 0.0))

    memory_mb = int(_to_number(vm.get("memory_mb")))
    provisioned_mib = int(_to_number(vm.get("provisioned_mib")))
    memory_gb = int(math.ceil(memory_mb / 1024.0))
    raw_provisioned_gb = int(math.ceil(provisioned_mib / 1024.0))
    provisioned_gb = max(MIN_BLOCK_VOLUME_GB, raw_provisioned_gb)

    cpu_monthly_cost = effective_ocpu * ocpu_unit_price * HOURS_PER_MONTH * burst_factor
    ram_monthly_cost = memory_gb * memory_unit_price * HOURS_PER_MONTH
    cpu_ram_monthly_cost = cpu_monthly_cost + ram_monthly_cost
    storage_capacity_monthly_cost = provisioned_gb * block_storage_unit_price
    storage_performance_monthly_cost = provisioned_gb * vpu_value * block_perf_unit_price
    storage_monthly_cost = storage_capacity_monthly_cost + storage_performance_monthly_cost
    os_license_monthly_cost = (
        windows_os_unit_price * effective_ocpu * HOURS_PER_MONTH * burst_factor
    ) if os_license == "Lic Include" else 0.0

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
        "power_state": str(vm.get("power_state") or "").strip(),
        "is_windows_server": is_windows_server,
        "os_license": os_license,
        "cpus": cpu_val,
        "ocpu": effective_ocpu,
        "burst": burst,
        "memory_mb": memory_mb,
        "provisioned_mib": provisioned_mib,
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


def build_vm_cost_rows(
    vms: list[dict[str, Any]],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return [build_vm_cost_row(vm, **kwargs) for vm in vms]


def fetch_oci_price_list(currency_code: str) -> dict[str, Any]:
    """Fetch OCI list pricing from Oracle CE tools API for a specific currency."""
    params = urlencode({"currencyCode": currency_code})
    url = f"{OCI_PRODUCTS_API_BASE}?{params}"

    # Some enterprise networks/proxies reject requests with the default Python user-agent.
    # Use browser-like headers and retry transient network failures.
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) vmware-to-oci-migration-assessment/1.0",
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
                with urlopen(req, timeout=PRICE_LIST_DOWNLOAD_TIMEOUT_SECONDS, context=ctx) as response:
                    body = response.read().decode("utf-8")
                break
            except (URLError, TimeoutError, ConnectionResetError) as exc:
                last_network_exc = exc
                if attempt < 3:
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
            if name.startswith("~$") or name.startswith("."):
                continue
            file_path = root_path / name
            if file_path.suffix.lower() in SUPPORTED_RVTOOLS_EXTENSIONS:
                files.append(str(file_path).replace("\\", "/"))
    return sorted(files)


def cleanup_owned_inventory_candidate(
    candidate_path: Any,
    owned_candidate_path: Any,
    previously_active_path: Any,
) -> bool:
    candidate_text = str(candidate_path or "").strip().replace("\\", "/")
    owned_text = str(owned_candidate_path or "").strip().replace("\\", "/")
    active_text = str(previously_active_path or "").strip().replace("\\", "/")
    if not candidate_text or not owned_text:
        return False

    try:
        candidate = Path(candidate_text).resolve()
        owned_candidate = Path(owned_text).resolve()
        rvtools_root = RVTOOLS_DIR.resolve()
        active_candidate = Path(active_text).resolve() if active_text else None
        candidate.relative_to(rvtools_root)
    except (OSError, ValueError):
        return False

    if candidate != owned_candidate or candidate == active_candidate:
        return False
    if candidate.suffix.lower() not in SUPPORTED_RVTOOLS_EXTENSIONS:
        return False

    try:
        candidate.unlink(missing_ok=True)
    except OSError:
        app.logger.exception("Owned inventory candidate cleanup failed")
        return False
    return True


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_sha256(upload: Any) -> str:
    digest = hashlib.sha256()
    stream = upload.stream
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


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
    if selected.is_file() and selected.suffix.lower() == ".csv":
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
        "Could not locate a supported VM inventory CSV for the selected export. "
        "Please select a matching RVTools or VMwareInventory export file/folder."
    )


def _col_letters_to_index(col_letters: str) -> int:
    idx = 0
    for ch in col_letters:
        if "A" <= ch <= "Z":
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(idx - 1, 0)


VM_NAME_HEADERS = (
    "VM",
    "VM ID",
    "VM-ID",
    "VMID",
    "MOB ID",
    "VM Name",
    "Name",
    "Server Name",
    "Hostname",
    "Host Name",
    "Machine Name",
    "Virtual Machine",
    "Full Qualified Domain Name",
    "Fully Qualified Domain Name",
    "FQDN",
)
POWER_STATE_HEADERS = ("Powerstate", "PowerState", "Power State", "IsRunning", "Running", "State")
TEMPLATE_HEADERS = ("Template", "Is Template")
OS_HEADERS = (
    "OS according to the configuration file",
    "OS according to configuration file",
    "OS according to VMware Tools",
    "OS according to the VMware Tools",
    "Guest Version",
    "VM OS",
    "Guest OS",
    "Operating System",
    "OS",
)
CPU_HEADERS = ("CPUs", "CPU", "vCPU", "vCPUs", "Virtual CPU", "Virtual CPUs", "CPU Count", "# vCPU", "Cores", "Core")
MEMORY_MIB_HEADERS = ("Memory", "Memory MiB", "Memory MB", "Provisioned Memory (MiB)", "Provisioned Memory MB", "Size MiB")
MEMORY_GB_HEADERS = ("Memory GB", "Memory (GB)", "RAM GB", "RAM (GB)", "RAM", "Mem GB")
STORAGE_MIB_HEADERS = (
    "Provisioned MiB",
    "Provisioned MB",
    "Virtual Disk Size (MiB)",
    "Guest VM Disk Capacity (MiB)",
    "Provisioned Storage MiB",
    "Capacity MiB",
)
STORAGE_GB_HEADERS = (
    "Storage GB",
    "Storage (GB)",
    "Provisioned GB",
    "Provisioned Storage GB",
    "Disk GB",
    "Total Storage GB",
    "Storage",
    "Total Disk (GB)",
    "Total Disk GB",
    "Disk (GB)",
    "Disk GB",
)


def _normalize_header_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def _normalized_header_set(headers: tuple[str, ...]) -> set[str]:
    return {_normalize_header_name(header) for header in headers if str(header or "").strip()}


VM_NAME_HEADER_SET = _normalized_header_set(VM_NAME_HEADERS)
POWER_STATE_HEADER_SET = _normalized_header_set(POWER_STATE_HEADERS)
TEMPLATE_HEADER_SET = _normalized_header_set(TEMPLATE_HEADERS)
OS_HEADER_SET = _normalized_header_set(OS_HEADERS)
CPU_HEADER_SET = _normalized_header_set(CPU_HEADERS)
MEMORY_MIB_HEADER_SET = _normalized_header_set(MEMORY_MIB_HEADERS)
MEMORY_GB_HEADER_SET = _normalized_header_set(MEMORY_GB_HEADERS)
STORAGE_MIB_HEADER_SET = _normalized_header_set(STORAGE_MIB_HEADERS)
STORAGE_GB_HEADER_SET = _normalized_header_set(STORAGE_GB_HEADERS)


def _record_first_value(record: dict[str, Any], *headers: str) -> str:
    for header in headers:
        value = record.get(header)
        if value is not None and str(value).strip():
            return str(value).strip()

    normalized_lookup = {
        _normalize_header_name(key): value
        for key, value in record.items()
        if str(key or "").strip()
    }
    for header in headers:
        value = normalized_lookup.get(_normalize_header_name(header))
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _record_first_value_from_set(record: dict[str, Any], header_set: set[str]) -> str:
    for key, value in record.items():
        if _normalize_header_name(key) in header_set and value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _size_text_to_mib(value: Any, default_unit: str) -> str:
    text = str(value or "").strip().lower().replace("\xa0", " ")
    if not text:
        return ""

    compact = text.replace(" ", "")
    if "," in compact and "." not in compact:
        compact = compact.replace(",", ".")
    else:
        compact = compact.replace(",", "")

    match = re.search(r"-?\d+(?:\.\d+)?", compact)
    if not match:
        return ""

    try:
        number = float(match.group(0))
    except ValueError:
        return ""

    if number < 0:
        return ""

    if "tib" in compact or re.search(r"\btb\b", text):
        mib = number * 1024.0 * 1024.0
    elif "gib" in compact or re.search(r"\bgb\b", text):
        mib = number * 1024.0
    elif "mib" in compact or re.search(r"\bmb\b", text):
        mib = number
    elif default_unit == "gb":
        mib = number * 1024.0
    elif default_unit == "tb":
        mib = number * 1024.0 * 1024.0
    else:
        mib = number

    return str(int(math.ceil(mib)))


def _normalize_xlsx_target_path(target: str) -> str:
    normalized = str(target or "").replace("\\", "/").lstrip("/")
    while normalized.startswith("../"):
        normalized = normalized[3:]
    if normalized.startswith("xl/"):
        return normalized
    return f"xl/{normalized}"


def _read_xlsx_sheets(xlsx_path: Path) -> dict[str, list[dict[int, str]]]:
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

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst_xml = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sst_xml.findall("m:si", ns_main):
                text_parts = [t.text or "" for t in si.findall(".//m:t", ns_main)]
                shared_strings.append("".join(text_parts))

        def read_cell_value(cell: ET.Element) -> str:
            cell_type = cell.attrib.get("t", "")
            if cell_type == "inlineStr":
                text_node = cell.find("m:is/m:t", ns_main)
                return (text_node.text or "") if text_node is not None else ""

            value_node = cell.find("m:v", ns_main)
            raw = (value_node.text or "") if value_node is not None else ""
            if cell_type == "s" and raw.isdigit():
                idx = int(raw)
                if 0 <= idx < len(shared_strings):
                    return shared_strings[idx]
            return raw

        parsed_sheets: dict[str, list[dict[int, str]]] = {}
        sheets = workbook_xml.find("m:sheets", ns_main)
        if sheets is None:
            return parsed_sheets

        for sheet in sheets:
            original_name = (sheet.attrib.get("name") or "").strip()
            rel_id = sheet.attrib.get(f"{{{offdoc_rel_ns}}}id", "")
            sheet_target = rel_map.get(rel_id, "")
            sheet_xml_path = _normalize_xlsx_target_path(sheet_target)
            if not original_name or sheet_xml_path not in z.namelist():
                continue

            sheet_xml = ET.fromstring(z.read(sheet_xml_path))
            sheet_data = sheet_xml.find("m:sheetData", ns_main)
            rows: list[dict[int, str]] = []
            if sheet_data is None:
                parsed_sheets[original_name] = rows
                continue

            for row in sheet_data.findall("m:row", ns_main):
                data: dict[int, str] = {}
                for cell in row.findall("m:c", ns_main):
                    ref = cell.attrib.get("r", "")
                    col_letters = "".join(ch for ch in ref if ch.isalpha()).upper()
                    col_idx = _col_letters_to_index(col_letters) if col_letters else len(data)
                    data[col_idx] = read_cell_value(cell).strip()
                rows.append(data)
            parsed_sheets[original_name] = rows

        return parsed_sheets


def _records_from_sheet_rows(rows: list[dict[int, str]], header_row_idx: int = 0) -> list[dict[str, str]]:
    if header_row_idx >= len(rows):
        return []

    headers_by_idx = rows[header_row_idx]
    headers: dict[int, str] = {
        idx: value.strip() for idx, value in headers_by_idx.items() if value.strip()
    }
    if not headers:
        return []

    parsed: list[dict[str, str]] = []
    for row in rows[header_row_idx + 1 :]:
        record = {header: row.get(idx, "") for idx, header in headers.items()}
        if any(str(value or "").strip() for value in record.values()):
            parsed.append(record)
    return parsed


def _header_roles(headers: list[str]) -> set[str]:
    roles: set[str] = set()
    for header in headers:
        normalized = _normalize_header_name(header)
        if normalized in VM_NAME_HEADER_SET:
            roles.add("vm")
        if normalized in CPU_HEADER_SET:
            roles.add("cpu")
        if normalized in MEMORY_MIB_HEADER_SET or normalized in MEMORY_GB_HEADER_SET:
            roles.add("memory")
        if normalized in STORAGE_MIB_HEADER_SET or normalized in STORAGE_GB_HEADER_SET:
            roles.add("storage")
        if normalized in OS_HEADER_SET:
            roles.add("os")
        if normalized in POWER_STATE_HEADER_SET:
            roles.add("power")
    return roles


def _looks_like_generic_vm_header(row: dict[int, str]) -> bool:
    headers = [value for value in row.values() if str(value or "").strip()]
    roles = _header_roles(headers)
    return {"vm", "cpu", "memory", "storage"}.issubset(roles)


def _looks_like_oci_estimate_workbook(sheet_rows_by_name: dict[str, list[dict[int, str]]]) -> bool:
    for rows in sheet_rows_by_name.values():
        for row in rows[:25]:
            values = [str(value or "").strip() for value in row.values() if str(value or "").strip()]
            if not values:
                continue
            row_text = " ".join(values).lower()
            if "oracle investment proposal" in row_text or "oci cost estimator" in row_text:
                return True
            normalized_values = {_normalize_header_name(value) for value in values}
            if {"part", "description", "unit price"}.issubset(normalized_values) and (
                "monthly cost" in normalized_values or "total cost 12 months" in normalized_values
            ):
                return True
    return False


def _vm_name_from_record(record: dict[str, Any]) -> str:
    return _record_first_value_from_set(record, VM_NAME_HEADER_SET)


def _records_by_vm(records: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for record in records:
        vm_name = _vm_name_from_record(record)
        if vm_name and vm_name not in indexed:
            indexed[vm_name] = record
    return indexed


def _sum_storage_mib_by_vm(records: list[dict[str, str]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for record in records:
        vm_name = _vm_name_from_record(record)
        if not vm_name:
            continue
        raw_mib = _record_first_value_from_set(record, STORAGE_MIB_HEADER_SET)
        storage_mib = _size_text_to_mib(raw_mib, "mib") if raw_mib else ""
        if not storage_mib:
            raw_gb = _record_first_value_from_set(record, STORAGE_GB_HEADER_SET)
            storage_mib = _size_text_to_mib(raw_gb, "gb") if raw_gb else ""
        totals[vm_name] = totals.get(vm_name, 0) + int(_to_number(storage_mib))
    return totals


def _first_indexed_record_value(index: dict[str, dict[str, str]], vm_name: str, header_set: set[str]) -> str:
    record = index.get(vm_name)
    if not record:
        return ""
    return _record_first_value_from_set(record, header_set)


def _sheet_records_by_lower_name(sheet_rows_by_name: dict[str, list[dict[int, str]]]) -> dict[str, list[dict[str, str]]]:
    return {
        sheet_name.lower(): _records_from_sheet_rows(rows, 0)
        for sheet_name, rows in sheet_rows_by_name.items()
    }


def _enrich_with_rvtools_detail_sheets(
    records: list[dict[str, str]],
    sheet_rows_by_name: dict[str, list[dict[int, str]]],
) -> list[dict[str, str]]:
    records_by_sheet = _sheet_records_by_lower_name(sheet_rows_by_name)
    cpu_index = _records_by_vm(records_by_sheet.get("vcpu", []))
    memory_index = _records_by_vm(records_by_sheet.get("vmemory", []))
    disk_records = records_by_sheet.get("vdisk", [])
    disk_totals = _sum_storage_mib_by_vm(disk_records)

    os_indexes = [
        _records_by_vm(records_by_sheet.get(sheet_name, []))
        for sheet_name in ("vinfo", "vtools", "vcd")
    ]
    power_indexes = [
        _records_by_vm(records_by_sheet.get(sheet_name, []))
        for sheet_name in ("vinfo", "vcpu", "vmemory", "vdisk", "vtools", "vcd")
    ]

    if not records:
        vm_names = sorted(set(cpu_index) | set(memory_index) | set(disk_totals))
        records = [{"VM": vm_name} for vm_name in vm_names]

    enriched: list[dict[str, str]] = []
    for original_record in records:
        record = dict(original_record)
        vm_name = _vm_name_from_record(record)
        if not vm_name:
            continue

        if not _record_first_value_from_set(record, CPU_HEADER_SET):
            value = _first_indexed_record_value(cpu_index, vm_name, CPU_HEADER_SET)
            if value:
                record["CPUs"] = value

        if not _record_first_value_from_set(record, MEMORY_MIB_HEADER_SET):
            value = _first_indexed_record_value(memory_index, vm_name, MEMORY_MIB_HEADER_SET)
            if value:
                record["Memory MiB"] = value

        if not _record_first_value_from_set(record, STORAGE_MIB_HEADER_SET) and disk_totals.get(vm_name):
            record["Provisioned MiB"] = str(disk_totals[vm_name])

        if not _record_first_value_from_set(record, OS_HEADER_SET):
            for index in os_indexes:
                value = _first_indexed_record_value(index, vm_name, OS_HEADER_SET)
                if value:
                    record["OS according to the configuration file"] = value
                    break

        if not _record_first_value_from_set(record, POWER_STATE_HEADER_SET):
            for index in power_indexes:
                value = _first_indexed_record_value(index, vm_name, POWER_STATE_HEADER_SET)
                if value:
                    record["Powerstate"] = value
                    break

        if not _record_first_value_from_set(record, TEMPLATE_HEADER_SET):
            value = _first_indexed_record_value(cpu_index, vm_name, TEMPLATE_HEADER_SET)
            if value:
                record["Template"] = value

        enriched.append(record)

    return enriched


def _find_generic_inventory_records(
    sheet_rows_by_name: dict[str, list[dict[int, str]]],
) -> tuple[list[dict[str, str]], str] | None:
    candidates: list[tuple[int, int, str, int, list[dict[str, str]]]] = []
    for sheet_name, rows in sheet_rows_by_name.items():
        for header_row_idx, row in enumerate(rows[:30]):
            if not _looks_like_generic_vm_header(row):
                continue
            records = _records_from_sheet_rows(rows, header_row_idx)
            vm_record_count = sum(1 for record in records if _vm_name_from_record(record))
            if not vm_record_count:
                continue
            normalized_sheet_name = _normalize_header_name(sheet_name)
            name_score = 0
            if "pivot" in normalized_sheet_name:
                name_score -= 100
            if "data vm" in normalized_sheet_name or normalized_sheet_name in {"vms", "virtual machines"}:
                name_score += 40
            elif "vm" in normalized_sheet_name:
                name_score += 20
            if "mssql" in normalized_sheet_name or "sql" in normalized_sheet_name:
                name_score -= 5
            candidates.append((name_score, vm_record_count, sheet_name, header_row_idx, records))

    if not candidates:
        return None

    _, _, sheet_name, header_row_idx, records = max(candidates, key=lambda item: (item[0], item[1]))
    return records, f"{sheet_name} row {header_row_idx + 1}"


def _find_partial_inventory_diagnostic(sheet_rows_by_name: dict[str, list[dict[int, str]]]) -> str:
    for sheet_name, rows in sheet_rows_by_name.items():
        for header_row_idx, row in enumerate(rows[:30]):
            headers = [value for value in row.values() if str(value or "").strip()]
            roles = _header_roles(headers)
            if "vm" not in roles:
                continue

            missing: list[str] = []
            if "cpu" not in roles:
                missing.append("vCPU")
            if "memory" not in roles:
                missing.append("RAM")
            if "storage" not in roles:
                missing.append("storage")

            if missing:
                return (
                    f"The workbook looks like a VM list or workload categorization file on sheet "
                    f"'{sheet_name}' row {header_row_idx + 1}, but it is missing required sizing columns: "
                    f"{', '.join(missing)}. Upload this as supplementary categorization later, or use a VM inventory "
                    "with VM name, vCPU, RAM, storage, and OS columns for sizing."
                )
    return ""


def _find_aggregate_capacity_diagnostic(sheet_rows_by_name: dict[str, list[dict[int, str]]]) -> str:
    for sheet_name, rows in sheet_rows_by_name.items():
        for header_row_idx, row in enumerate(rows[:30]):
            normalized_values = {
                _normalize_header_name(value)
                for value in row.values()
                if str(value or "").strip()
            }
            if "environment" in normalized_values and "core" in normalized_values and "memory" in normalized_values and "storage" in normalized_values:
                return (
                    f"The workbook looks like an aggregate infrastructure capacity assessment on sheet "
                    f"'{sheet_name}'. It contains environment-level cores, RAM, and storage totals, but not "
                    "per-VM rows. Use it as advisory input, or upload VM-level inventory for sizing and "
                    "migration-path analysis."
                )
    return ""


def parse_vinfo_from_xlsx(xlsx_path: Path) -> tuple[list[dict[str, str]], str]:
    """Parse VM inventory records from RVTools, VMwareInventory, or generic VM inventory sheets."""
    sheet_rows_by_name = _read_xlsx_sheets(xlsx_path)
    if not sheet_rows_by_name:
        raise ValueError("The selected workbook does not contain readable worksheets.")

    lower_to_name = {sheet_name.lower(): sheet_name for sheet_name in sheet_rows_by_name}

    if "vinfo" in lower_to_name:
        source_sheet = lower_to_name["vinfo"]
        records = _records_from_sheet_rows(sheet_rows_by_name[source_sheet], 0)
        records = _enrich_with_rvtools_detail_sheets(records, sheet_rows_by_name)
        return records, f"{source_sheet} + RVTools detail tabs"

    for candidate in ("vms", "virtual machines"):
        if candidate in lower_to_name:
            source_sheet = lower_to_name[candidate]
            records = _records_from_sheet_rows(sheet_rows_by_name[source_sheet], 0)
            return records, source_sheet

    rvtools_detail_sheets = {"vcpu", "vmemory", "vdisk"}
    if rvtools_detail_sheets.issubset(set(lower_to_name)):
        records = _enrich_with_rvtools_detail_sheets([], sheet_rows_by_name)
        if records:
            return records, "RVTools detail tabs"

    generic_match = _find_generic_inventory_records(sheet_rows_by_name)
    if generic_match:
        return generic_match

    partial_inventory_message = _find_partial_inventory_diagnostic(sheet_rows_by_name)
    if partial_inventory_message:
        raise ValueError(partial_inventory_message)

    aggregate_capacity_message = _find_aggregate_capacity_diagnostic(sheet_rows_by_name)
    if aggregate_capacity_message:
        raise ValueError(aggregate_capacity_message)

    if _looks_like_oci_estimate_workbook(sheet_rows_by_name):
        raise ValueError(
            "The selected workbook appears to be an OCI pricing estimate, not a VM-level inventory. "
            "Please upload an RVTools export or a spreadsheet with VM name, vCPU, RAM, storage, and OS columns."
        )

    raise ValueError(
        "Could not find a VM-level inventory table. Supported workbooks need RVTools tabs "
        "(vInfo, or vCPU/vMemory/vDisk) or a table with VM name, vCPU, RAM, storage, and OS columns."
    )


def _load_imported_normalized_inventory(
    selected: Path,
) -> tuple[list[dict[str, Any]], str]:
    imported_root_path = DOWNLOADS_DIR / "imported_assessments"
    if DOWNLOADS_DIR.is_symlink() or imported_root_path.is_symlink():
        raise ValueError("The imported normalized inventory root is unsafe.")
    imported_root = imported_root_path.resolve()
    try:
        resolved = selected.resolve(strict=True)
    except OSError as exc:
        raise ValueError("The imported normalized inventory file is missing.") from exc
    if (
        resolved.name != "normalized_inventory.json"
        or imported_root not in resolved.parents
    ):
        raise ValueError("JSON inventory is only supported for generated imports.")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("The imported normalized inventory file is invalid.") from exc
    if not isinstance(payload, dict):
        raise ValueError("The imported normalized inventory file is invalid.")
    if (
        payload.get("format") != IMPORTED_INVENTORY_FORMAT
        or payload.get("schema_version") != IMPORTED_INVENTORY_SCHEMA_VERSION
    ):
        raise ValueError("The imported normalized inventory format is unsupported.")
    canonical = build_portable_package(
        {},
        payload.get("inventory", {}),
        {},
        exported_at="1970-01-01T00:00:00Z",
    )
    rows = canonical["inventory"]["rows"]
    _validate_loaded_inventory_rows(rows)
    source_path = str(selected).replace("\\", "/")
    source = f"{source_path}::Portable normalized inventory"
    return rows, source


def load_vms_from_vinfo(selected_path: str) -> tuple[list[dict[str, Any]], str]:
    """Load VM rows from a supported RVTools or VMwareInventory export."""
    selected = Path(selected_path)
    mapping_config = load_os_mapping_config()

    if selected.suffix.lower() == ".json":
        return _load_imported_normalized_inventory(selected)

    def _build_vm_rows(records: list[dict[str, str]]) -> list[dict[str, Any]]:
        def _first_value(rec: dict[str, str], *keys: str) -> str:
            return _record_first_value(rec, *keys)

        def _to_short_power_state(raw_value: str) -> str:
            value = str(raw_value or "").strip().lower().replace(" ", "")
            if value in {"poweredon", "on", "running", "true", "yes", "1"}:
                return "On"
            if not value:
                return "Unknown"
            return "Off"

        parsed_rows: list[dict[str, Any]] = []
        seen_names: dict[str, int] = {}
        for rec in records:
            # Prefer VM name; if blank, use VM/MOB ID from alternate inventory exports.
            source_vm_name = _first_value(rec, "VM", "VM ID", "VM-ID", "VMID", "MOB ID", *VM_NAME_HEADERS)
            if not source_vm_name:
                continue
            occurrence = seen_names.get(source_vm_name, 0) + 1
            seen_names[source_vm_name] = occurrence
            vm_name = source_vm_name if occurrence == 1 else f"{source_vm_name} [{occurrence}]"
            raw_os = _first_value(
                rec,
                *OS_HEADERS,
            )
            power_state_raw = _first_value(rec, *POWER_STATE_HEADERS)
            cpus_raw = _first_value(rec, *CPU_HEADERS)

            mem_mib_raw = _record_first_value_from_set(rec, MEMORY_MIB_HEADER_SET)
            mem_gb_raw = _record_first_value_from_set(rec, MEMORY_GB_HEADER_SET)
            mem_raw = _size_text_to_mib(mem_mib_raw, "mib") if mem_mib_raw else ""
            if not mem_raw and mem_gb_raw:
                mem_raw = _size_text_to_mib(mem_gb_raw, "gb")

            provisioned_mib_raw = _record_first_value_from_set(rec, STORAGE_MIB_HEADER_SET)
            provisioned_gb_raw = _record_first_value_from_set(rec, STORAGE_GB_HEADER_SET)
            provisioned_mib = _size_text_to_mib(provisioned_mib_raw, "mib") if provisioned_mib_raw else ""
            if not provisioned_mib and provisioned_gb_raw:
                provisioned_mib = _size_text_to_mib(provisioned_gb_raw, "gb")

            parsed_rows.append(
                {
                    "name": vm_name,
                    "source_name": source_vm_name,
                    "duplicate_index": occurrence,
                    "power_state": _to_short_power_state(power_state_raw),
                    "raw_os": raw_os,
                    "mapped_os": map_os_name(raw_os, mapping_config),
                    "cpus": cpus_raw,
                    "memory_mb": mem_raw,
                    "provisioned_mib": provisioned_mib,
                }
            )
        return parsed_rows

    # If a workbook was selected, parse a supported VM inventory sheet directly.
    if selected.suffix.lower() in {".xlsx", ".xlsm"}:
        records, source_sheet = parse_vinfo_from_xlsx(selected)
        selected_path = str(selected).replace("\\", "/")
        rows = _build_vm_rows(records)
        _validate_loaded_inventory_rows(rows)
        return rows, f"{selected_path}::{source_sheet}"

    vinfo_csv = resolve_vinfo_csv(selected_path)

    records: list[dict[str, str]] = []
    last_exc: Exception | None = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with vinfo_csv.open("r", encoding=enc, newline="") as f:
                sample = f.read(8192)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.DictReader(f, dialect=dialect)
                records = [dict(r) for r in reader]
            break
        except UnicodeDecodeError as exc:
            last_exc = exc

    if not records and last_exc is not None:
        raise last_exc

    rows = _build_vm_rows(records)
    _validate_loaded_inventory_rows(rows)
    return rows, str(vinfo_csv).replace("\\", "/")


def _is_empty_or_zero(value: Any) -> bool:
    return _to_number(value) <= 0.0


def _is_unknown_os(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"", "n/a", "na", "nan", "none", "unknown", "unknown / empty"}


def _validate_loaded_inventory_rows(vm_rows: list[dict[str, Any]]) -> None:
    if not vm_rows:
        raise ValueError("No VM rows were found in the selected inventory.")

    if all(_is_empty_or_zero(row.get("cpus")) for row in vm_rows):
        raise ValueError("No usable vCPU values were found. Check the inventory CPU/vCPU column mapping.")
    if all(_is_empty_or_zero(row.get("memory_mb")) for row in vm_rows):
        raise ValueError("No usable RAM values were found. Check the inventory memory column and units.")
    if all(_is_empty_or_zero(row.get("provisioned_mib")) for row in vm_rows):
        raise ValueError("No usable storage values were found. Check the inventory storage column and units.")


def build_inventory_import_summary(vm_rows: list[dict[str, Any]], source: str) -> dict[str, Any]:
    source_name_counts: dict[str, int] = {}
    for row in vm_rows:
        source_name = str(row.get("source_name") or row.get("name") or "").strip()
        if source_name:
            source_name_counts[source_name] = source_name_counts.get(source_name, 0) + 1

    duplicate_name_count = sum(1 for count in source_name_counts.values() if count > 1)
    duplicate_row_count = sum(max(0, count - 1) for count in source_name_counts.values())
    missing_cpu_count = sum(1 for row in vm_rows if _is_empty_or_zero(row.get("cpus")))
    missing_memory_count = sum(1 for row in vm_rows if _is_empty_or_zero(row.get("memory_mb")))
    missing_storage_count = sum(1 for row in vm_rows if _is_empty_or_zero(row.get("provisioned_mib")))
    unknown_os_count = sum(1 for row in vm_rows if _is_unknown_os(row.get("raw_os")))
    unknown_power_count = sum(1 for row in vm_rows if str(row.get("power_state") or "").strip().lower() == "unknown")

    warning_messages: list[str] = []
    if duplicate_row_count:
        warning_messages.append(
            f"{duplicate_row_count:,} duplicate VM name row(s) detected across {duplicate_name_count:,} VM name(s); duplicates were kept with a numeric suffix."
        )
    if missing_cpu_count:
        warning_messages.append(f"{missing_cpu_count:,} VM row(s) have missing or zero vCPU.")
    if missing_memory_count:
        warning_messages.append(f"{missing_memory_count:,} VM row(s) have missing or zero RAM.")
    if missing_storage_count:
        warning_messages.append(
            f"{missing_storage_count:,} VM row(s) have missing or zero storage; OCI Native costing applies the minimum block volume size when selected."
        )
    if unknown_os_count:
        warning_messages.append(f"{unknown_os_count:,} VM row(s) have missing or unknown OS.")
    if unknown_power_count:
        warning_messages.append(f"{unknown_power_count:,} VM row(s) have unknown power state because the source did not provide it.")

    total_memory_mb = int(sum(_to_number(row.get("memory_mb")) for row in vm_rows))
    total_storage_mib = int(sum(_to_number(row.get("provisioned_mib")) for row in vm_rows))

    return {
        "source": source,
        "vm_count": len(vm_rows),
        "total_vcpus": int(sum(_to_number(row.get("cpus")) for row in vm_rows)),
        "total_memory_gb": int(math.ceil(total_memory_mb / 1024.0)) if total_memory_mb else 0,
        "total_storage_gb": int(math.ceil(total_storage_mib / 1024.0)) if total_storage_mib else 0,
        "unknown_power_count": unknown_power_count,
        "unknown_os_count": unknown_os_count,
        "missing_cpu_count": missing_cpu_count,
        "missing_memory_count": missing_memory_count,
        "missing_storage_count": missing_storage_count,
        "duplicate_name_count": duplicate_name_count,
        "duplicate_row_count": duplicate_row_count,
        "warning_messages": warning_messages,
    }


def _inventory_review_row(
    row: dict[str, Any],
    detected_value: str,
    issue: str,
    recommendation: str,
    action: str,
) -> dict[str, str]:
    return {
        "vm_name": str(row.get("name") or row.get("source_name") or "Unknown VM"),
        "detected_value": detected_value,
        "issue": issue,
        "reason": issue,
        "recommendation": recommendation,
        "action": action,
    }


def build_inventory_review_issues(vm_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add_issue(
        issue_id: str,
        title: str,
        detail: str,
        severity: str,
        rows: list[dict[str, str]],
        default_action: str,
    ) -> None:
        if not rows:
            return
        issues.append(
            {
                "id": issue_id,
                "title": title,
                "detail": detail,
                "severity": severity,
                "count": len(rows),
                "default_action": default_action,
                "vm_names": [row["vm_name"] for row in rows],
                "vm_rows_by_name": {row["vm_name"]: row for row in rows},
                "vm_rows": rows[:50],
                "hidden_count": max(0, len(rows) - 50),
            }
        )

    supported_signatures = load_supported_os_signatures()
    if supported_signatures:
        unsupported_rows = [
            _inventory_review_row(
                row,
                str(row.get("raw_os") or "Unknown / Empty"),
                "Native migration requires a documented remediation treatment",
                "Keep the VM in scope on OCVS or remediate the guest OS before Native migration.",
                "Review Native treatment",
            )
            for row in vm_rows
            if not _is_unknown_os(row.get("raw_os"))
            and not is_oci_supported_os(str(row.get("raw_os") or ""), supported_signatures)
        ]
        add_issue(
            "unsupported-native",
            "Unsupported for OCI Native",
            "These VMs remain in scope but require remediation review before using a Native placement.",
            "advisory",
            unsupported_rows,
            "Review Native treatment",
    )

    missing_storage_rows = [
        _inventory_review_row(
            row,
            "Empty / 0 storage",
            "Missing storage value",
            "Review the affected VM before relying on storage sizing results.",
            "Review storage inputs",
        )
        for row in vm_rows
        if _is_empty_or_zero(row.get("provisioned_mib"))
    ]
    add_issue(
        "missing-storage",
        "Missing storage values",
        "Some VMs have empty or zero storage values. Sizing can continue, but storage estimates should be confirmed.",
        "advisory",
        missing_storage_rows,
        "Review storage inputs",
    )

    missing_cpu_rows = [
        _inventory_review_row(
            row,
            "Empty / 0 vCPU",
            "Missing vCPU value",
            "Review the affected VM before relying on compute shape sizing.",
            "Review vCPU inputs",
        )
        for row in vm_rows
        if _is_empty_or_zero(row.get("cpus"))
    ]
    add_issue(
        "missing-cpu",
        "Missing vCPU values",
        "Some VMs have empty or zero vCPU values. Sizing can continue, but compute estimates should be confirmed.",
        "advisory",
        missing_cpu_rows,
        "Review CPU inputs",
    )

    missing_memory_rows = [
        _inventory_review_row(
            row,
            "Empty / 0 RAM",
            "Missing RAM value",
            "Review the affected VM before relying on memory sizing.",
            "Review RAM inputs",
        )
        for row in vm_rows
        if _is_empty_or_zero(row.get("memory_mb"))
    ]
    add_issue(
        "missing-memory",
        "Missing RAM values",
        "Some VMs have empty or zero RAM values. Sizing can continue, but memory estimates should be confirmed.",
        "advisory",
        missing_memory_rows,
        "Review RAM inputs",
    )

    unknown_os_rows = [
        _inventory_review_row(
            row,
            str(row.get("raw_os") or "Unknown / Empty"),
            "Unknown OS",
            "Confirm guest OS so OCI Native support and Hybrid placement are accurate.",
            "Review OS",
        )
        for row in vm_rows
        if _is_unknown_os(row.get("raw_os"))
    ]
    add_issue(
        "unknown-os",
        "Unknown OS values",
        "Unknown operating systems require manual review before final target placement.",
        "advisory",
        unknown_os_rows,
        "Review OS values",
    )

    source_name_counts: dict[str, int] = {}
    for row in vm_rows:
        source_name = str(row.get("source_name") or row.get("name") or "").strip()
        if source_name:
            source_name_counts[source_name] = source_name_counts.get(source_name, 0) + 1
    duplicate_rows = [
        _inventory_review_row(
            row,
            str(row.get("source_name") or row.get("name") or "Unknown VM"),
            "Duplicate VM name",
            "Reconcile the source records and keep the intended workload row in scope.",
            "Review source record",
        )
        for row in vm_rows
        if source_name_counts.get(str(row.get("source_name") or row.get("name") or "").strip(), 0) > 1
    ]
    add_issue(
        "duplicate-vm-name",
        "Duplicate VM names",
        "Duplicate source VM names were kept with suffixes and should be reviewed before export.",
        "advisory",
        duplicate_rows,
        "Review duplicates",
    )

    return issues


def build_inventory_review_issues_from_path(selected_path: Any) -> list[dict[str, Any]]:
    clean_path = str(selected_path or "").strip()
    if not clean_path:
        return []
    try:
        vm_rows, _source = load_vms_from_vinfo(clean_path)
    except Exception:
        return []
    return build_inventory_review_issues(vm_rows)


def inventory_placement_field_name(prefix: str, vm_name: str) -> str:
    return f"{prefix}:{urlencode({'': vm_name})[1:]}"


def default_inventory_placement(vm: dict[str, Any], supported_signatures: list[str]) -> str:
    raw_os = str(vm.get("raw_os") or "")
    if _is_unknown_os(raw_os) or not supported_signatures:
        return "review"
    return "native" if is_oci_supported_os(raw_os, supported_signatures) else "ocvs"


def parse_exact_placement_fields(
    form: Any,
    prefix: str,
    expected_vm_names: list[str],
    known_vm_names: list[str],
    missing_defaults: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    expected_set = set(expected_vm_names)
    known_fields = {
        inventory_placement_field_name(prefix, vm_name): vm_name
        for vm_name in known_vm_names
    }
    submitted_fields = [str(key) for key in form.keys() if str(key).startswith(f"{prefix}:")]
    parsed: dict[str, str] = {}
    errors: list[str] = []
    field_errors: dict[str, str] = {}

    def add_error(message: str) -> None:
        if message not in errors:
            errors.append(message)

    for field_name in submitted_fields:
        vm_name = known_fields.get(field_name)
        if vm_name is None:
            add_error("A placement was submitted for an unknown VM.")
            continue
        values = form.getlist(field_name)
        if len(values) != 1:
            message = "Choose exactly one placement for every included VM."
            field_errors[vm_name] = message
            add_error(message)
            continue
        placement = str(values[0]).strip().lower()
        if placement not in HYBRID_PLACEMENT_VALUES:
            message = "Choose a valid placement: OCI Native, OCVS, or Review."
            field_errors[vm_name] = message
            add_error(message)
            continue
        parsed[vm_name] = placement

    outside_scope = set(parsed) - expected_set
    if outside_scope:
        add_error("Placements may only be submitted for included VMs.")
    missing = expected_set - set(parsed)
    if missing_defaults is not None:
        for vm_name in missing:
            default_placement = str(missing_defaults.get(vm_name, "")).strip().lower()
            if default_placement in HYBRID_PLACEMENT_VALUES:
                parsed[vm_name] = default_placement
        missing = expected_set - set(parsed)
    if missing:
        add_error("Choose a valid placement for every included VM.")
        for vm_name in missing:
            field_errors.setdefault(vm_name, "Choose a placement for this included VM.")

    return (
        {vm_name: parsed[vm_name] for vm_name in expected_vm_names if vm_name in parsed},
        errors,
        field_errors,
    )


def inventory_review_readiness_errors(
    all_vms: list[dict[str, Any]],
    state: dict[str, Any],
    inventory_issues: list[dict[str, Any]] | None = None,
) -> list[str]:
    vm_names = [str(vm.get("name") or "") for vm in all_vms]
    vm_name_set = set(vm_names)
    selected_value = state.get("selected_vm_names")
    selected_names = selected_value if isinstance(selected_value, list) else []
    errors: list[str] = []

    if not selected_names:
        errors.append("Include at least one VM before continuing to scenarios.")
    elif (
        any(not isinstance(name, str) or name not in vm_name_set for name in selected_names)
        or len(selected_names) != len(set(selected_names))
    ):
        errors.append("Return to Inventory Review and save a valid VM selection before continuing.")

    placements = state.get("step4_hybrid_placements")
    if not isinstance(placements, dict):
        placements = {}
    selected_set = set(selected_names)
    if (
        set(placements) != selected_set
        or any(str(value).strip().lower() not in HYBRID_PLACEMENT_VALUES for value in placements.values())
    ):
        errors.append("Choose a valid placement for every included VM before continuing.")

    issues = inventory_issues if inventory_issues is not None else build_inventory_review_issues(all_vms)
    if any(issue.get("severity") == "critical" for issue in issues):
        errors.append("Resolve critical inventory issues in Setup or the source inventory before continuing.")

    return errors


class SetupFieldError(ValueError):
    def __init__(self, field_id: str, message: str) -> None:
        super().__init__(message)
        self.field_id = field_id


def _parse_manual_sizing_int(form_key: str, label: str) -> int:
    raw_value = str(request.form.get(form_key, "")).strip()
    try:
        parsed = int(float(raw_value))
    except (TypeError, ValueError):
        raise SetupFieldError(form_key, f"{label} must be a whole number.")
    if parsed < 0:
        raise SetupFieldError(form_key, f"{label} cannot be negative.")
    return parsed


def _distribute_integer_total(total: int, count: int) -> list[int]:
    if count <= 0:
        return []
    base = total // count
    remainder = total % count
    return [base + (1 if idx < remainder else 0) for idx in range(count)]


def create_manual_inventory_csv_from_form() -> tuple[Path, list[str]]:
    vm_count = _parse_manual_sizing_int("manual_vm_count", "VM count")
    total_vcpus = _parse_manual_sizing_int("manual_total_vcpus", "Total vCPU")
    total_memory_gb = _parse_manual_sizing_int("manual_total_memory_gb", "Total RAM GB")
    total_storage_gb = _parse_manual_sizing_int("manual_total_storage_gb", "Total storage GB")
    supported_count = _parse_manual_sizing_int("manual_supported_vm_count", "OCI-supported VM count")
    unsupported_count = _parse_manual_sizing_int("manual_unsupported_vm_count", "Unsupported/legacy VM count")

    if vm_count <= 0:
        raise SetupFieldError("manual_vm_count", "VM count must be greater than zero.")
    if supported_count + unsupported_count != vm_count:
        raise SetupFieldError(
            "manual_supported_vm_count",
            "Manual sizing counts must add up to the VM count.",
        )
    if total_vcpus < vm_count or total_memory_gb < vm_count or total_storage_gb < vm_count:
        if total_vcpus < vm_count:
            field_id = "manual_total_vcpus"
        elif total_memory_gb < vm_count:
            field_id = "manual_total_memory_gb"
        else:
            field_id = "manual_total_storage_gb"
        raise SetupFieldError(
            field_id,
            "Total vCPU, RAM GB, and storage GB must each be at least the VM count.",
        )

    cpu_values = _distribute_integer_total(total_vcpus, vm_count)
    memory_gb_values = _distribute_integer_total(total_memory_gb, vm_count)
    storage_gb_values = _distribute_integer_total(total_storage_gb, vm_count)
    os_values = ["Oracle Linux 8 (64-bit)"] * supported_count + ["Solaris 11.4 (64-bit)"] * unsupported_count

    manual_dir = RVTOOLS_DIR / "manual"
    manual_dir.mkdir(parents=True, exist_ok=True)
    file_path = manual_dir / f"manual_inventory_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}.csv"
    vm_names: list[str] = []
    headers = [
        "VM",
        "Powerstate",
        "Template",
        "OS according to the configuration file",
        "CPUs",
        "Memory",
        "Provisioned MiB",
    ]
    with file_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for idx in range(vm_count):
            vm_name = f"manual-vm-{idx + 1:03d}"
            vm_names.append(vm_name)
            writer.writerow(
                [
                    vm_name,
                    "poweredOn",
                    "False",
                    os_values[idx],
                    cpu_values[idx],
                    memory_gb_values[idx] * 1024,
                    storage_gb_values[idx] * 1024,
                ]
            )

    return file_path, vm_names


def is_manual_inventory_path(path_text: Any) -> bool:
    clean_path = str(path_text or "").strip().replace("\\", "/")
    if not clean_path:
        return False
    path = Path(clean_path)
    return path.parent.name == "manual" and path.name.startswith("manual_inventory_") and path.suffix.lower() == ".csv"


def default_manual_sizing_form() -> dict[str, Any]:
    return {
        "is_active": False,
        "submit_label": "Create Summary",
        "vm_count": "",
        "total_vcpus": "",
        "total_memory_gb": "",
        "total_storage_gb": "",
        "supported_vm_count": "0",
        "unsupported_vm_count": "0",
    }


def build_manual_sizing_form(
    selected_path: Any,
    submitted_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    form_state = default_manual_sizing_form()
    if is_manual_inventory_path(selected_path):
        try:
            vm_rows, _source = load_vms_from_vinfo(str(selected_path))
        except Exception:
            vm_rows = []
        if vm_rows:
            unsupported_count = sum(
                1
                for row in vm_rows
                if str(row.get("mapped_os") or "").strip().lower().startswith("unmapped")
            )
            supported_count = max(0, len(vm_rows) - unsupported_count)
            form_state.update(
                {
                    "is_active": True,
                    "submit_label": "Update Summary",
                    "vm_count": str(len(vm_rows)),
                    "total_vcpus": str(int(sum(_to_number(row.get("cpus")) for row in vm_rows))),
                    "total_memory_gb": str(
                        int(sum(math.ceil(_to_number(row.get("memory_mb")) / 1024.0) for row in vm_rows))
                    ),
                    "total_storage_gb": str(
                        int(sum(math.ceil(_to_number(row.get("provisioned_mib")) / 1024.0) for row in vm_rows))
                    ),
                    "supported_vm_count": str(supported_count),
                    "unsupported_vm_count": str(unsupported_count),
                }
            )

    if submitted_values is not None:
        submitted_keys = {
            "vm_count": "manual_vm_count",
            "total_vcpus": "manual_total_vcpus",
            "total_memory_gb": "manual_total_memory_gb",
            "total_storage_gb": "manual_total_storage_gb",
            "supported_vm_count": "manual_supported_vm_count",
            "unsupported_vm_count": "manual_unsupported_vm_count",
        }
        for state_key, form_key in submitted_keys.items():
            form_state[state_key] = str(submitted_values.get(form_key, "")).strip()
    return form_state


def build_rejected_inventory_info(file_info: dict[str, Any], reason: str) -> dict[str, Any]:
    reason_text = str(reason or "").strip()
    normalized_reason = reason_text.lower()
    category = "Unsupported inventory format"
    recommended_use = "Upload a VM-level inventory file for sizing and use this file only as reference material."

    if "workload categorization" in normalized_reason or "vm list" in normalized_reason:
        category = "Workload categorization file"
        recommended_use = "Use later as supplementary input for migration waves, placement review, or application grouping after a sizing inventory is loaded."
    elif "aggregate infrastructure capacity assessment" in normalized_reason:
        category = "Aggregate capacity assessment"
        recommended_use = "Use as advisory context only. It can inform architecture discussion, but it cannot drive per-VM OCI sizing without VM-level rows."
    elif "oci pricing estimate" in normalized_reason or "oracle investment proposal" in normalized_reason:
        category = "OCI pricing estimate"
        recommended_use = "Use later as a reference estimate or commercial benchmark, not as source workload inventory."

    return {
        "file_path": file_info.get("file_path", ""),
        "file_name": file_info.get("file_name", ""),
        "size_kb": file_info.get("size_kb", ""),
        "category": category,
        "reason": reason_text,
        "recommended_use": recommended_use,
        "required_input": "Primary sizing requires VM name, vCPU, RAM, storage, and OS columns.",
    }


def format_total_memory_gb_or_tb(total_mb: int) -> str:
    """Format total RAM for Step 3: GB if under 1 TiB, otherwise TB (1024-based)."""
    total_mb = max(0, int(total_mb))
    total_gb = total_mb / 1024.0
    if total_gb < 1024.0:
        return f"{total_gb:,.1f} GB"
    return f"{total_gb / 1024.0:,.2f} TB"


def _ceil_div_positive(numerator: float, denominator: float) -> int:
    if numerator <= 0:
        return 0
    if denominator <= 0:
        return 10**9
    return int(math.ceil(numerator / denominator))


def normalize_step4_scenario_tab(value: Any, default: str = "paths") -> str:
    tab = str(value or default).strip().lower().replace("scenario-", "")
    return tab if tab in {"paths", "native", "ocvs", "hybrid", "price"} else default


def normalize_native_editor_query(values: Any) -> dict[str, Any]:
    try:
        page = int(str(values.get("native_page", "1")).strip())
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)

    try:
        page_size = int(str(values.get("native_page_size", NATIVE_VM_INPUT_ROW_LIMIT)).strip())
    except (TypeError, ValueError):
        page_size = NATIVE_VM_INPUT_ROW_LIMIT
    if page_size not in NATIVE_PAGE_SIZE_OPTIONS:
        page_size = NATIVE_VM_INPUT_ROW_LIMIT

    search = str(values.get("native_search", "") or "").strip()[:NATIVE_SEARCH_MAX_LENGTH]
    support = str(values.get("native_support", "all") or "all").strip().lower()
    if support not in NATIVE_SUPPORT_FILTERS:
        support = "all"
    return {
        "page": page,
        "page_size": page_size,
        "search": search,
        "support": support,
    }


def build_native_editor_page(
    vm_rows: list[dict[str, Any]],
    query: dict[str, Any],
    supported_signatures: list[str],
) -> dict[str, Any]:
    annotated_rows: list[dict[str, Any]] = []
    for row_index, source_row in enumerate(vm_rows, start=1):
        row = dict(source_row)
        os_name = str(row.get("os_name") or "Unknown / Empty")
        if _is_unknown_os(os_name) or not supported_signatures:
            support_state = "review"
            support_label = "Review required"
        elif is_oci_supported_os(os_name, supported_signatures):
            support_state = "supported"
            support_label = "Supported"
        else:
            support_state = "remediation"
            support_label = "Requires remediation"
        row.update(
            {
                "native_row_index": row_index,
                "native_support_state": support_state,
                "native_support_label": support_label,
            }
        )
        annotated_rows.append(row)

    search_term = str(query.get("search") or "").casefold()
    support_filter = str(query.get("support") or "all")
    filtered_rows = [
        row
        for row in annotated_rows
        if (
            not search_term
            or search_term
            in f"{row.get('vm_name', '')} {row.get('os_name', '')}".casefold()
        )
        and (
            support_filter == "all"
            or row.get("native_support_state") == support_filter
        )
    ]
    page_size = int(query.get("page_size") or NATIVE_VM_INPUT_ROW_LIMIT)
    page_count = max(1, int(math.ceil(len(filtered_rows) / page_size)))
    page = min(max(1, int(query.get("page") or 1)), page_count)
    start = (page - 1) * page_size
    return {
        "rows": filtered_rows[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "page_size_options": NATIVE_PAGE_SIZE_OPTIONS,
        "page_count": page_count,
        "filtered_count": len(filtered_rows),
        "workload_count": len(vm_rows),
        "search": str(query.get("search") or ""),
        "support": support_filter,
        "first_row": start + 1 if filtered_rows else 0,
        "last_row": min(start + page_size, len(filtered_rows)),
    }


NATIVE_EDITOR_FORM_FIELDS = (
    "vm_name",
    "oci_shape",
    "vm_ocpu",
    "vm_burst",
    "vm_vpu",
    "vm_os_license",
)


def parse_native_editor_page_fields(
    form: Any,
    expected_rows: list[dict[str, Any]],
    valid_shape_values: set[str],
    valid_burst_values: set[str],
    valid_vpu_values: set[int],
) -> tuple[dict[str, dict[str, Any]] | None, list[str]]:
    if not any(field_name in form for field_name in NATIVE_EDITOR_FORM_FIELDS):
        return None, []

    expected_vm_names = [str(row.get("vm_name") or "") for row in expected_rows]
    submitted_values = {
        field_name: form.getlist(field_name)
        for field_name in NATIVE_EDITOR_FORM_FIELDS
    }
    submitted_vm_names = [str(value).strip() for value in submitted_values["vm_name"]]
    errors: list[str] = []

    if submitted_vm_names != expected_vm_names:
        errors.append("Native editor rows do not match the requested page and filters.")
    if len(submitted_vm_names) != len(set(submitted_vm_names)):
        errors.append("Each Native editor VM must be submitted exactly once.")
    expected_count = len(expected_vm_names)
    for field_name in NATIVE_EDITOR_FORM_FIELDS[1:]:
        if len(submitted_values[field_name]) != expected_count:
            errors.append("Every Native editor row must include one value for every setting.")
            break
    if errors:
        return None, errors

    parsed: dict[str, dict[str, Any]] = {}
    for row_index, expected_row in enumerate(expected_rows):
        vm_name = expected_vm_names[row_index]
        shape = str(submitted_values["oci_shape"][row_index]).strip()
        ocpu_raw = str(submitted_values["vm_ocpu"][row_index]).strip()
        burst = str(submitted_values["vm_burst"][row_index]).strip()
        vpu_raw = str(submitted_values["vm_vpu"][row_index]).strip()
        os_license = str(submitted_values["vm_os_license"][row_index]).strip()

        if shape not in valid_shape_values:
            errors.append(f"Choose a valid OCI shape for {vm_name}.")
        if not re.fullmatch(r"[1-9][0-9]*", ocpu_raw):
            errors.append(f"Choose a positive whole OCPU count for {vm_name}.")
        if burst not in valid_burst_values:
            errors.append(f"Choose a valid burst setting for {vm_name}.")
        if not re.fullmatch(r"[0-9]+", vpu_raw) or int(vpu_raw) not in valid_vpu_values:
            errors.append(f"Choose a valid VPU setting for {vm_name}.")

        is_windows_server = "windows server" in str(expected_row.get("raw_os") or "").lower()
        if is_windows_server and os_license not in OS_LICENSE_VALUES:
            errors.append(f"Choose a valid Windows license setting for {vm_name}.")
        elif not is_windows_server and os_license:
            errors.append(f"OS license must be empty for non-Windows VM {vm_name}.")

        if not errors:
            parsed[vm_name] = {
                "oci_shape": shape,
                "ocpu": int(ocpu_raw),
                "burst": burst,
                "vpu": int(vpu_raw),
                "os_license": os_license,
            }

    return (parsed if not errors else None), errors


def parse_step4_scalar_submission(form: Any) -> tuple[dict[str, Any], list[str]]:
    """Strictly parse the single-value Step 4 controls before persistence."""
    parsed: dict[str, Any] = {}
    errors: list[str] = []

    for field_name in STEP4_SINGLE_VALUE_FIELDS:
        if field_name in form and len(form.getlist(field_name)) != 1:
            errors.append(f"Submit exactly one value for {field_name}.")

    action = str(form.get("action", "save")).strip()
    if action not in STEP4_ALLOWED_ACTIONS:
        errors.append("Choose a valid Step 4 action.")
    parsed["action"] = action

    active_scenario = str(form.get("active_scenario", "native")).strip()
    if active_scenario not in STEP4_ACTIVE_SCENARIOS:
        errors.append("Choose a valid active scenario.")
        active_scenario = "native"
    parsed["active_scenario"] = active_scenario

    if "ocvs_profile" in form:
        profile = str(form.get("ocvs_profile", "")).strip()
        valid_profiles = {"best_fit"} | {
            str(item.get("shape") or "").strip()
            for item in OCVS_HOST_PROFILES
            if str(item.get("shape") or "").strip()
        }
        if profile not in valid_profiles:
            errors.append("Choose a valid OCVS node profile.")
        else:
            parsed["ocvs_profile"] = profile

    if "hybrid_ocvs_profile" in form:
        profile = str(form.get("hybrid_ocvs_profile", "")).strip()
        valid_profiles = {"best_fit"} | {
            str(item.get("shape") or "").strip()
            for item in OCVS_HOST_PROFILES
            if str(item.get("shape") or "").strip()
        }
        if profile not in valid_profiles:
            errors.append("Choose a valid Hybrid OCVS node profile.")
        else:
            parsed["hybrid_ocvs_profile"] = profile

    if "ocvs_commitment_term" in form:
        commitment_term = str(form.get("ocvs_commitment_term", "")).strip()
        if commitment_term not in OCVS_COMMITMENT_TERMS:
            errors.append("Choose a valid OCVS commitment term.")
        else:
            parsed["ocvs_commitment_term"] = commitment_term

    if "hybrid_ocvs_commitment_term" in form:
        commitment_term = str(form.get("hybrid_ocvs_commitment_term", "")).strip()
        if commitment_term not in OCVS_COMMITMENT_TERMS:
            errors.append("Choose a valid Hybrid OCVS commitment term.")
        else:
            parsed["hybrid_ocvs_commitment_term"] = commitment_term

    numeric_rules = {
        "iaas_discount_pct": ("IaaS discount", 0.0, 100.0, False, None),
        "vmware_license_price_per_core_yearly": (
            "VCF list price per physical core/year",
            0.0,
            1_000_000.0,
            False,
            None,
        ),
        "ocvs_vcpu_per_ocpu": ("vCPU per OCPU", 1.0, 16.0, False, None),
        "ocvs_cpu_headroom_pct": ("CPU headroom", 0.0, 90.0, True, None),
        "ocvs_memory_headroom_pct": ("RAM headroom", 0.0, 90.0, True, None),
        "ocvs_storage_headroom_pct": ("storage headroom", 0.0, 90.0, True, None),
        "ocvs_dense_vsan_usable_pct": ("dense vSAN usable", 10.0, 95.0, True, None),
        "ocvs_standard_storage_vpu": (
            "standard storage VPU/GB",
            10.0,
            120.0,
            True,
            set(VPU_OPTIONS),
        ),
        "ocvs_dr_nodes": (
            "additional spare nodes",
            float(min(VALID_OCVS_DR_NODE_COUNTS)),
            float(max(VALID_OCVS_DR_NODE_COUNTS)),
            True,
            set(VALID_OCVS_DR_NODE_COUNTS),
        ),
        "hybrid_vmware_license_price_per_core_yearly": (
            "Hybrid VCF list price per physical core/year",
            0.0,
            1_000_000.0,
            False,
            None,
        ),
        "hybrid_ocvs_vcpu_per_ocpu": ("Hybrid vCPU per OCPU", 1.0, 16.0, False, None),
        "hybrid_ocvs_cpu_headroom_pct": ("Hybrid CPU headroom", 0.0, 90.0, True, None),
        "hybrid_ocvs_memory_headroom_pct": ("Hybrid RAM headroom", 0.0, 90.0, True, None),
        "hybrid_ocvs_storage_headroom_pct": ("Hybrid storage headroom", 0.0, 90.0, True, None),
        "hybrid_ocvs_dense_vsan_usable_pct": ("Hybrid dense vSAN usable", 10.0, 95.0, True, None),
        "hybrid_ocvs_standard_storage_vpu": (
            "Hybrid standard storage VPU/GB",
            10.0,
            120.0,
            True,
            set(VPU_OPTIONS),
        ),
        "hybrid_ocvs_dr_nodes": (
            "Hybrid additional spare nodes",
            float(min(VALID_OCVS_DR_NODE_COUNTS)),
            float(max(VALID_OCVS_DR_NODE_COUNTS)),
            True,
            set(VALID_OCVS_DR_NODE_COUNTS),
        ),
    }
    for field_name, (label, minimum, maximum, whole_number, allowed_values) in numeric_rules.items():
        if field_name not in form:
            continue
        raw_value = str(form.get(field_name, "")).strip()
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError):
            numeric_value = math.nan
        valid = (
            math.isfinite(numeric_value)
            and minimum <= numeric_value <= maximum
            and (not whole_number or numeric_value.is_integer())
            and (allowed_values is None or int(numeric_value) in allowed_values)
        )
        if not valid:
            errors.append(f"Enter a valid {label} value.")
            continue
        parsed[field_name] = int(numeric_value) if whole_number else numeric_value

    return parsed, errors


def _single_string_form_value(
    form: Any,
    field_name: str,
    *,
    cardinality_error: str,
    type_error: str,
    errors: list[str],
) -> str | None:
    values = form.getlist(field_name)
    if len(values) != 1:
        errors.append(cardinality_error)
        return None
    value = values[0]
    if not isinstance(value, str):
        errors.append(type_error)
        return None
    return value


def parse_recommendation_submission(form: Any) -> tuple[dict[str, str], list[str]]:
    """Validate the Results decision form without accepting scenario fields."""
    parsed = {"recommendation": "", "recommendation_rationale": ""}
    errors: list[str] = []

    unknown_fields = sorted(set(form.keys()) - RESULT_RECOMMENDATION_FIELDS)
    if unknown_fields:
        errors.append("The recommendation form contains unsupported fields.")

    action_values = form.getlist("action")
    if len(action_values) != 1 or action_values[0] != "save_recommendation":
        errors.append("Submit exactly one valid recommendation action.")

    recommendation_value = _single_string_form_value(
        form,
        "recommendation",
        cardinality_error="Submit exactly one specialist recommendation.",
        type_error="Specialist recommendation must be text.",
        errors=errors,
    )
    if recommendation_value is not None:
        recommendation = recommendation_value.strip()
        if recommendation not in RESULT_RECOMMENDATION_VALUES:
            errors.append("Choose a valid specialist recommendation.")
        else:
            parsed["recommendation"] = recommendation

    rationale_value = _single_string_form_value(
        form,
        "recommendation_rationale",
        cardinality_error="Submit exactly one recommendation rationale.",
        type_error="Recommendation rationale must be text.",
        errors=errors,
    )
    if rationale_value is not None:
        rationale = rationale_value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(rationale) > 4000:
            errors.append("Recommendation rationale must be 4,000 characters or fewer.")
        else:
            parsed["recommendation_rationale"] = rationale

    return parsed, errors


WORKSPACE_STAGE_MAP = {
    "setup": {
        "number": 1,
        "name": "Setup & Inventory",
        "endpoint": "index",
        "url_values": {},
        "previous_stage": "",
        "continue_stage": "inventory",
        "continue_presentation": "link",
    },
    "inventory": {
        "number": 2,
        "name": "Inventory Review",
        "endpoint": "step3",
        "url_values": {},
        "previous_stage": "setup",
        "continue_stage": "scenarios",
        "continue_presentation": "form",
    },
    "scenarios": {
        "number": 3,
        "name": "Scenario Configuration",
        "endpoint": "step4",
        "url_values": {"tab": "native"},
        "previous_stage": "inventory",
        "continue_stage": "results",
        "continue_presentation": "form",
    },
    "results": {
        "number": 4,
        "name": "Results & Export",
        "endpoint": "step4",
        "url_values": {"tab": "price"},
        "previous_stage": "scenarios",
        "continue_stage": "",
        "continue_presentation": "none",
    },
}


def _readiness_finite_cost(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _readiness_positive_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed > 0.0


def _readiness_nonnegative_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0.0 or not parsed.is_integer():
        return None
    return int(parsed)


def _readiness_vm_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("vm_name") or "").strip()


def _readiness_hybrid_partition_complete(
    *,
    selected_names: list[str],
    scenario_row: Any,
    supported_native_rows: Any,
    unsupported_ocvs_rows: Any,
    placement_plan: Any,
) -> bool:
    if (
        not selected_names
        or not isinstance(scenario_row, dict)
        or not isinstance(placement_plan, dict)
    ):
        return False
    selected_set = set(selected_names)
    if len(selected_set) != len(selected_names):
        return False

    def row_names(value: Any) -> tuple[list[str], set[str]] | None:
        if not isinstance(value, list):
            return None
        names: list[str] = []
        for row in value:
            if not isinstance(row, dict):
                return None
            name = _readiness_vm_name(row)
            if not name:
                return None
            names.append(name)
        name_set = set(names)
        if len(name_set) != len(names) or not name_set <= selected_set:
            return None
        return names, name_set

    top_native = row_names(supported_native_rows)
    top_ocvs = row_names(unsupported_ocvs_rows)
    plan_native = row_names(placement_plan.get("native_rows"))
    plan_ocvs = row_names(placement_plan.get("ocvs_rows"))
    if None in (top_native, top_ocvs, plan_native, plan_ocvs):
        return False
    assert top_native is not None and top_ocvs is not None
    assert plan_native is not None and plan_ocvs is not None
    native_names, native_set = top_native
    ocvs_names, ocvs_set = top_ocvs
    if (
        native_set & ocvs_set
        or native_set | ocvs_set != selected_set
        or plan_native[1] != native_set
        or plan_ocvs[1] != ocvs_set
    ):
        return False

    def exact_count(mapping: dict[str, Any], key: str, expected: int) -> bool:
        return _readiness_nonnegative_count(mapping.get(key)) == expected

    if not (
        exact_count(scenario_row, "native_vm_count", len(native_names))
        and exact_count(scenario_row, "ocvs_vm_count", len(ocvs_names))
        and exact_count(placement_plan, "native_count", len(native_names))
        and exact_count(placement_plan, "ocvs_priced_count", len(ocvs_names))
    ):
        return False

    plan_rows_present = "rows" in placement_plan
    plan_review_set: set[str] | None = None
    plan_explicit_ocvs_set: set[str] | None = None
    if plan_rows_present:
        plan_rows = placement_plan.get("rows")
        parsed_plan_rows = row_names(plan_rows)
        if parsed_plan_rows is None or parsed_plan_rows[1] != selected_set:
            return False
        target_native_set: set[str] = set()
        target_ocvs_set: set[str] = set()
        plan_review_set = set()
        plan_explicit_ocvs_set = set()
        assert isinstance(plan_rows, list)
        for row in plan_rows:
            name = _readiness_vm_name(row)
            placement = str(row.get("hybrid_placement") or "").strip().lower()
            effective_target = str(
                row.get("hybrid_effective_target") or ""
            ).strip().lower()
            expected_target = (
                "native"
                if placement == "native"
                else "ocvs"
                if placement in {"ocvs", "review"}
                else ""
            )
            if not expected_target or effective_target != expected_target:
                return False
            if effective_target == "native":
                target_native_set.add(name)
            else:
                target_ocvs_set.add(name)
            if placement == "review":
                plan_review_set.add(name)
            elif placement == "ocvs":
                plan_explicit_ocvs_set.add(name)
        if target_native_set != native_set or target_ocvs_set != ocvs_set:
            return False

    review_rows = (
        row_names(placement_plan.get("review_rows"))
        if "review_rows" in placement_plan
        else None
    )
    explicit_ocvs_rows = (
        row_names(placement_plan.get("explicit_ocvs_rows"))
        if "explicit_ocvs_rows" in placement_plan
        else None
    )
    if "review_rows" in placement_plan and review_rows is None:
        return False
    if "explicit_ocvs_rows" in placement_plan and explicit_ocvs_rows is None:
        return False
    review_set = review_rows[1] if review_rows is not None else plan_review_set
    explicit_ocvs_set = (
        explicit_ocvs_rows[1]
        if explicit_ocvs_rows is not None
        else plan_explicit_ocvs_set
    )
    if review_set is None or explicit_ocvs_set is None:
        return False
    if (
        not review_set <= ocvs_set
        or not explicit_ocvs_set <= ocvs_set
        or review_set & explicit_ocvs_set
        or review_set | explicit_ocvs_set != ocvs_set
        or (plan_review_set is not None and review_set != plan_review_set)
        or (
            plan_explicit_ocvs_set is not None
            and explicit_ocvs_set != plan_explicit_ocvs_set
        )
        or not exact_count(placement_plan, "review_count", len(review_set))
        or not exact_count(
            placement_plan, "ocvs_count", len(explicit_ocvs_set)
        )
    ):
        return False
    return True


def _readiness_item(
    item: dict[str, Any],
    *,
    stage: str,
    acknowledged_ids: set[str],
    default_id: str = "",
) -> dict[str, Any]:
    item_id = str(item.get("id") or default_id).strip()
    title = str(item.get("title") or item_id.replace("-", " ").title()).strip()
    detail = str(item.get("detail") or "").strip()
    raw_names = item.get("affected_vm_names", item.get("vm_names", []))
    affected_vm_names = (
        [str(name).strip() for name in raw_names if str(name).strip()]
        if isinstance(raw_names, (list, tuple, set, frozenset))
        else []
    )
    severity = str(item.get("severity") or "advisory").strip().lower()
    return {
        "id": item_id,
        "title": title,
        "detail": detail,
        "message": detail or title,
        "stage": stage,
        "affected_vm_names": affected_vm_names,
        "severity": severity,
        "acknowledged": item_id in acknowledged_ids,
    }


def build_current_readiness_context(
    inventory_rows: list[dict[str, Any]] | None,
    selected_vm_names: list[str] | None,
    scenario_analysis: dict[str, Any] | None = None,
    scenario_views: list[dict[str, Any]] | None = None,
    app_state: dict[str, Any] | None = None,
    setup_metadata: dict[str, Any] | None = None,
    has_unsaved_scenario_changes: bool = False,
    inventory_issues: list[dict[str, Any]] | None = None,
    pricing_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adapt already-loaded assessment data to the central readiness model."""
    adapter_inventory_issues: list[dict[str, Any]] = []
    adapter_scenario_issues: list[dict[str, Any]] = []

    def add_integrity_issue(
        target: list[dict[str, Any]],
        issue_id: str,
        title: str,
        detail: str,
        *,
        stage: str,
        severity: str = "critical",
    ) -> None:
        target.append(
            _readiness_item(
                {
                    "id": issue_id,
                    "title": title,
                    "detail": detail,
                    "severity": severity,
                    "affected_vm_names": [],
                },
                stage=stage,
                acknowledged_ids=set(),
            )
        )

    if inventory_rows is None:
        inventory_row_values: list[Any] = []
    elif isinstance(inventory_rows, list):
        inventory_row_values = inventory_rows
        if not all(isinstance(row, dict) for row in inventory_row_values):
            add_integrity_issue(
                adapter_inventory_issues,
                "invalid-inventory-rows",
                "Invalid inventory row data",
                "Inventory rows must be a list containing only mappings.",
                stage="inventory",
            )
    else:
        inventory_row_values = []
        add_integrity_issue(
            adapter_inventory_issues,
            "invalid-inventory-rows",
            "Invalid inventory row data",
            "Inventory rows must be a list containing only mappings.",
            stage="inventory",
        )
    rows = [row for row in inventory_row_values if isinstance(row, dict)]
    row_by_name = {
        _readiness_vm_name(row): row
        for row in rows
        if _readiness_vm_name(row)
    }

    selected_values: list[Any]
    selected_values_valid = True
    if selected_vm_names is None:
        selected_values = []
    elif isinstance(selected_vm_names, list):
        selected_values = selected_vm_names
    else:
        selected_values = []
        selected_values_valid = False
    selected_names: list[str] = []
    selected_seen: set[str] = set()
    for name in selected_values:
        if not isinstance(name, str):
            selected_values_valid = False
            continue
        clean_name = name.strip()
        if not clean_name or clean_name not in row_by_name or clean_name in selected_seen:
            selected_values_valid = False
            continue
        selected_names.append(clean_name)
        selected_seen.add(clean_name)
    if not selected_values_valid:
        add_integrity_issue(
            adapter_inventory_issues,
            "invalid-selected-vm-names",
            "Invalid selected VM names",
            "Selected VM names must be a unique list of inventory VM names.",
            stage="inventory",
        )

    state = app_state if isinstance(app_state, dict) else {}
    setup = setup_metadata if isinstance(setup_metadata, dict) else {}
    if scenario_analysis is None:
        analysis: dict[str, Any] = {}
    elif isinstance(scenario_analysis, dict):
        analysis = scenario_analysis
    else:
        analysis = {}
        add_integrity_issue(
            adapter_scenario_issues,
            "invalid-scenario-analysis",
            "Invalid scenario analysis data",
            "Scenario analysis must be a mapping when supplied.",
            stage="scenarios",
        )
    if pricing_inputs is None:
        pricing: dict[str, Any] = {}
    elif isinstance(pricing_inputs, dict):
        pricing = pricing_inputs
    else:
        pricing = {}
        add_integrity_issue(
            adapter_scenario_issues,
            "invalid-pricing-inputs",
            "Invalid pricing input data",
            "Pricing inputs must be a mapping when supplied.",
            stage="scenarios",
        )
    acknowledged_value = state.get("acknowledged_warning_ids", [])
    acknowledged_ids = {
        str(item).strip()
        for item in acknowledged_value
        if str(item).strip()
    } if isinstance(acknowledged_value, list) else set()

    if inventory_issues is None:
        source_issue_values: list[Any] = build_inventory_review_issues(rows)
    elif isinstance(inventory_issues, dict):
        source_issue_values = [inventory_issues]
    elif isinstance(inventory_issues, list):
        source_issue_values = inventory_issues
        if not all(isinstance(issue, dict) for issue in source_issue_values):
            add_integrity_issue(
                adapter_inventory_issues,
                "invalid-inventory-issues",
                "Invalid inventory issue data",
                "Inventory issues must be a mapping or a list containing only mappings.",
                stage="inventory",
            )
    else:
        source_issue_values = []
        add_integrity_issue(
            adapter_inventory_issues,
            "invalid-inventory-issues",
            "Invalid inventory issue data",
            "Inventory issues must be a mapping or a list containing only mappings.",
            stage="inventory",
        )
    mapped_inventory_issues = [
        _readiness_item(
            issue,
            stage="inventory",
            acknowledged_ids=acknowledged_ids,
        )
        for issue in source_issue_values
        if isinstance(issue, dict)
    ] + adapter_inventory_issues

    unsupported_native_input: Any = []
    if "oci_unsupported_rows" in analysis:
        unsupported_source = analysis.get("oci_unsupported_rows")
        if (
            isinstance(unsupported_source, list)
            and all(isinstance(row, dict) for row in unsupported_source)
            and all(_readiness_vm_name(row) for row in unsupported_source)
        ):
            unsupported_set = {
                _readiness_vm_name(row)
                for row in unsupported_source
                if _readiness_vm_name(row)
            }
            unsupported_native_input = [
                name for name in selected_names if name in unsupported_set
            ]
        else:
            # Preserve malformed compatibility data for the pure model's
            # integrity advisory instead of silently treating it as empty.
            unsupported_native_input = 0
    else:
        supported_signatures = load_supported_os_signatures()
        unsupported_native_input = [
            name
            for name in selected_names
            if not supported_signatures
            or not is_oci_supported_os(
                str(
                    row_by_name[name].get("raw_os")
                    or row_by_name[name].get("os_name")
                    or ""
                ),
                supported_signatures,
            )
        ]

    comparison_rows: list[Any] = []
    if "scenario_comparison" in analysis:
        comparison = analysis.get("scenario_comparison")
        if not isinstance(comparison, dict):
            add_integrity_issue(
                adapter_scenario_issues,
                "invalid-scenario-comparison",
                "Invalid scenario comparison data",
                "Scenario comparison data must be a mapping.",
                stage="scenarios",
            )
        else:
            comparison_value = comparison.get("rows")
            if not isinstance(comparison_value, list):
                add_integrity_issue(
                    adapter_scenario_issues,
                    "invalid-scenario-comparison-rows",
                    "Invalid scenario comparison rows",
                    "Scenario comparison rows must be a list containing only mappings.",
                    stage="scenarios",
                )
            else:
                comparison_rows = comparison_value
                if not all(isinstance(row, dict) for row in comparison_rows):
                    add_integrity_issue(
                        adapter_scenario_issues,
                        "invalid-scenario-comparison-rows",
                        "Invalid scenario comparison rows",
                        "Scenario comparison rows must be a list containing only mappings.",
                        stage="scenarios",
                    )
    scenario_rows = {
        str(row.get("id") or "").strip().lower(): row
        for row in comparison_rows
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }
    if scenario_views is None:
        view_values: list[Any] = []
    elif isinstance(scenario_views, list):
        view_values = scenario_views
        if not all(isinstance(view, dict) for view in view_values):
            add_integrity_issue(
                adapter_scenario_issues,
                "invalid-scenario-views",
                "Invalid scenario view data",
                "Scenario views must be a list containing only mappings.",
                stage="scenarios",
            )
    else:
        view_values = []
        add_integrity_issue(
            adapter_scenario_issues,
            "invalid-scenario-views",
            "Invalid scenario view data",
            "Scenario views must be a list containing only mappings.",
            stage="scenarios",
        )
    for view in view_values:
        if not isinstance(view, dict):
            continue
        scenario_id = str(view.get("id") or "").strip().lower()
        view_scenario = view.get("scenario", {})
        if scenario_id and scenario_id not in scenario_rows and isinstance(view_scenario, dict):
            scenario_rows[scenario_id] = view_scenario

    price_lookup = pricing.get("price_lookup", {})
    price_source_available = bool(
        str(pricing.get("source_pricelist_file") or "").strip()
        and isinstance(price_lookup, dict)
        and price_lookup
    )
    modeled_rows_value = pricing.get("modeled_vm_rows", [])
    if isinstance(modeled_rows_value, list):
        modeled_rows = [
            row for row in modeled_rows_value if isinstance(row, dict)
        ]
        if not all(isinstance(row, dict) for row in modeled_rows_value):
            add_integrity_issue(
                adapter_scenario_issues,
                "invalid-modeled-vm-rows",
                "Invalid modeled VM rows",
                "Modeled VM rows must be a list containing only mappings.",
                stage="scenarios",
            )
    else:
        modeled_rows = []
        add_integrity_issue(
            adapter_scenario_issues,
            "invalid-modeled-vm-rows",
            "Invalid modeled VM rows",
            "Modeled VM rows must be a list containing only mappings.",
            stage="scenarios",
        )
    modeled_by_name = {
        _readiness_vm_name(row): row
        for row in modeled_rows
        if _readiness_vm_name(row)
    }

    def native_prices_complete(required_rows: list[dict[str, Any]]) -> bool:
        required_names = [_readiness_vm_name(row) for row in required_rows]
        required_names = [name for name in required_names if name]
        if not required_names:
            return True
        if not price_source_available:
            return False
        if any(name not in modeled_by_name for name in required_names):
            return False
        if any(
            not _readiness_positive_number(
                modeled_by_name[name].get("ocpu_unit_price", 0.0)
            )
            or not _readiness_positive_number(
                modeled_by_name[name].get("memory_unit_price", 0.0)
            )
            for name in required_names
        ):
            return False
        if (
            not _readiness_positive_number(
                pricing.get("block_storage_unit_price", 0.0)
            )
            or not _readiness_positive_number(
                pricing.get("block_perf_unit_price", 0.0)
            )
        ):
            return False
        return not any(
            bool(modeled_by_name[name].get("is_windows_server"))
            and str(modeled_by_name[name].get("os_license") or "") == "Lic Include"
            and not _readiness_positive_number(
                pricing.get("windows_os_unit_price", 0.0)
            )
            for name in required_names
        )

    def ocvs_infrastructure_complete(
        summary: Any,
        license_item: Any,
        workload_count: int | None,
    ) -> bool:
        if workload_count is None:
            return False
        if workload_count == 0:
            return True
        if not isinstance(summary, dict) or not isinstance(license_item, dict):
            return False
        selected = summary.get("selected")
        if not isinstance(selected, dict):
            return False
        host_count = _readiness_nonnegative_count(selected.get("host_count"))
        return bool(
            price_source_available
            and host_count is not None
            and host_count > 0
            and selected.get("pricing_available") is True
        )

    full_selected_rows = [row_by_name[name] for name in selected_names]
    hybrid_native_value = analysis.get("supported_native_rows")
    hybrid_native_rows_valid = bool(
        "supported_native_rows" in analysis
        and isinstance(hybrid_native_value, list)
        and all(
            isinstance(row, dict) and bool(_readiness_vm_name(row))
            for row in hybrid_native_value
        )
    )
    hybrid_native_rows = (
        list(hybrid_native_value) if hybrid_native_rows_valid else []
    )
    if "supported_native_rows" in analysis and not hybrid_native_rows_valid:
        add_integrity_issue(
            adapter_scenario_issues,
            "invalid-hybrid-native-rows",
            "Invalid Hybrid Native rows",
            "Hybrid Native rows must be a list containing only mappings.",
            stage="scenarios",
        )
    vmware_summary = analysis.get("vmware_license_summary", {})
    if not isinstance(vmware_summary, dict):
        vmware_summary = {}
    raw_vcf_price = state.get(
        "step4_vmware_license_price_per_core_yearly",
        vmware_summary.get("price_per_core_yearly", 0.0),
    )
    try:
        vcf_price_per_core_yearly = float(raw_vcf_price or 0.0)
    except (TypeError, ValueError):
        vcf_price_per_core_yearly = 0.0
    if not math.isfinite(vcf_price_per_core_yearly):
        vcf_price_per_core_yearly = 0.0

    hybrid_scenario_row = scenario_rows.get("hybrid", {})
    placement_plan_value = analysis.get("hybrid_placement_plan")
    hybrid_ocvs_workload_count = _readiness_nonnegative_count(
        hybrid_scenario_row.get("ocvs_vm_count")
    )
    hybrid_partition_complete = _readiness_hybrid_partition_complete(
        selected_names=selected_names,
        scenario_row=hybrid_scenario_row,
        supported_native_rows=hybrid_native_value,
        unsupported_ocvs_rows=analysis.get("unsupported_ocvs_rows"),
        placement_plan=placement_plan_value,
    )

    fit_warning_values = analysis.get("fit_warnings", [])
    if isinstance(fit_warning_values, list):
        fit_warning_mappings = [
            warning for warning in fit_warning_values if isinstance(warning, dict)
        ]
        if not all(isinstance(warning, dict) for warning in fit_warning_values):
            add_integrity_issue(
                adapter_scenario_issues,
                "invalid-fit-warnings",
                "Invalid scenario fit warnings",
                "Scenario fit warnings must be a list containing only mappings.",
                stage="scenarios",
            )
    else:
        fit_warning_mappings = []
        add_integrity_issue(
            adapter_scenario_issues,
            "invalid-fit-warnings",
            "Invalid scenario fit warnings",
            "Scenario fit warnings must be a list containing only mappings.",
            stage="scenarios",
        )

    used_issue_ids = {
        str(item.get("id") or "").strip()
        for item in mapped_inventory_issues + adapter_scenario_issues
        if str(item.get("id") or "").strip()
    }
    used_issue_ids.update(
        {"invalid-native-unsupported-vms", "invalid-scenario-issues"}
    )
    fit_issues: list[dict[str, Any]] = []
    for index, warning in enumerate(fit_warning_mappings):
        title = str(warning.get("title") or "Scenario review item").strip()
        detail = str(warning.get("detail") or title).strip()
        source_id = str(warning.get("id") or title or index + 1).strip().lower()
        source_slug = re.sub(r"[^a-z0-9]+", "-", source_id).strip("-")
        base_id = source_slug if source_slug.startswith("fit-") else f"fit-{source_slug}"
        if base_id == "fit-":
            base_id = f"fit-{index + 1}"
        item_id = base_id
        suffix = 2
        while item_id in used_issue_ids:
            item_id = f"{base_id}-{suffix}"
            suffix += 1
        used_issue_ids.add(item_id)
        fit_issues.append(
            _readiness_item(
                {**warning, "id": item_id, "title": title, "detail": detail},
                stage="scenarios",
                acknowledged_ids=set(),
            )
        )
    scenario_issues = adapter_scenario_issues + fit_issues

    scenario_inputs: dict[str, dict[str, Any]] = {}
    for scenario_id in ("native", "ocvs", "hybrid"):
        scenario_row = scenario_rows.get(scenario_id, {})
        monthly_cost = _readiness_finite_cost(scenario_row.get("monthly_cost"))
        modeled = bool(selected_names and scenario_row and monthly_cost is not None)
        if scenario_id == "native":
            pricing_complete = bool(
                modeled and native_prices_complete(full_selected_rows)
            )
        else:
            summary_key = "ocvs_price" if scenario_id == "ocvs" else "hybrid_ocvs_price"
            license_item = vmware_summary.get(scenario_id, {})
            ocvs_workload_count = (
                len(selected_names)
                if scenario_id == "ocvs"
                else hybrid_ocvs_workload_count
            )
            native_subset_complete = (
                True
                if scenario_id == "ocvs"
                else native_prices_complete(hybrid_native_rows)
            )
            partition_complete = (
                True if scenario_id == "ocvs" else hybrid_partition_complete
            )
            pricing_complete = bool(
                modeled
                and partition_complete
                and native_subset_complete
                and ocvs_infrastructure_complete(
                    analysis.get(summary_key),
                    license_item,
                    ocvs_workload_count,
                )
            )
        scenario_inputs[scenario_id] = {
            "technically_eligible": modeled,
            "pricing_complete": pricing_complete,
            "monthly_cost": monthly_cost,
            "unsupported_vm_names": (
                unsupported_native_input if scenario_id == "native" else []
            ),
        }

    placements_value = state.get("step4_hybrid_placements", {})
    placements = placements_value if isinstance(placements_value, dict) else {}
    readiness = build_assessment_readiness(
        {
            "setup": {
                "assessment_name": setup.get("assessment_name", ""),
                "customer_name": setup.get("customer_name", ""),
                "has_price_list": setup.get("has_price_list") is True,
                "has_inventory": setup.get("has_inventory") is True,
            },
            "inventory": {
                "included_vm_names": selected_names,
                "placements": {
                    name: placements.get(name) for name in selected_names
                },
                "issues": mapped_inventory_issues,
                "acknowledged_warning_ids": list(acknowledged_ids),
            },
            "scenarios": scenario_inputs,
            "scenario_issues": scenario_issues,
            "has_unsaved_scenario_changes": has_unsaved_scenario_changes,
            "recommendation": state.get("assessor_recommendation", ""),
            "recommendation_rationale": state.get(
                "assessor_recommendation_rationale", ""
            ),
        }
    )

    source_by_id = {
        item["id"]: item for item in mapped_inventory_issues + scenario_issues
    }

    def enrich_model_item(item: dict[str, Any]) -> dict[str, Any]:
        enriched = {**source_by_id.get(str(item.get("id") or ""), {}), **item}
        title = str(enriched.get("title") or "").strip()
        detail = str(enriched.get("detail") or "").strip()
        enriched["message"] = (
            f"{title}: {detail}"
            if title and detail and detail != title
            else detail or title or str(enriched.get("id") or "")
        )
        enriched.setdefault("acknowledged", False)
        return enriched

    for collection_name in (
        "blocking_items",
        "advisory_items",
        "display_advisory_items",
    ):
        values = readiness.get(collection_name, [])
        readiness[collection_name] = [
            enrich_model_item(item) for item in values if isinstance(item, dict)
        ]
    for stage_values in readiness.get("stages", {}).values():
        if not isinstance(stage_values, dict):
            continue
        for collection_name in ("blockers", "advisories"):
            values = stage_values.get(collection_name, [])
            stage_values[collection_name] = [
                enrich_model_item(item) for item in values if isinstance(item, dict)
            ]
    return readiness


def build_workspace_context(
    stage_id: str,
    readiness: dict[str, Any] | None = None,
    **values: Any,
) -> dict[str, Any]:
    """Adapt route values to the shared four-stage workspace shell."""
    if stage_id not in WORKSPACE_STAGE_MAP:
        raise ValueError(f"Unknown workspace stage: {stage_id}")

    workspace_readiness: dict[str, Any] = {
        "state": "not_started",
        "blockers": [],
        "advisories": [],
        "stages": {},
    }
    if isinstance(readiness, dict):
        workspace_readiness.update(readiness)
        workspace_readiness["blockers"] = list(
            readiness.get("blocking_items", readiness.get("blockers", []))
        )
        workspace_readiness["advisories"] = list(
            readiness.get(
                "display_advisory_items",
                readiness.get("advisory_items", readiness.get("advisories", [])),
            )
        )
    for collection_name in ("blockers", "advisories"):
        if not isinstance(workspace_readiness.get(collection_name), (list, tuple)):
            workspace_readiness[collection_name] = []
    if not isinstance(workspace_readiness.get("stages"), dict):
        workspace_readiness["stages"] = {}

    selected_inventory_source = bool(str(session.get("selected_rvtools_file", "")).strip())
    app_state = load_app_state()
    selected_vm_names = app_state.get("selected_vm_names", [])
    has_selected_vms = bool(
        isinstance(selected_vm_names, list)
        and any(isinstance(vm_name, str) and vm_name.strip() for vm_name in selected_vm_names)
    )
    prerequisite_availability = {
        "setup": True,
        "inventory": selected_inventory_source,
        "scenarios": selected_inventory_source and has_selected_vms,
        "results": selected_inventory_source and has_selected_vms,
    }
    navigation_availability = dict(prerequisite_availability)
    navigation_availability[stage_id] = True

    stage_status_labels = {
        "available": "Available",
        "not_started": "Not started",
        "needs_attention": "Needs attention",
        "ready": "Ready",
        "complete": "Complete",
        "incomplete": "Incomplete",
        "blocked": "Blocked",
        "disabled": "Prerequisites required",
    }
    readiness_stages = workspace_readiness["stages"]
    workspace_stages: list[dict[str, Any]] = []
    for mapped_id, mapped_stage in WORKSPACE_STAGE_MAP.items():
        mapped_readiness = readiness_stages.get(mapped_id, {})
        if isinstance(mapped_readiness, dict):
            mapped_status = str(mapped_readiness.get("state", "available"))
        elif isinstance(mapped_readiness, str):
            mapped_status = mapped_readiness
        else:
            mapped_status = "available"
        if mapped_status not in stage_status_labels:
            mapped_status = "available"
        is_current = mapped_id == stage_id
        is_available = navigation_availability[mapped_id]
        if not is_available:
            mapped_status = "disabled"
        status_label = stage_status_labels[mapped_status]
        if not is_current and mapped_status == "available":
            status_label = "Next step" if int(mapped_stage["number"]) == int(WORKSPACE_STAGE_MAP[stage_id]["number"]) + 1 else "Available later"
        workspace_stages.append(
            {
                "id": mapped_id,
                "number": mapped_stage["number"],
                "name": mapped_stage["name"],
                "url": url_for(mapped_stage["endpoint"], **mapped_stage["url_values"]),
                "is_current": is_current,
                "available": is_available,
                "is_disabled": not is_available,
                "status": "current" if is_current else mapped_status,
                "status_label": "Current stage" if is_current else status_label,
            }
        )

    stage = WORKSPACE_STAGE_MAP[stage_id]
    previous_id = str(stage["previous_stage"])
    continue_id = str(stage["continue_stage"])
    previous_url = ""
    continue_url = ""
    if previous_id:
        previous_stage = WORKSPACE_STAGE_MAP[previous_id]
        previous_url = url_for(previous_stage["endpoint"], **previous_stage["url_values"])
    if continue_id:
        continue_stage = WORKSPACE_STAGE_MAP[continue_id]
        continue_url = url_for(continue_stage["endpoint"], **continue_stage["url_values"])
    continue_presentation = str(stage["continue_presentation"])
    continue_is_safe_link = bool(
        continue_presentation == "link"
        and continue_id
        and prerequisite_availability.get(continue_id, False)
    )
    continue_unavailable_message = ""
    if stage_id == "setup" and not prerequisite_availability["inventory"]:
        continue_unavailable_message = "Add an inventory source to continue."

    assessment_name = normalize_assessment_name(
        values.get("active_assessment_name", session.get("active_assessment_name", ""))
    )
    customer_name = normalize_customer_name(values.get("customer_name", session.get("customer_name", "")))
    active_assessment_id = _clean_assessment_id(
        values.get("active_assessment_id", session.get("active_assessment_id", ""))
    )

    context = dict(values)
    context.update(
        {
            "workspace_stage": stage_id,
            "workspace_stage_number": stage["number"],
            "workspace_stage_count": len(WORKSPACE_STAGE_MAP),
            "workspace_stage_name": stage["name"],
            "workspace_stages": workspace_stages,
            "workspace_readiness": workspace_readiness,
            "readiness": workspace_readiness,
            "workspace_assessment_name": assessment_name or "Untitled assessment",
            "workspace_customer_name": customer_name or "Customer not set",
            "workspace_is_saved": bool(active_assessment_id),
            "workspace_previous_url": previous_url,
            "workspace_continue_url": continue_url,
            "workspace_continue_presentation": continue_presentation,
            "workspace_continue_is_safe_link": continue_is_safe_link,
            "workspace_continue_unavailable_message": continue_unavailable_message,
            "workspace_continue_form_id": str(values.get("workspace_continue_form_id", "")),
            "workspace_continue_submit_name": str(values.get("workspace_continue_submit_name", "")),
            "workspace_continue_submit_value": str(values.get("workspace_continue_submit_value", "")),
            "workspace_continue_label": str(values.get("workspace_continue_label", "Continue")),
            "workspace_can_export": prerequisite_availability["results"],
        }
    )
    return context


def step4_tab_redirect(tab: str = "native", **native_query: Any) -> str:
    normalized_tab = normalize_step4_scenario_tab(tab, "native")
    if normalized_tab == "paths":
        return url_for("step3")
    values: dict[str, Any] = {"tab": normalized_tab}
    if normalized_tab == "native" and native_query:
        query = normalize_native_editor_query(native_query)
        values.update(
            {
                "native_page": query["page"],
                "native_page_size": query["page_size"],
                "native_search": query["search"],
                "native_support": query["support"],
            }
        )
    return url_for("step4", **values)


def normalize_hybrid_placement(value: Any, default: str = "ocvs") -> str:
    fallback = default if default in HYBRID_PLACEMENT_VALUES else "ocvs"
    placement = str(value or fallback).strip().lower()
    return placement if placement in HYBRID_PLACEMENT_VALUES else fallback


def build_hybrid_placement_plan(
    vm_rows: list[dict[str, Any]],
    hybrid_placement_selection: dict[str, Any] | None,
    supported_signatures: list[str],
) -> dict[str, Any]:
    support_source_available = bool(supported_signatures)
    selection = hybrid_placement_selection if isinstance(hybrid_placement_selection, dict) else {}
    rows: list[dict[str, Any]] = []

    for source_row in vm_rows:
        vm_name = str(source_row.get("vm_name", "")).strip()
        os_name = str(source_row.get("os_name", ""))
        is_unknown_os = _is_unknown_os(os_name)
        is_supported = bool(support_source_available and is_oci_supported_os(os_name, supported_signatures))
        support_state = (
            "review"
            if is_unknown_os or not support_source_available
            else "supported"
            if is_supported
            else "unsupported"
        )
        recommended = (
            "review"
            if support_state == "review"
            else "native"
            if is_supported
            else "ocvs"
        )
        placement = normalize_hybrid_placement(selection.get(vm_name), recommended)
        effective_target = "native" if placement == "native" else "ocvs"

        if placement == "review":
            reason = "Pending placement review; conservatively priced as OCVS"
            manual_override = placement != recommended
        elif placement == recommended:
            if recommended == "native":
                reason = "OCI-supported OS"
            elif support_source_available:
                reason = "Not OCI-native-supported"
            else:
                reason = "Support source missing; validate final target"
            manual_override = False
        else:
            reason = f"Manual override from {HYBRID_PLACEMENT_LABELS.get(recommended, recommended)} recommendation"
            manual_override = True

        rows.append(
            {
                **source_row,
                "hybrid_placement": placement,
                "hybrid_placement_field_name": inventory_placement_field_name(
                    "hybrid_placement",
                    vm_name,
                ),
                "hybrid_placement_label": HYBRID_PLACEMENT_LABELS.get(placement, placement),
                "hybrid_effective_target": effective_target,
                "hybrid_recommended_placement": recommended,
                "hybrid_recommended_label": HYBRID_PLACEMENT_LABELS.get(recommended, recommended),
                "hybrid_manual_override": manual_override,
                "hybrid_is_oci_supported": is_supported,
                "hybrid_support_state": support_state,
                "hybrid_reason": reason,
            }
        )

    native_rows = [row for row in rows if row["hybrid_effective_target"] == "native"]
    ocvs_rows = [row for row in rows if row["hybrid_effective_target"] == "ocvs"]
    review_rows = [row for row in rows if row["hybrid_placement"] == "review"]
    explicit_ocvs_rows = [row for row in rows if row["hybrid_placement"] == "ocvs"]
    manual_override_rows = [row for row in rows if row["hybrid_manual_override"]]

    return {
        "rows": rows,
        "native_rows": native_rows,
        "ocvs_rows": ocvs_rows,
        "review_rows": review_rows,
        "explicit_ocvs_rows": explicit_ocvs_rows,
        "manual_override_rows": manual_override_rows,
        "native_count": len(native_rows),
        "ocvs_count": len(explicit_ocvs_rows),
        "review_count": len(review_rows),
        "ocvs_priced_count": len(ocvs_rows),
        "manual_override_count": len(manual_override_rows),
        "support_source_available": support_source_available,
    }


def build_ocvs_price_summary(
    vm_rows: list[dict[str, Any]],
    price_lookup: dict[str, float],
    block_storage_unit_price: float,
    block_perf_unit_price: float,
    iaas_discount_pct: float,
    policy: dict[str, Any] | None = None,
    selected_profile: str = "best_fit",
    dr_node_count: int = 0,
    vmware_license_price_per_core_yearly: float = 0.0,
    ocvs_commitment_term: str = "payg",
) -> dict[str, Any]:
    """Size OCVS host options from selected VM totals and return the lowest-cost profile."""
    total_vcpus = sum(int(row.get("cpus", 0) or 0) for row in vm_rows)
    total_memory_gb = sum(int(row.get("memory_gb", 0) or 0) for row in vm_rows)
    total_storage_gb = sum(int(row.get("provisioned_gb", 0) or 0) for row in vm_rows)
    has_workload = bool(total_vcpus or total_memory_gb or total_storage_gb)

    policy = normalize_ocvs_policy(policy or OCVS_DEFAULT_SIZING_POLICY)
    selected_profile = normalize_ocvs_profile(selected_profile)
    ocvs_commitment_term = normalize_ocvs_commitment_term(ocvs_commitment_term)
    ocvs_commitment_label = OCVS_COMMITMENT_LABELS.get(ocvs_commitment_term, OCVS_COMMITMENT_LABELS["payg"])
    dr_node_count = normalize_ocvs_dr_nodes(dr_node_count)
    vcpu_per_ocpu = max(1.0, float(policy["vcpu_per_ocpu"]))
    cpu_headroom_factor = max(0.01, 1.0 - (float(policy["cpu_headroom_pct"]) / 100.0))
    memory_headroom_factor = max(0.01, 1.0 - (float(policy["memory_headroom_pct"]) / 100.0))
    storage_headroom_factor = max(0.01, 1.0 - (float(policy["storage_headroom_pct"]) / 100.0))
    dense_vsan_usable_factor = max(0.01, min(1.0, float(policy["dense_vsan_usable_pct"]) / 100.0))
    standard_storage_vpu = _bounded_int(policy.get("standard_storage_vpu"), 10, 10, 120)
    discount_factor = max(0.0, min(1.0, 1.0 - (float(iaas_discount_pct or 0.0) / 100.0)))
    price_per_core_yearly = max(0.0, float(vmware_license_price_per_core_yearly or 0.0))

    profile_results: list[dict[str, Any]] = []
    for profile in OCVS_HOST_PROFILES:
        ocpus = int(profile.get("ocpus", 0) or 0)
        memory_gb = int(profile.get("memory_gb", 0) or 0)
        nvme_tb = float(profile.get("nvme_tb", 0.0) or 0.0)
        min_hosts = int(profile.get("min_hosts", 1) or 1)
        max_hosts = int(profile.get("max_hosts", 0) or 0)
        host_type = str(profile.get("host_type", "Dense"))

        cpu_capacity_per_host = ocpus * vcpu_per_ocpu * cpu_headroom_factor
        memory_capacity_per_host = memory_gb * memory_headroom_factor
        hosts_by_cpu = _ceil_div_positive(total_vcpus, cpu_capacity_per_host)
        hosts_by_memory = _ceil_div_positive(total_memory_gb, memory_capacity_per_host)

        raw_storage_gb_per_host = nvme_tb * 1024.0
        dense_usable_storage_gb_per_host = raw_storage_gb_per_host * dense_vsan_usable_factor
        storage_capacity_per_host = dense_usable_storage_gb_per_host * storage_headroom_factor
        if host_type == "Standard":
            hosts_by_storage = 0
            storage_monthly_cost = (
                (total_storage_gb * float(block_storage_unit_price or 0.0))
                + (total_storage_gb * standard_storage_vpu * float(block_perf_unit_price or 0.0))
            ) * discount_factor
        else:
            hosts_by_storage = _ceil_div_positive(total_storage_gb, storage_capacity_per_host)
            storage_monthly_cost = 0.0

        if has_workload:
            base_host_count = max(min_hosts, hosts_by_cpu, hosts_by_memory, hosts_by_storage)
            applied_dr_node_count = dr_node_count
            host_count = base_host_count + applied_dr_node_count
        else:
            base_host_count = 0
            applied_dr_node_count = 0
            host_count = 0
            storage_monthly_cost = 0.0
        is_within_limit = max_hosts <= 0 or host_count <= max_hosts
        cluster_count = _ceil_div_positive(host_count, max_hosts) if max_hosts > 0 else (1 if host_count else 0)
        cluster_split_required = bool(max_hosts > 0 and host_count > max_hosts)

        ocpu_display_name = str(profile.get("ocpu_display_name", "")).strip()
        memory_display_name = str(profile.get("memory_display_name", "")).strip()
        nvme_display_name = str(profile.get("nvme_display_name", "")).strip()
        ocpu_unit_price = float(price_lookup.get(ocpu_display_name, 0.0))
        memory_unit_price = float(price_lookup.get(memory_display_name, 0.0))
        nvme_unit_price = float(price_lookup.get(nvme_display_name, 0.0))
        required_host_prices_available = bool(
            ocpu_unit_price > 0.0
            and memory_unit_price > 0.0
            and (nvme_tb <= 0.0 or (nvme_display_name and nvme_unit_price > 0.0))
        )
        required_storage_prices_available = bool(
            host_type != "Standard"
            or total_storage_gb <= 0
            or (block_storage_unit_price > 0.0 and block_perf_unit_price > 0.0)
        )
        commitment_discount_pct = ocvs_term_discount_pct(profile.get("shape", ""), ocvs_commitment_term)
        commitment_discount_factor = max(0.0, min(1.0, 1.0 - (commitment_discount_pct / 100.0)))
        host_monthly_cost = (
            (ocpus * ocpu_unit_price * HOURS_PER_MONTH)
            + (memory_gb * memory_unit_price * HOURS_PER_MONTH)
            + (nvme_tb * nvme_unit_price * HOURS_PER_MONTH)
        ) * discount_factor * commitment_discount_factor
        total_monthly_cost = (host_count * host_monthly_cost) + storage_monthly_cost
        physical_cores = host_count * ocpus
        vmware_license_yearly_cost = physical_cores * price_per_core_yearly
        vmware_license_monthly_cost = vmware_license_yearly_cost / 12.0
        selection_monthly_cost = total_monthly_cost + vmware_license_monthly_cost

        sizing_reasons = {
            "minimum": min_hosts,
            "cpu": hosts_by_cpu,
            "memory": hosts_by_memory,
            "storage": hosts_by_storage,
        }
        constraint = max(sizing_reasons, key=sizing_reasons.get) if has_workload else "none"
        if has_workload and sizing_reasons[constraint] < min_hosts:
            constraint = "minimum"

        total_cpu_capacity = max(1.0, host_count * ocpus * vcpu_per_ocpu)
        total_memory_capacity = max(1.0, host_count * memory_gb)
        if host_type == "Standard":
            total_storage_capacity = max(1.0, float(total_storage_gb or 1))
        else:
            total_storage_capacity = max(1.0, host_count * dense_usable_storage_gb_per_host)

        profile_results.append(
            {
                "shape": profile.get("shape", ""),
                "label": profile.get("label", ""),
                "host_type": host_type,
                "host_count": host_count,
                "base_host_count": base_host_count,
                "dr_node_count": applied_dr_node_count,
                "max_hosts": max_hosts,
                "is_within_limit": is_within_limit,
                "cluster_count": cluster_count,
                "cluster_split_required": cluster_split_required,
                "constraint": constraint,
                "hosts_by_cpu": hosts_by_cpu,
                "hosts_by_memory": hosts_by_memory,
                "hosts_by_storage": hosts_by_storage,
                "host_monthly_cost": host_monthly_cost,
                "storage_monthly_cost": storage_monthly_cost,
                "total_monthly_cost": total_monthly_cost,
                "physical_cores": physical_cores,
                "vmware_license_monthly_cost": vmware_license_monthly_cost,
                "vmware_license_yearly_cost": vmware_license_yearly_cost,
                "selection_monthly_cost": selection_monthly_cost,
                "commitment_term": ocvs_commitment_term,
                "commitment_label": ocvs_commitment_label,
                "commitment_discount_pct": commitment_discount_pct,
                "ocpus_per_host": ocpus,
                "memory_gb_per_host": memory_gb,
                "raw_storage_tb_per_host": nvme_tb,
                "usable_storage_gb_per_host": int(round(dense_usable_storage_gb_per_host)),
                "cpu_utilization_pct": min(999.0, (total_vcpus / total_cpu_capacity) * 100.0),
                "memory_utilization_pct": min(999.0, (total_memory_gb / total_memory_capacity) * 100.0),
                "storage_utilization_pct": min(999.0, (total_storage_gb / total_storage_capacity) * 100.0),
                "pricing_available": (
                    required_host_prices_available
                    and required_storage_prices_available
                ),
                "standard_storage_vpu": standard_storage_vpu,
            }
        )

    viable_results = [
        item
        for item in profile_results
        if item["host_count"] == 0 or item["pricing_available"] or item["total_monthly_cost"] > 0
    ]
    if selected_profile == "best_fit":
        selected = min(viable_results, key=lambda item: item["selection_monthly_cost"]) if viable_results else profile_results[0]
    else:
        selected = next((item for item in profile_results if item["shape"] == selected_profile), profile_results[0])

    return {
        "selected": selected,
        "profiles": profile_results,
        "selected_profile": selected_profile,
        "totals": {
            "vcpus": total_vcpus,
            "memory_gb": total_memory_gb,
            "storage_gb": total_storage_gb,
        },
        "policy": policy,
        "dr_node_count": dr_node_count,
        "commitment_term": ocvs_commitment_term,
        "commitment_label": ocvs_commitment_label,
    }


def summarize_native_price(vm_rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_cpu_ram_monthly_cost = sum(float(r["cpu_ram_monthly_cost"]) for r in vm_rows)
    total_storage_monthly_cost = sum(float(r["storage_monthly_cost"]) for r in vm_rows)
    total_os_license_monthly_cost = sum(float(r["os_license_monthly_cost"]) for r in vm_rows)
    total_monthly_cost = (
        total_cpu_ram_monthly_cost
        + total_storage_monthly_cost
        + total_os_license_monthly_cost
    )
    return {
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
        "total_cpu_ram_monthly_cost": total_cpu_ram_monthly_cost,
        "total_storage_monthly_cost": total_storage_monthly_cost,
        "total_os_license_monthly_cost": total_os_license_monthly_cost,
        "total_monthly_cost": total_monthly_cost,
    }


def build_native_shape_strategy_rows(vm_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize selected VMs by OS for the lightweight native default-shape modal."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in vm_rows:
        os_name = str(row.get("os_name") or "").strip() or "Unknown / Empty"
        bucket = grouped.setdefault(
            os_name,
            {
                "os_name": os_name,
                "vm_count": 0,
                "shape": str(row.get("oci_shape") or "").strip(),
                "burst": normalize_burst_value(row.get("burst")),
            },
        )
        bucket["vm_count"] = int(bucket.get("vm_count", 0) or 0) + 1

    return sorted(grouped.values(), key=lambda item: str(item.get("os_name", "")).lower())


def build_workload_summary(
    vm_rows: list[dict[str, Any]],
    supported_native_rows: list[dict[str, Any]],
    unsupported_ocvs_rows: list[dict[str, Any]],
    supported_os_source_available: bool,
) -> dict[str, Any]:
    def pct(part: int, total: int) -> float:
        return (float(part) / float(total) * 100.0) if total else 0.0

    def is_powered_on(row: dict[str, Any]) -> bool:
        state = str(row.get("power_state", "")).strip().lower().replace(" ", "")
        return state in {"on", "poweredon", "running"}

    def is_powered_off(row: dict[str, Any]) -> bool:
        state = str(row.get("power_state", "")).strip().lower().replace(" ", "")
        return state in {"off", "poweredoff", "stopped"}

    powered_on_count = sum(1 for row in vm_rows if is_powered_on(row))
    powered_off_count = sum(1 for row in vm_rows if is_powered_off(row))
    unknown_power_count = max(0, len(vm_rows) - powered_on_count - powered_off_count)
    supported_count = len(supported_native_rows)
    unsupported_count = len(unsupported_ocvs_rows)
    vm_count = len(vm_rows)
    total_vcpus = sum(int(row.get("cpus", 0) or 0) for row in vm_rows)
    total_memory_gb = sum(int(row.get("memory_gb", 0) or 0) for row in vm_rows)
    total_storage_gb = sum(int(row.get("provisioned_gb", 0) or 0) for row in vm_rows)
    top_os_rows, other_os_count = _top_os_distribution(vm_rows, 5)

    return {
        "vm_count": vm_count,
        "total_vcpus": total_vcpus,
        "total_memory_gb": total_memory_gb,
        "total_storage_gb": total_storage_gb,
        "powered_on_count": powered_on_count,
        "powered_off_count": powered_off_count,
        "unknown_power_count": unknown_power_count,
        "oci_supported_count": supported_count,
        "oci_not_supported_count": unsupported_count,
        "support_source_available": bool(supported_os_source_available),
        "top_os": _top_os_counts(vm_rows, 3) if vm_rows else "No selected VMs",
        "top_os_rows": top_os_rows,
        "other_os_count": other_os_count,
        "other_os_pct": pct(other_os_count, vm_count),
        "powered_on_pct": pct(powered_on_count, vm_count),
        "powered_off_pct": pct(powered_off_count, vm_count),
        "unknown_power_pct": pct(unknown_power_count, vm_count),
        "oci_supported_pct": pct(supported_count, vm_count),
        "oci_not_supported_pct": pct(unsupported_count, vm_count),
        "avg_vcpu_per_vm": (float(total_vcpus) / float(vm_count)) if vm_count else 0.0,
        "avg_memory_gb_per_vm": (float(total_memory_gb) / float(vm_count)) if vm_count else 0.0,
        "avg_storage_gb_per_vm": (float(total_storage_gb) / float(vm_count)) if vm_count else 0.0,
    }


def _top_os_counts(vm_rows: list[dict[str, Any]], limit: int = 4) -> str:
    counts: dict[str, int] = {}
    for row in vm_rows:
        os_name = str(row.get("os_name", "Unknown / Empty") or "Unknown / Empty")
        counts[os_name] = counts.get(os_name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    return ", ".join(f"{name} ({count})" for name, count in ranked[:limit])


def _top_os_distribution(vm_rows: list[dict[str, Any]], limit: int = 5) -> tuple[list[dict[str, Any]], int]:
    counts: dict[str, int] = {}
    total = len(vm_rows)
    for row in vm_rows:
        os_name = str(row.get("os_name", "Unknown / Empty") or "Unknown / Empty").strip() or "Unknown / Empty"
        counts[os_name] = counts.get(os_name, 0) + 1

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    top_items = ranked[:limit]
    rows = [
        {
            "os_name": name,
            "count": count,
            "pct": (float(count) / float(total) * 100.0) if total else 0.0,
        }
        for name, count in top_items
    ]
    other_count = max(0, total - sum(count for _, count in top_items))
    return rows, other_count


def build_fit_warnings(
    vm_rows: list[dict[str, Any]],
    unsupported_ocvs_rows: list[dict[str, Any]],
    scenario_comparison: dict[str, Any],
    overall: dict[str, Any],
    ocvs_price: dict[str, Any],
    hybrid_ocvs_price: dict[str, Any],
    source_pricelist_file: str,
    price_lookup: dict[str, float],
    block_storage_unit_price: float,
    block_perf_unit_price: float,
    windows_os_unit_price: float,
    vmware_license_summary: dict[str, Any],
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []

    def add(severity: str, title: str, detail: str) -> None:
        warnings.append({"severity": severity, "title": title, "detail": detail})

    def add_vcf_license_note(label: str, summary: dict[str, Any]) -> None:
        selected = summary["selected"]
        host_count = int(selected.get("host_count", 0) or 0)
        cores_per_host = int(selected.get("ocpus_per_host", 0) or 0)
        if host_count <= 0 or cores_per_host <= 0:
            return

        total_cores = host_count * cores_per_host
        raw_vsan_tb = float(selected.get("raw_storage_tb_per_host", 0.0) or 0.0) * host_count
        vsan_note = (
            f" VCF also includes 1 TiB of vSAN entitlement per licensed core; modeled raw dense storage is {raw_vsan_tb:,.1f} TB."
            if raw_vsan_tb > 0
            else ""
        )
        add(
            "info",
            f"{label} VCF license coverage",
            (
                f"Plan Broadcom VCF BYOL coverage for {total_cores:,} physical core/OCPU(s) "
                f"({host_count:,} node(s) x {cores_per_host:,}). VCF is licensed per physical core "
                "with a 16-core-per-processor minimum, and every server core must be covered."
                f"{vsan_note} Validate license portability, add-ons, and compliance with Broadcom and Oracle."
            ),
        )

    if not source_pricelist_file or not price_lookup:
        add("critical", "Missing active price list", "Costs can show as zero until an OCI price list is selected or downloaded.")

    missing_native_shapes = sorted(
        {
            str(row.get("oci_shape", ""))
            for row in vm_rows
            if float(row.get("ocpu_unit_price", 0.0)) <= 0.0 or float(row.get("memory_unit_price", 0.0)) <= 0.0
        }
    )
    if missing_native_shapes:
        add(
            "warning",
            "Native compute pricing incomplete",
            f"Missing CPU or RAM pricing for: {', '.join(missing_native_shapes[:4])}.",
        )

    if block_storage_unit_price <= 0.0 or block_perf_unit_price <= 0.0:
        add("warning", "Block Volume pricing incomplete", "Storage capacity or VPU pricing is missing from the active price list.")

    vmware_full = vmware_license_summary.get("ocvs", {})
    vmware_hybrid = vmware_license_summary.get("hybrid", {})
    if bool(vmware_license_summary.get("is_priced", False)):
        add(
            "info",
            "VCF license cost included",
            (
                f"Full OCVS license exposure covers {int(vmware_full.get('physical_cores', 0) or 0):,} physical core(s) "
                f"at {float(vmware_license_summary.get('price_per_core_yearly', 0.0) or 0.0):,.2f} list per core/year."
            ),
        )

    selected_ocvs = ocvs_price["selected"]
    hybrid_selected = hybrid_ocvs_price["selected"]
    if int(selected_ocvs.get("host_count", 0)) > 0 and not bool(selected_ocvs.get("pricing_available", False)):
        add("warning", "OCVS host pricing incomplete", f"Pricing was not found for {selected_ocvs.get('shape', 'the selected OCVS shape')}.")
    if int(hybrid_selected.get("host_count", 0)) > 0 and not bool(hybrid_selected.get("pricing_available", False)):
        add("warning", "Hybrid OCVS pricing incomplete", f"Pricing was not found for {hybrid_selected.get('shape', 'the hybrid OCVS shape')}.")

    vsan_mirroring_note_added = False
    for label, summary in (("OCVS", ocvs_price), ("Hybrid OCVS subset", hybrid_ocvs_price)):
        selected = summary["selected"]
        host_count = int(selected.get("host_count", 0) or 0)
        max_hosts = int(selected.get("max_hosts", 0) or 0)
        if max_hosts and host_count > max_hosts:
            add(
                "info",
                f"{label} multi-cluster planning",
                (
                    f"{selected.get('shape')} needs {host_count} node(s). Plan as a multi-cluster OCVS design "
                    "when the node count exceeds the cluster size limit."
                ),
            )

        constraint = str(selected.get("constraint", ""))
        if selected.get("host_type") == "Dense" and constraint == "storage":
            add(
                "warning",
                f"{label} is storage-driven",
                f"Storage requires {selected.get('hosts_by_storage')} node(s), more than CPU/RAM for {selected.get('shape')}.",
            )
        if selected.get("host_type") == "Dense" and host_count > 0:
            policy = summary.get("policy", {})
            add(
                "info",
                f"{label} vSAN mirroring assumption",
                (
                    f"Dense OCVS vSAN is modeled at {float(policy.get('dense_vsan_usable_pct', 50.0)):.0f}% usable "
                    "capacity to reflect FTT=1 RAID-1 mirroring before storage headroom is applied."
                ),
            )
            vsan_mirroring_note_added = True
        if selected.get("host_type") == "Standard" and host_count > 0:
            add(
                "info",
                f"{label} uses Block Volume storage",
                f"Standard OCVS storage is modeled separately at {selected.get('standard_storage_vpu')} VPU/GB.",
            )

    if not vsan_mirroring_note_added and any(
        str(item.get("host_type", "")) == "Dense" and int(item.get("host_count", 0) or 0) > 0
        for item in ocvs_price.get("profiles", [])
    ):
        policy = ocvs_price.get("policy", {})
        add(
            "info",
            "Dense OCVS vSAN mirroring assumption",
            (
                f"Dense shape comparisons use {float(policy.get('dense_vsan_usable_pct', 50.0)):.0f}% usable vSAN "
                "to reflect FTT=1 RAID-1 mirroring before storage headroom is applied."
            ),
        )

    if not bool(scenario_comparison.get("supported_os_source_available")):
        add("warning", "Hybrid OS support check unavailable", "OCI-SupportedOS.txt could not be loaded, so review the Hybrid placement planner before using the split.")
    elif unsupported_ocvs_rows:
        add(
            "info",
            "Hybrid OCVS placement scope",
            f"{len(unsupported_ocvs_rows):,} selected VM(s) are routed to OCVS in the Hybrid path. Top OS examples: {_top_os_counts(unsupported_ocvs_rows)}.",
        )

    windows_server_rows = [row for row in vm_rows if bool(row.get("is_windows_server"))]
    license_included_rows = [row for row in windows_server_rows if str(row.get("os_license", "")) == "Lic Include"]
    windows_license_monthly = float(overall.get("total_os_license_monthly_cost", 0.0))
    if license_included_rows and windows_os_unit_price <= 0.0:
        add("warning", "Windows license pricing missing", "Some Windows Server VMs use license-included pricing, but the Windows OS price was not found.")
    elif windows_license_monthly > 0.0:
        add(
            "info",
            "Windows license impact included",
            f"{len(license_included_rows):,} Windows Server VM(s) add OS license cost to the OCI Native path.",
        )
    elif windows_server_rows:
        add(
            "info",
            "Windows Server modeled as BYOL",
            f"{len(windows_server_rows):,} Windows Server VM(s) are currently using BYOL in the Native estimate.",
        )

    add_vcf_license_note("OCVS", ocvs_price)
    add_vcf_license_note("Hybrid OCVS subset", hybrid_ocvs_price)

    return warnings


def build_executive_summary(
    scenario_comparison: dict[str, Any],
    ocvs_price: dict[str, Any],
    hybrid_ocvs_price: dict[str, Any],
    fit_warnings: list[dict[str, str]],
) -> dict[str, Any]:
    best = scenario_comparison["best"]
    best_id = str(best.get("id", ""))
    delta = float(best.get("monthly_delta", 0.0))
    critical_count = sum(1 for item in fit_warnings if item.get("severity") == "critical")
    warning_count = sum(1 for item in fit_warnings if item.get("severity") == "warning")

    if best_id == "native":
        selected_ocvs = ocvs_price["selected"]
        driver = (
            "Native is currently lowest; the full OCVS path is "
            f"{selected_ocvs.get('host_count')} node(s), driven by {selected_ocvs.get('constraint')}."
        )
    elif best_id == "ocvs":
        selected_ocvs = ocvs_price["selected"]
        driver = (
            f"All selected VMs fit on {selected_ocvs.get('host_count')} x {selected_ocvs.get('shape')}, "
            f"driven by {selected_ocvs.get('constraint')}."
        )
    else:
        selected_ocvs = hybrid_ocvs_price["selected"]
        driver = (
            f"{best.get('native_vm_count')} VM(s) move to OCI Native and {best.get('ocvs_vm_count')} VM(s) remain on "
            f"{selected_ocvs.get('host_count')} x {selected_ocvs.get('shape')}."
        )

    if critical_count:
        confidence = f"Needs review: {critical_count} critical fit issue(s)"
    elif warning_count:
        confidence = f"Review recommended: {warning_count} warning(s)"
    else:
        confidence = "No blocking fit issues detected"

    return {
        "recommended_path": best.get("label", ""),
        "decision_label": "Lowest monthly cost path",
        "decision_note": (
            "Cost-ranked result only; validate migration waves, app dependencies, downtime, "
            "licensing, and support constraints before positioning it as the target path."
        ),
        "monthly_cost": float(best.get("monthly_cost", 0.0)),
        "yearly_cost": float(best.get("yearly_cost", 0.0)),
        "monthly_delta": delta,
        "yearly_delta": delta * 12.0,
        "driver": driver,
        "confidence": confidence,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "info_count": sum(1 for item in fit_warnings if item.get("severity") == "info"),
    }


def _format_display_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


def build_migration_waves(
    *,
    vm_rows: list[dict[str, Any]],
    supported_native_rows: list[dict[str, Any]],
    unsupported_ocvs_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    supported_names = {str(row.get("vm_name", "")) for row in supported_native_rows}
    unsupported_names = {str(row.get("vm_name", "")) for row in unsupported_ocvs_rows}

    def is_powered_off(row: dict[str, Any]) -> bool:
        state = str(row.get("power_state", "")).strip().lower()
        return state in {"off", "powered off", "poweredoff"} or "off" in state

    def needs_native_review(row: dict[str, Any]) -> bool:
        return (
            bool(row.get("is_windows_server"))
            or int(row.get("cpus", 0) or 0) >= 16
            or int(row.get("memory_gb", 0) or 0) >= 256
            or int(row.get("provisioned_gb", 0) or 0) >= 2048
        )

    powered_off_rows = [row for row in vm_rows if is_powered_off(row)]
    active_rows = [row for row in vm_rows if not is_powered_off(row)]
    native_active = [row for row in active_rows if str(row.get("vm_name", "")) in supported_names]
    native_review_rows = [row for row in native_active if needs_native_review(row)]
    native_review_names = {str(row.get("vm_name", "")) for row in native_review_rows}
    native_quick_rows = [row for row in native_active if str(row.get("vm_name", "")) not in native_review_names]
    ocvs_rows = [row for row in active_rows if str(row.get("vm_name", "")) in unsupported_names]

    def summarize(rows: list[dict[str, Any]], wave: int, title: str, target: str, action: str) -> dict[str, Any]:
        sorted_rows = sorted(rows, key=lambda row: str(row.get("vm_name", "")).lower())
        return {
            "wave": wave,
            "title": title,
            "target": target,
            "action": action,
            "vm_count": len(rows),
            "vcpus": int(sum(int(row.get("cpus", 0) or 0) for row in rows)),
            "memory_gb": int(sum(int(row.get("memory_gb", 0) or 0) for row in rows)),
            "storage_gb": int(sum(int(row.get("provisioned_gb", 0) or 0) for row in rows)),
            "top_os": _top_os_counts(rows, limit=3) if rows else "No VMs in this wave",
            "sample_vms": ", ".join(str(row.get("vm_name", "")) for row in sorted_rows[:5]) if rows else "",
        }

    waves = [
        summarize(
            native_quick_rows,
            1,
            "Native quick candidates",
            "OCI Native",
            "Validate app owner, backup, monitoring, and target shape; good first migration wave.",
        ),
        summarize(
            native_review_rows,
            2,
            "Native validation candidates",
            "OCI Native review",
            "Check Windows licensing, large resource profiles, dependencies, and performance before migration.",
        ),
        summarize(
            ocvs_rows,
            3,
            "OCVS landing-zone candidates",
            "OCVS",
            "Keep VMware compatibility or plan OS/app remediation before moving native.",
        ),
        summarize(
            powered_off_rows,
            4,
            "Defer, archive, or retire",
            "Governance",
            "Confirm ownership and business need before including these VMs in the migration scope.",
        ),
    ]
    max_count = max((int(wave["vm_count"]) for wave in waves), default=0)
    for wave in waves:
        wave["bar_pct"] = _pct_of_max(float(wave["vm_count"]), float(max_count), minimum=4.0) if max_count else 0.0

    return {
        "waves": waves,
        "total_vms": len(vm_rows),
        "native_candidate_count": len(native_quick_rows) + len(native_review_rows),
        "ocvs_candidate_count": len(ocvs_rows),
        "powered_off_count": len(powered_off_rows),
    }


def _pct_of_max(value: float, maximum: float, minimum: float = 2.0) -> float:
    if maximum <= 0:
        return 0.0
    if value <= 0:
        return 0.0
    return max(minimum, min(100.0, (value / maximum) * 100.0))


def build_ocvs_shape_comparison(ocvs_price: dict[str, Any]) -> dict[str, Any]:
    profiles = list(ocvs_price.get("profiles", []))
    selected_shape = str(ocvs_price.get("selected", {}).get("shape", ""))
    max_monthly = max((float(item.get("selection_monthly_cost", item.get("total_monthly_cost", 0.0)) or 0.0) for item in profiles), default=0.0)
    viable_profiles = [
        item
        for item in profiles
        if int(item.get("host_count", 0) or 0) == 0
        or bool(item.get("pricing_available", False))
        or float(item.get("total_monthly_cost", 0.0) or 0.0) > 0.0
    ]
    best_fit_shape = ""
    if viable_profiles:
        best_fit_shape = str(
            min(
                viable_profiles,
                key=lambda item: float(item.get("selection_monthly_cost", item.get("total_monthly_cost", 0.0)) or 0.0),
            ).get("shape", "")
        )
    rows: list[dict[str, Any]] = []

    for item in profiles:
        host_count = int(item.get("host_count", 0) or 0)
        base_host_count = int(item.get("base_host_count", host_count) or 0)
        dr_node_count = int(item.get("dr_node_count", 0) or 0)
        host_monthly_cost = float(item.get("host_monthly_cost", 0.0) or 0.0)
        host_total_monthly_cost = host_count * host_monthly_cost
        storage_monthly_cost = float(item.get("storage_monthly_cost", 0.0) or 0.0)
        total_monthly_cost = float(item.get("total_monthly_cost", 0.0) or 0.0)
        vmware_license_monthly_cost = float(item.get("vmware_license_monthly_cost", 0.0) or 0.0)
        selection_monthly_cost = float(item.get("selection_monthly_cost", total_monthly_cost) or 0.0)
        host_type = str(item.get("host_type", ""))
        storage_model = "vSAN local NVMe" if host_type == "Dense" else f"OCI Block Volume at {item.get('standard_storage_vpu', 10)} VPU/GB"

        rows.append(
            {
                "shape": item.get("shape", ""),
                "label": item.get("label", ""),
                "host_type": host_type,
                "host_count": host_count,
                "base_host_count": base_host_count,
                "dr_node_count": dr_node_count,
                "max_hosts": int(item.get("max_hosts", 0) or 0),
                "cluster_count": int(item.get("cluster_count", 0) or 0),
                "cluster_split_required": bool(item.get("cluster_split_required", False)),
                "host_monthly_cost": host_monthly_cost,
                "host_total_monthly_cost": host_total_monthly_cost,
                "storage_monthly_cost": storage_monthly_cost,
                "total_monthly_cost": total_monthly_cost,
                "vmware_license_monthly_cost": vmware_license_monthly_cost,
                "vmware_license_yearly_cost": float(item.get("vmware_license_yearly_cost", 0.0) or 0.0),
                "selection_monthly_cost": selection_monthly_cost,
                "selection_yearly_cost": selection_monthly_cost * 12.0,
                "physical_cores": int(item.get("physical_cores", 0) or 0),
                "monthly_bar_pct": _pct_of_max(selection_monthly_cost, max_monthly),
                "constraint": item.get("constraint", ""),
                "pricing_available": bool(item.get("pricing_available", False)),
                "is_within_limit": bool(item.get("is_within_limit", False)),
                "is_selected": str(item.get("shape", "")) == selected_shape,
                "is_best_fit": str(item.get("shape", "")) == best_fit_shape,
                "storage_model": storage_model,
                "ocpus_per_host": int(item.get("ocpus_per_host", 0) or 0),
                "memory_gb_per_host": int(item.get("memory_gb_per_host", 0) or 0),
                "usable_storage_gb_per_host": int(item.get("usable_storage_gb_per_host", 0) or 0),
            }
        )

    return {"rows": rows, "max_monthly_cost": max_monthly, "best_fit_shape": best_fit_shape}


def build_vmware_license_summary(
    ocvs_price: dict[str, Any],
    hybrid_ocvs_price: dict[str, Any],
    price_per_core_yearly: float,
    hybrid_price_per_core_yearly: Any = None,
) -> dict[str, Any]:
    price_per_core_yearly = max(0.0, float(price_per_core_yearly or 0.0))
    if hybrid_price_per_core_yearly is None:
        hybrid_price_per_core_yearly = price_per_core_yearly
    hybrid_price_per_core_yearly = max(0.0, float(hybrid_price_per_core_yearly or 0.0))

    def build_item(label: str, summary: dict[str, Any], item_price_per_core_yearly: float) -> dict[str, Any]:
        selected = summary.get("selected", {})
        host_count = int(selected.get("host_count", 0) or 0)
        base_host_count = int(selected.get("base_host_count", host_count) or 0)
        dr_node_count = int(selected.get("dr_node_count", 0) or 0)
        cores_per_host = int(selected.get("ocpus_per_host", 0) or 0)
        physical_cores = host_count * cores_per_host
        yearly_cost = physical_cores * item_price_per_core_yearly
        monthly_cost = yearly_cost / 12.0
        return {
            "label": label,
            "host_count": host_count,
            "base_host_count": base_host_count,
            "dr_node_count": dr_node_count,
            "cores_per_host": cores_per_host,
            "physical_cores": physical_cores,
            "price_per_core_yearly": item_price_per_core_yearly,
            "is_priced": item_price_per_core_yearly > 0.0,
            "yearly_cost": yearly_cost,
            "monthly_cost": monthly_cost,
        }

    ocvs = build_item("OCVS", ocvs_price, price_per_core_yearly)
    hybrid = build_item("Hybrid OCVS subset", hybrid_ocvs_price, hybrid_price_per_core_yearly)
    return {
        "price_per_core_yearly": price_per_core_yearly,
        "hybrid_price_per_core_yearly": hybrid_price_per_core_yearly,
        "is_priced": price_per_core_yearly > 0.0,
        "all_priced": price_per_core_yearly > 0.0 and hybrid_price_per_core_yearly > 0.0,
        "ocvs": ocvs,
        "hybrid": hybrid,
        "rows": [ocvs, hybrid],
    }


def build_scenario_chart_rows(scenario_comparison: dict[str, Any]) -> list[dict[str, Any]]:
    rows = list(scenario_comparison.get("rows", []))
    max_monthly = max((float(item.get("monthly_cost", 0.0) or 0.0) for item in rows), default=0.0)
    max_three_year = max((float(item.get("yearly_cost", 0.0) or 0.0) * 3.0 for item in rows), default=0.0)
    chart_rows: list[dict[str, Any]] = []
    for item in rows:
        row = dict(item)
        monthly_cost = float(item.get("monthly_cost", 0.0) or 0.0)
        yearly_cost = float(item.get("yearly_cost", 0.0) or 0.0)
        three_year_cost = yearly_cost * 3.0
        native_vm_count = int(item.get("native_vm_count", 0) or 0)
        ocvs_vm_count = int(item.get("ocvs_vm_count", 0) or 0)
        total_vm_count = max(0, native_vm_count + ocvs_vm_count)
        row["bar_pct"] = _pct_of_max(monthly_cost, max_monthly)
        row["three_year_cost"] = three_year_cost
        row["three_year_bar_pct"] = _pct_of_max(three_year_cost, max_three_year)
        row["native_vm_pct"] = (native_vm_count / total_vm_count * 100.0) if total_vm_count else 0.0
        row["ocvs_vm_pct"] = (ocvs_vm_count / total_vm_count * 100.0) if total_vm_count else 0.0
        row["cost_per_vm"] = (monthly_cost / total_vm_count) if total_vm_count else 0.0
        chart_rows.append(row)
    return chart_rows


def _build_breakdown_segments(components: list[dict[str, Any]]) -> tuple[float, list[dict[str, Any]]]:
    total = sum(float(item.get("value", 0.0) or 0.0) for item in components)
    segments: list[dict[str, Any]] = []
    for item in components:
        value = float(item.get("value", 0.0) or 0.0)
        segments.append({**item, "pct": (value / total * 100.0) if total > 0 else 0.0})
    return total, segments


def build_cost_breakdown_rows(
    overall: dict[str, Any],
    supported_native_summary: dict[str, Any],
    ocvs_price: dict[str, Any],
    hybrid_ocvs_price: dict[str, Any],
    vmware_license_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    ocvs_selected = ocvs_price["selected"]
    hybrid_selected = hybrid_ocvs_price["selected"]
    vmware_ocvs = vmware_license_summary.get("ocvs", {})
    vmware_hybrid = vmware_license_summary.get("hybrid", {})
    rows_seed = [
        {
            "label": "OCI Native",
            "components": [
                {"label": "Compute", "value": float(overall.get("total_cpu_ram_monthly_cost", 0.0)), "class": "seg-compute"},
                {"label": "Storage", "value": float(overall.get("total_storage_monthly_cost", 0.0)), "class": "seg-storage"},
                {"label": "OS license", "value": float(overall.get("total_os_license_monthly_cost", 0.0)), "class": "seg-license"},
            ],
        },
        {
            "label": "OCVS",
            "components": [
                {
                    "label": "OCVS nodes",
                    "value": int(ocvs_selected.get("host_count", 0) or 0) * float(ocvs_selected.get("host_monthly_cost", 0.0) or 0.0),
                    "class": "seg-hosts",
                },
                {"label": "Datastore", "value": float(ocvs_selected.get("storage_monthly_cost", 0.0) or 0.0), "class": "seg-ocvs-storage"},
                {"label": "VCF license", "value": float(vmware_ocvs.get("monthly_cost", 0.0) or 0.0), "class": "seg-vmware-license"},
            ],
        },
        {
            "label": "Hybrid",
            "components": [
                {"label": "Native compute", "value": float(supported_native_summary.get("total_cpu_ram_monthly_cost", 0.0)), "class": "seg-compute"},
                {"label": "Native storage", "value": float(supported_native_summary.get("total_storage_monthly_cost", 0.0)), "class": "seg-storage"},
                {"label": "OS license", "value": float(supported_native_summary.get("total_os_license_monthly_cost", 0.0)), "class": "seg-license"},
                {
                    "label": "OCVS nodes",
                    "value": int(hybrid_selected.get("host_count", 0) or 0) * float(hybrid_selected.get("host_monthly_cost", 0.0) or 0.0),
                    "class": "seg-hosts",
                },
                {"label": "OCVS datastore", "value": float(hybrid_selected.get("storage_monthly_cost", 0.0) or 0.0), "class": "seg-ocvs-storage"},
                {"label": "VCF license", "value": float(vmware_hybrid.get("monthly_cost", 0.0) or 0.0), "class": "seg-vmware-license"},
            ],
        },
    ]
    totals_and_segments = [_build_breakdown_segments(row["components"]) for row in rows_seed]
    max_total = max((total for total, _segments in totals_and_segments), default=0.0)
    rows: list[dict[str, Any]] = []
    for row, (total, segments) in zip(rows_seed, totals_and_segments):
        rows.append({**row, "total": total, "segments": segments, "bar_pct": _pct_of_max(total, max_total)})
    return rows


def build_executive_insights(
    scenario_comparison: dict[str, Any],
    executive_summary: dict[str, Any],
    ocvs_price: dict[str, Any],
    hybrid_ocvs_price: dict[str, Any],
    fit_warnings: list[dict[str, str]],
    vmware_license_summary: dict[str, Any],
) -> list[dict[str, str]]:
    best = scenario_comparison.get("best", {})
    ocvs_selected = ocvs_price["selected"]
    hybrid_selected = hybrid_ocvs_price["selected"]
    insights: list[dict[str, str]] = [
        {
            "title": "Lowest-cost path",
            "detail": (
                f"{executive_summary.get('driver', '')} Treat this as a cost ranking until dependencies, "
                "migration waves, downtime, and commercial terms are validated."
            ),
        },
        {
            "title": "Hybrid split",
            "detail": (
                f"{scenario_comparison.get('supported_vm_count', 0):,} VM(s) are placed on OCI Native; "
                f"{scenario_comparison.get('unsupported_vm_count', 0):,} VM(s) are priced on OCVS."
            ),
        },
        {
            "title": "OCVS sizing driver",
            "detail": (
                f"Full OCVS uses {ocvs_selected.get('host_count')} x {ocvs_selected.get('shape')} driven by {ocvs_selected.get('constraint')}; "
                f"hybrid OCVS uses {hybrid_selected.get('host_count')} x {hybrid_selected.get('shape')}."
            ),
        },
    ]

    if ocvs_selected.get("host_type") == "Dense":
        policy = ocvs_price.get("policy", {})
        insights.append(
            {
                "title": "vSAN capacity policy",
                "detail": (
                    f"Dense OCVS uses {float(policy.get('dense_vsan_usable_pct', 50.0)):.0f}% usable vSAN to reflect "
                    f"FTT=1 RAID-1 mirroring, then reserves {float(policy.get('storage_headroom_pct', 25.0)):.0f}% storage headroom."
                ),
            }
        )

    if int(ocvs_selected.get("host_count", 0) or 0) > 0:
        total_cores = int(ocvs_selected.get("host_count", 0) or 0) * int(ocvs_selected.get("ocpus_per_host", 0) or 0)
        insights.append(
            {
                "title": "VCF license coverage",
                "detail": f"Full OCVS requires planning for {total_cores:,} physical core/OCPU(s), subject to Broadcom VCF per-core terms.",
            }
        )

    vmware_full = vmware_license_summary.get("ocvs", {})
    if int(vmware_full.get("physical_cores", 0) or 0) > 0:
        if bool(vmware_license_summary.get("is_priced", False)):
            insights.append(
                {
                    "title": "VCF license run-rate",
                    "detail": (
                        f"Full OCVS adds {float(vmware_full.get('yearly_cost', 0.0) or 0.0):,.0f} per year "
                        f"for {int(vmware_full.get('physical_cores', 0) or 0):,} physical core(s)."
                    ),
                }
            )
        else:
            insights.append(
                {
                    "title": "VCF license assumption",
                    "detail": "Enter a VCF list price per physical core/year to include license run-rate in OCVS and Hybrid costs.",
                }
            )

    if fit_warnings:
        critical = sum(1 for item in fit_warnings if item.get("severity") == "critical")
        warnings = sum(1 for item in fit_warnings if item.get("severity") == "warning")
        insights.append(
                {
                    "title": "Review focus",
                    "detail": f"{critical} critical item(s) and {warnings} warning(s) need review before using this as a final recommendation.",
                }
            )

    if str(best.get("id", "")) != "native":
        delta = float(best.get("monthly_delta", 0.0) or 0.0)
        direction = "saves" if delta < 0 else "costs"
        insights.append(
            {
                "title": "Run-rate delta",
                "detail": f"The lowest-cost path {direction} {abs(delta):,.0f} per month against the OCI Native baseline.",
            }
        )

    return insights


def build_price_analysis_from_rows(
    vm_rows: list[dict[str, Any]],
    price_lookup: dict[str, float],
    block_storage_unit_price: float,
    block_perf_unit_price: float,
    windows_os_unit_price: float,
    iaas_discount_pct: float,
    ocvs_policy: dict[str, Any],
    ocvs_profile_choice: str,
    source_pricelist_file: str,
    vmware_license_price_per_core_yearly: float,
    ocvs_dr_nodes: int,
    ocvs_commitment_term: str = "payg",
    hybrid_ocvs_policy: dict[str, Any] | None = None,
    hybrid_ocvs_profile_choice: str | None = None,
    hybrid_vmware_license_price_per_core_yearly: float | None = None,
    hybrid_ocvs_dr_nodes: int | None = None,
    hybrid_ocvs_commitment_term: str | None = None,
    hybrid_placement_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hybrid_ocvs_policy = normalize_ocvs_policy(hybrid_ocvs_policy or ocvs_policy)
    hybrid_ocvs_profile_choice = normalize_ocvs_profile(
        hybrid_ocvs_profile_choice or ocvs_profile_choice
    )
    hybrid_vmware_license_price_per_core_yearly = _bounded_float(
        (
            vmware_license_price_per_core_yearly
            if hybrid_vmware_license_price_per_core_yearly is None
            else hybrid_vmware_license_price_per_core_yearly
        ),
        vmware_license_price_per_core_yearly,
        0.0,
        1_000_000.0,
    )
    hybrid_ocvs_dr_nodes = normalize_ocvs_dr_nodes(
        ocvs_dr_nodes if hybrid_ocvs_dr_nodes is None else hybrid_ocvs_dr_nodes
    )
    hybrid_ocvs_commitment_term = normalize_ocvs_commitment_term(
        hybrid_ocvs_commitment_term or ocvs_commitment_term
    )
    overall = summarize_native_price(vm_rows)
    ocvs_price = build_ocvs_price_summary(
        vm_rows=vm_rows,
        price_lookup=price_lookup,
        block_storage_unit_price=block_storage_unit_price,
        block_perf_unit_price=block_perf_unit_price,
        iaas_discount_pct=iaas_discount_pct,
        policy=ocvs_policy,
        selected_profile=ocvs_profile_choice,
        dr_node_count=ocvs_dr_nodes,
        vmware_license_price_per_core_yearly=vmware_license_price_per_core_yearly,
        ocvs_commitment_term=ocvs_commitment_term,
    )
    ocvs_selected = ocvs_price["selected"]
    supported_signatures = load_supported_os_signatures()
    oci_supported_rows = [
        row
        for row in vm_rows
        if supported_signatures and is_oci_supported_os(str(row.get("os_name", "")), supported_signatures)
    ]
    oci_supported_names = {str(row.get("vm_name", "")) for row in oci_supported_rows}
    oci_unsupported_rows = [row for row in vm_rows if str(row.get("vm_name", "")) not in oci_supported_names]

    hybrid_placement_plan = build_hybrid_placement_plan(
        vm_rows=vm_rows,
        hybrid_placement_selection=hybrid_placement_selection,
        supported_signatures=supported_signatures,
    )
    supported_native_rows = list(hybrid_placement_plan["native_rows"])
    unsupported_ocvs_rows = list(hybrid_placement_plan["ocvs_rows"])
    hybrid_review_rows = list(hybrid_placement_plan["review_rows"])
    supported_native_summary = summarize_native_price(supported_native_rows)
    hybrid_ocvs_price = build_ocvs_price_summary(
        vm_rows=unsupported_ocvs_rows,
        price_lookup=price_lookup,
        block_storage_unit_price=block_storage_unit_price,
        block_perf_unit_price=block_perf_unit_price,
        iaas_discount_pct=iaas_discount_pct,
        policy=hybrid_ocvs_policy,
        selected_profile=hybrid_ocvs_profile_choice,
        dr_node_count=hybrid_ocvs_dr_nodes,
        vmware_license_price_per_core_yearly=hybrid_vmware_license_price_per_core_yearly,
        ocvs_commitment_term=hybrid_ocvs_commitment_term,
    )
    hybrid_ocvs_selected = hybrid_ocvs_price["selected"]
    vmware_license_summary = build_vmware_license_summary(
        ocvs_price=ocvs_price,
        hybrid_ocvs_price=hybrid_ocvs_price,
        price_per_core_yearly=vmware_license_price_per_core_yearly,
        hybrid_price_per_core_yearly=hybrid_vmware_license_price_per_core_yearly,
    )

    baseline_monthly = float(overall["total_monthly_cost"])
    ocvs_monthly = float(ocvs_selected.get("total_monthly_cost", 0.0)) + float(
        vmware_license_summary["ocvs"].get("monthly_cost", 0.0)
    )
    hybrid_monthly = float(supported_native_summary["total_monthly_cost"]) + float(
        hybrid_ocvs_selected.get("total_monthly_cost", 0.0)
    ) + float(
        vmware_license_summary["hybrid"].get("monthly_cost", 0.0)
    )
    ocvs_host_count = int(ocvs_selected.get("host_count", 0) or 0)
    hybrid_ocvs_host_count = int(hybrid_ocvs_selected.get("host_count", 0) or 0)
    native_viable = bool(not vm_rows or baseline_monthly > 0.0)
    ocvs_viable = bool(
        (ocvs_host_count == 0 or bool(ocvs_selected.get("pricing_available", False)))
    )
    hybrid_viable = bool(
        (hybrid_ocvs_host_count == 0 or bool(hybrid_ocvs_selected.get("pricing_available", False)))
    )

    scenario_rows = [
        {
            "id": "native",
            "label": "OCI Native",
            "monthly_cost": baseline_monthly,
            "yearly_cost": baseline_monthly * 12.0,
            "monthly_delta": 0.0,
            "native_vm_count": len(vm_rows),
            "ocvs_vm_count": 0,
            "sizing_basis": f"{len(vm_rows):,} selected VMs sized to flexible OCI compute and block volume storage",
            "detail": "Baseline",
            "is_viable": native_viable,
        },
        {
            "id": "ocvs",
            "label": "OCVS",
            "monthly_cost": ocvs_monthly,
            "yearly_cost": ocvs_monthly * 12.0,
            "monthly_delta": ocvs_monthly - baseline_monthly,
            "native_vm_count": 0,
            "ocvs_vm_count": len(vm_rows),
            "sizing_basis": f"{ocvs_selected['host_count']} x {ocvs_selected['shape']} ({ocvs_selected['label']}), driven by {ocvs_selected['constraint']}",
            "detail": (
                f"All selected VMs remain on VMware; VCF license {vmware_license_summary['ocvs']['physical_cores']:,} cores"
            ),
            "is_viable": ocvs_viable,
        },
        {
            "id": "hybrid",
            "label": "Hybrid",
            "monthly_cost": hybrid_monthly,
            "yearly_cost": hybrid_monthly * 12.0,
            "monthly_delta": hybrid_monthly - baseline_monthly,
            "native_vm_count": len(supported_native_rows),
            "ocvs_vm_count": len(unsupported_ocvs_rows),
            "sizing_basis": (
                f"{len(supported_native_rows):,} VM(s) to Native; "
                f"{len(unsupported_ocvs_rows):,} VM(s) priced on OCVS"
            ),
            "detail": (
                f"OCVS subset: {hybrid_ocvs_selected['host_count']} x {hybrid_ocvs_selected['shape']}; "
                f"VCF license {vmware_license_summary['hybrid']['physical_cores']:,} cores"
                if unsupported_ocvs_rows
                else "No VMs currently priced on OCVS in the Hybrid placement planner"
            ),
            "is_viable": hybrid_viable,
        },
    ]
    viable_scenarios = [scenario for scenario in scenario_rows if bool(scenario.get("is_viable", False))]
    best_scenario = min(viable_scenarios or scenario_rows, key=lambda item: float(item["monthly_cost"]))
    scenario_cost_pool = viable_scenarios or scenario_rows
    monthly_cost_values = [float(item.get("monthly_cost", 0.0) or 0.0) for item in scenario_cost_pool]
    lowest_monthly_cost = min(monthly_cost_values, default=0.0)
    highest_monthly_cost = max(monthly_cost_values, default=0.0)
    scenario_comparison = {
        "rows": scenario_rows,
        "best": best_scenario,
        "supported_vm_count": len(supported_native_rows),
        "unsupported_vm_count": len(unsupported_ocvs_rows),
        "review_vm_count": len(hybrid_review_rows),
        "manual_override_count": int(hybrid_placement_plan.get("manual_override_count", 0) or 0),
        "supported_os_source_available": bool(supported_signatures),
        "monthly_spread": max(0.0, highest_monthly_cost - lowest_monthly_cost),
        "three_year_spread": max(0.0, (highest_monthly_cost - lowest_monthly_cost) * 36.0),
    }
    workload_summary = build_workload_summary(
        vm_rows=vm_rows,
        supported_native_rows=oci_supported_rows,
        unsupported_ocvs_rows=oci_unsupported_rows,
        supported_os_source_available=bool(supported_signatures),
    )
    price_comparison = {
        "native_monthly_cost": baseline_monthly,
        "native_yearly_cost": baseline_monthly * 12.0,
        "ocvs_monthly_cost": ocvs_monthly,
        "ocvs_yearly_cost": ocvs_monthly * 12.0,
        "hybrid_monthly_cost": hybrid_monthly,
        "hybrid_yearly_cost": hybrid_monthly * 12.0,
        "monthly_delta": ocvs_monthly - baseline_monthly,
        "yearly_delta": (ocvs_monthly - baseline_monthly) * 12.0,
        "vmware_license_monthly_cost": float(vmware_license_summary["ocvs"].get("monthly_cost", 0.0)),
        "vmware_license_yearly_cost": float(vmware_license_summary["ocvs"].get("yearly_cost", 0.0)),
    }
    fit_warnings = build_fit_warnings(
        vm_rows=vm_rows,
        unsupported_ocvs_rows=unsupported_ocvs_rows,
        scenario_comparison=scenario_comparison,
        overall=overall,
        ocvs_price=ocvs_price,
        hybrid_ocvs_price=hybrid_ocvs_price,
        source_pricelist_file=source_pricelist_file,
        price_lookup=price_lookup,
        block_storage_unit_price=block_storage_unit_price,
        block_perf_unit_price=block_perf_unit_price,
        windows_os_unit_price=windows_os_unit_price,
        vmware_license_summary=vmware_license_summary,
    )
    executive_summary = build_executive_summary(
        scenario_comparison=scenario_comparison,
        ocvs_price=ocvs_price,
        hybrid_ocvs_price=hybrid_ocvs_price,
        fit_warnings=fit_warnings,
    )

    return {
        "overall": overall,
        "ocvs_price": ocvs_price,
        "hybrid_ocvs_price": hybrid_ocvs_price,
        "supported_native_summary": supported_native_summary,
        "supported_native_rows": supported_native_rows,
        "unsupported_ocvs_rows": unsupported_ocvs_rows,
        "oci_supported_rows": oci_supported_rows,
        "oci_unsupported_rows": oci_unsupported_rows,
        "hybrid_placement_plan": hybrid_placement_plan,
        "workload_summary": workload_summary,
        "scenario_comparison": scenario_comparison,
        "price_comparison": price_comparison,
        "fit_warnings": fit_warnings,
        "executive_summary": executive_summary,
        "ocvs_shape_comparison": build_ocvs_shape_comparison(ocvs_price),
        "vmware_license_summary": vmware_license_summary,
        "scenario_chart_rows": build_scenario_chart_rows(scenario_comparison),
        "cost_breakdown_rows": build_cost_breakdown_rows(
            overall=overall,
            supported_native_summary=supported_native_summary,
            ocvs_price=ocvs_price,
            hybrid_ocvs_price=hybrid_ocvs_price,
            vmware_license_summary=vmware_license_summary,
        ),
        "executive_insights": build_executive_insights(
            scenario_comparison=scenario_comparison,
            executive_summary=executive_summary,
            ocvs_price=ocvs_price,
            hybrid_ocvs_price=hybrid_ocvs_price,
            fit_warnings=fit_warnings,
            vmware_license_summary=vmware_license_summary,
        ),
    }


def build_current_price_page_context() -> tuple[dict[str, Any] | None, str]:
    selected_rvtools_file = str(session.get("selected_rvtools_file", ""))
    customer_name = normalize_customer_name(session.get("customer_name", ""))
    if not selected_rvtools_file:
        flash("Select a VM inventory export in Step 1 to continue.", "rvtools_info")
        return None, "index"

    try:
        all_vms, source_vinfo_csv = load_vms_from_vinfo(selected_rvtools_file)
    except Exception as exc:
        flash(f"Could not load VM inventory data: {exc}", "rvtools_error")
        return None, "index"

    vm_index = {vm["name"]: vm for vm in all_vms}
    app_state = load_app_state()
    selected_vm_names = app_state.get("selected_vm_names", [])
    if not isinstance(selected_vm_names, list):
        selected_vm_names = []
    selected_vm_names = [n for n in selected_vm_names if n in vm_index]
    selected_vms = [vm_index[name] for name in selected_vm_names if name in vm_index]
    if not selected_vms:
        flash("No VMs selected yet. Please select VMs in Step 2 first.", "error")
        return None, "step3"

    shape_options = load_oci_target_shapes()
    shape_pricing_map = load_oci_price_mapping_details()
    if shape_pricing_map:
        shape_options = [s for s in shape_options if s in shape_pricing_map] or list(shape_pricing_map.keys())

    selected_pricelist_file = str(session.get("selected_pricelist_file", "")).strip().replace("\\", "/")
    price_lookup, pricing_currency, source_pricelist_file = load_price_lookup(selected_pricelist_file or None)
    pricing_unit_prices = resolve_pricing_unit_prices(price_lookup)
    block_storage_unit_price = pricing_unit_prices["block_storage_unit_price"]
    block_perf_unit_price = pricing_unit_prices["block_perf_unit_price"]
    windows_os_unit_price = pricing_unit_prices["windows_os_unit_price"]

    valid_shape_values = set(shape_options)
    valid_vpu_values = set(VPU_OPTIONS)
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
    hybrid_placement_selection = app_state.get("step4_hybrid_placements", {})
    if not isinstance(hybrid_placement_selection, dict):
        hybrid_placement_selection = {}
    try:
        iaas_discount_pct = float(app_state.get("step4_iaas_discount_pct", 0.0))
    except (TypeError, ValueError):
        iaas_discount_pct = 0.0
    iaas_discount_pct = max(0.0, min(100.0, iaas_discount_pct))
    ocvs_profile_choice = normalize_ocvs_profile(app_state.get("step4_ocvs_profile", "best_fit"))
    ocvs_policy = normalize_ocvs_policy(app_state.get("step4_ocvs_policy", {}))
    ocvs_commitment_term = normalize_ocvs_commitment_term(app_state.get("step4_ocvs_commitment_term", "payg"))
    vmware_license_price_per_core_yearly = _bounded_float(
        app_state.get("step4_vmware_license_price_per_core_yearly"),
        0.0,
        0.0,
        1_000_000.0,
    )
    ocvs_dr_nodes = normalize_ocvs_dr_nodes(app_state.get("step4_ocvs_dr_nodes", 0))
    hybrid_ocvs_assumptions = effective_hybrid_ocvs_assumptions(
        app_state,
        ocvs_profile_choice=ocvs_profile_choice,
        ocvs_policy=ocvs_policy,
        ocvs_commitment_term=ocvs_commitment_term,
        vmware_license_price_per_core_yearly=vmware_license_price_per_core_yearly,
        ocvs_dr_nodes=ocvs_dr_nodes,
    )
    hybrid_ocvs_customized = bool(hybrid_ocvs_assumptions["customized"])
    hybrid_ocvs_profile_choice = str(hybrid_ocvs_assumptions["profile_choice"])
    hybrid_ocvs_policy = dict(hybrid_ocvs_assumptions["policy"])
    hybrid_ocvs_commitment_term = str(hybrid_ocvs_assumptions["commitment_term"])
    hybrid_vmware_license_price_per_core_yearly = float(
        hybrid_ocvs_assumptions["vmware_license_price_per_core_yearly"]
    )
    hybrid_ocvs_dr_nodes = int(hybrid_ocvs_assumptions["dr_nodes"])
    hybrid_ocvs_assumptions = effective_hybrid_ocvs_assumptions(
        app_state,
        ocvs_profile_choice=ocvs_profile_choice,
        ocvs_policy=ocvs_policy,
        ocvs_commitment_term=ocvs_commitment_term,
        vmware_license_price_per_core_yearly=vmware_license_price_per_core_yearly,
        ocvs_dr_nodes=ocvs_dr_nodes,
    )
    step4_last_updated_at = str(app_state.get("step4_last_updated_at", "") or "")
    snapshot = load_step4_snapshot()
    if (
        not step4_last_updated_at
        and str(snapshot.get("source_vinfo_csv", "")) == source_vinfo_csv
        and snapshot.get("saved_at")
    ):
        step4_last_updated_at = str(snapshot.get("saved_at"))

    vm_rows = build_vm_cost_rows(
        selected_vms,
        shape_options=shape_options,
        shape_pricing_map=shape_pricing_map,
        price_lookup=price_lookup,
        block_storage_unit_price=block_storage_unit_price,
        block_perf_unit_price=block_perf_unit_price,
        windows_os_unit_price=windows_os_unit_price,
        iaas_discount_pct=iaas_discount_pct,
        vm_shape_selection=vm_shape_selection,
        vm_ocpu_selection=vm_ocpu_selection,
        vm_burst_selection=vm_burst_selection,
        vm_vpu_selection=vm_vpu_selection,
        vm_os_license_selection=vm_os_license_selection,
        valid_shape_values=valid_shape_values,
        valid_vpu_values=valid_vpu_values,
    )
    vm_rows.sort(key=lambda r: str(r["vm_name"]).lower())
    analysis = build_price_analysis_from_rows(
        vm_rows=vm_rows,
        price_lookup=price_lookup,
        block_storage_unit_price=block_storage_unit_price,
        block_perf_unit_price=block_perf_unit_price,
        windows_os_unit_price=windows_os_unit_price,
        iaas_discount_pct=iaas_discount_pct,
        ocvs_policy=ocvs_policy,
        ocvs_profile_choice=ocvs_profile_choice,
        source_pricelist_file=source_pricelist_file,
        vmware_license_price_per_core_yearly=vmware_license_price_per_core_yearly,
        ocvs_dr_nodes=ocvs_dr_nodes,
        ocvs_commitment_term=ocvs_commitment_term,
        hybrid_ocvs_policy=hybrid_ocvs_assumptions["policy"],
        hybrid_ocvs_profile_choice=hybrid_ocvs_assumptions["profile_choice"],
        hybrid_vmware_license_price_per_core_yearly=hybrid_ocvs_assumptions[
            "vmware_license_price_per_core_yearly"
        ],
        hybrid_ocvs_dr_nodes=hybrid_ocvs_assumptions["dr_nodes"],
        hybrid_ocvs_commitment_term=hybrid_ocvs_assumptions["commitment_term"],
        hybrid_placement_selection=hybrid_placement_selection,
    )
    migration_waves = build_migration_waves(
        vm_rows=vm_rows,
        supported_native_rows=analysis["supported_native_rows"],
        unsupported_ocvs_rows=analysis["unsupported_ocvs_rows"],
    )

    return {
        "selected_rvtools_file": selected_rvtools_file,
        "source_vinfo_csv": source_vinfo_csv,
        "pricing_currency": pricing_currency,
        "source_pricelist_file": source_pricelist_file,
        "iaas_discount_pct": iaas_discount_pct,
        "vm_rows": vm_rows,
        "ocvs_profile_choice": ocvs_profile_choice,
        "ocvs_policy": ocvs_policy,
        "ocvs_commitment_term": ocvs_commitment_term,
        "ocvs_dr_nodes": ocvs_dr_nodes,
        "vmware_license_price_per_core_yearly": vmware_license_price_per_core_yearly,
        "hybrid_ocvs_assumptions": hybrid_ocvs_assumptions,
        "step4_last_updated_at": step4_last_updated_at,
        "step4_last_updated_display": _format_display_timestamp(step4_last_updated_at),
        "migration_waves": migration_waves,
        "last_export_file": session.get("last_export_file", ""),
        "customer_name": customer_name,
        **analysis,
    }, ""


def build_scenario_configuration_display(
    analysis: dict[str, Any],
    readiness: dict[str, Any],
    ocvs_commitment_term: str,
    hybrid_ocvs_customized: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    readiness_scenarios = readiness.get("scenarios", {})
    if not isinstance(readiness_scenarios, dict):
        readiness_scenarios = {}

    def readiness_display(scenario_id: str) -> dict[str, Any]:
        item = readiness_scenarios.get(scenario_id, {})
        if not isinstance(item, dict):
            item = {}
        state = str(item.get("state") or "incomplete")
        pricing_state = str(item.get("pricing_state") or "incomplete")
        rankable = bool(item.get("rankable", False))
        if pricing_state != "complete":
            status_label = "Pricing incomplete"
            status_tone = "incomplete"
        elif state == "ready":
            status_label = "Ready"
            status_tone = "ready"
        else:
            status_label = "Needs attention"
            status_tone = "attention"
        return {
            "state": state,
            "pricing_state": pricing_state,
            "rankable": rankable,
            "status_label": status_label,
            "status_tone": status_tone,
        }

    ocvs_price = analysis["ocvs_price"]
    hybrid_price = analysis["hybrid_ocvs_price"]
    ocvs_selected = ocvs_price["selected"]
    hybrid_selected = hybrid_price["selected"]
    vmware_summary = analysis["vmware_license_summary"]
    placement_plan = analysis["hybrid_placement_plan"]
    supported_native = analysis["supported_native_summary"]

    ocvs_status = readiness_display("ocvs")
    ocvs_infrastructure_subtotal = float(ocvs_selected.get("total_monthly_cost", 0.0) or 0.0)
    ocvs_vcf_subtotal = float(vmware_summary["ocvs"].get("monthly_cost", 0.0) or 0.0)
    ocvs_vcf_is_priced = bool(vmware_summary["ocvs"].get("is_priced", False))
    ocvs_has_vcf_scope = int(vmware_summary["ocvs"].get("physical_cores", 0) or 0) > 0
    ocvs_vcf_optional = bool(ocvs_has_vcf_scope and not ocvs_vcf_is_priced)
    ocvs_display = {
        **ocvs_status,
        "workload_count": int(analysis.get("overall", {}).get("vm_count", 0) or 0),
        "selected_shape": str(ocvs_selected.get("shape") or ""),
        "selected_label": str(ocvs_selected.get("label") or ""),
        "commitment_term": normalize_ocvs_commitment_term(ocvs_commitment_term),
        "commitment_label": str(ocvs_selected.get("commitment_label") or ""),
        "term_discount_pct": ocvs_term_discount_pct(
            ocvs_selected.get("shape"),
            ocvs_commitment_term,
        ),
        "infrastructure_subtotal": ocvs_infrastructure_subtotal,
        "vcf_subtotal": ocvs_vcf_subtotal,
        "monthly_total": ocvs_infrastructure_subtotal + ocvs_vcf_subtotal,
        "total_label": (
            "Base monthly total"
            if ocvs_vcf_optional
            else "Complete monthly total"
            if ocvs_status["rankable"]
            else "Partial monthly total"
        ),
        "vcf_label": "Optional add-on" if ocvs_vcf_optional else f"{ocvs_vcf_subtotal:,.0f}",
        "vcf_note": (
            "Enter a per-core price only when VMware/Broadcom licensing should be included."
            if ocvs_vcf_optional
            else "Included in the modeled monthly total"
            if ocvs_vcf_is_priced and ocvs_has_vcf_scope
            else "No VCF license scope in this scenario"
        ),
        "vcf_optional": ocvs_vcf_optional,
        "vcf_price_required": False,
    }

    hybrid_status = readiness_display("hybrid")
    hybrid_native_subtotal = float(supported_native.get("total_monthly_cost", 0.0) or 0.0)
    hybrid_ocvs_subtotal = float(hybrid_selected.get("total_monthly_cost", 0.0) or 0.0)
    hybrid_vcf_subtotal = float(vmware_summary["hybrid"].get("monthly_cost", 0.0) or 0.0)
    hybrid_vcf_is_priced = bool(vmware_summary["hybrid"].get("is_priced", False))
    hybrid_has_vcf_scope = int(vmware_summary["hybrid"].get("physical_cores", 0) or 0) > 0
    hybrid_vcf_optional = bool(hybrid_has_vcf_scope and not hybrid_vcf_is_priced)
    hybrid_display = {
        **hybrid_status,
        "workload_count": len(placement_plan.get("rows", [])),
        "native_count": int(placement_plan.get("native_count", 0) or 0),
        "ocvs_count": int(placement_plan.get("ocvs_count", 0) or 0),
        "review_count": int(placement_plan.get("review_count", 0) or 0),
        "ocvs_priced_count": int(placement_plan.get("ocvs_priced_count", 0) or 0),
        "manual_override_count": int(placement_plan.get("manual_override_count", 0) or 0),
        "native_subtotal": hybrid_native_subtotal,
        "ocvs_infrastructure_subtotal": hybrid_ocvs_subtotal,
        "vcf_subtotal": hybrid_vcf_subtotal,
        "monthly_total": hybrid_native_subtotal + hybrid_ocvs_subtotal + hybrid_vcf_subtotal,
        "total_label": (
            "Base monthly total"
            if hybrid_vcf_optional
            else "Complete monthly total"
            if hybrid_status["rankable"]
            else "Partial monthly total"
        ),
        "selected_shape": str(hybrid_selected.get("shape") or ""),
        "commitment_label": str(hybrid_selected.get("commitment_label") or ""),
        "shared_source": (
            f"{hybrid_selected.get('shape', '')} / "
            f"{hybrid_selected.get('commitment_label', OCVS_COMMITMENT_LABELS['payg'])}"
        ),
        "assumptions_label": (
            "Customized for Hybrid"
            if hybrid_ocvs_customized
            else "Inherited from OCVS scenario"
        ),
        "vcf_label": "Optional add-on" if hybrid_vcf_optional else f"{hybrid_vcf_subtotal:,.0f}",
        "vcf_note": (
            "Enter a per-core price only when VMware/Broadcom licensing should be included."
            if hybrid_vcf_optional
            else "Included in the modeled monthly total"
            if hybrid_vcf_is_priced and hybrid_has_vcf_scope
            else "No VCF license scope in this scenario"
        ),
        "vcf_optional": hybrid_vcf_optional,
        "vcf_price_required": False,
    }
    return ocvs_display, hybrid_display


def build_scenario_view(scenario_id: str, context: dict[str, Any]) -> dict[str, Any]:
    scenario_rows = list(context["scenario_comparison"]["rows"])
    scenario = next((row for row in scenario_rows if str(row.get("id", "")) == scenario_id), scenario_rows[0])
    chart_rows = list(context.get("scenario_chart_rows", []))
    chart_scenario = next((row for row in chart_rows if str(row.get("id", "")) == scenario_id), {})
    scenario = {**scenario, **chart_scenario}
    composition_label = {
        "native": "OCI Native",
        "ocvs": "OCVS",
        "hybrid": "Hybrid",
    }.get(scenario_id, "OCI Native")
    composition = next(
        (row for row in context.get("cost_breakdown_rows", []) if row.get("label") == composition_label),
        {"label": composition_label, "total": 0.0, "segments": [], "bar_pct": 0.0},
    )

    def money(value: Any) -> float:
        return float(value or 0.0)

    overall = context["overall"]
    ocvs_selected = context["ocvs_price"]["selected"]
    hybrid_selected = context["hybrid_ocvs_price"]["selected"]
    vmware_summary = context["vmware_license_summary"]
    supported_native = context["supported_native_summary"]

    def ocvs_driver_name(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return {
            "cpu": "CPU",
            "memory": "RAM",
            "storage": "Storage",
            "minimum": "Minimum nodes",
        }.get(normalized, normalized.capitalize() or "Unknown")

    if scenario_id == "native":
        title = "OCI Native"
        intro = "Modernize selected VMs onto OCI Compute and Block Volume using the active sizing, VPU, discount, and licensing assumptions."
        cards = [
            {"label": "Monthly cost", "value": money(scenario.get("monthly_cost")), "kind": "money"},
            {"label": "Annual cost", "value": money(scenario.get("yearly_cost")), "kind": "money"},
            {"label": "Cost / VM / month", "value": money(scenario.get("cost_per_vm")), "kind": "money"},
            {"label": "Modeled VMs", "value": int(overall.get("vm_count", 0) or 0), "kind": "number"},
            {"label": "License-included VMs", "value": int(overall.get("total_license_included_vms", 0) or 0), "kind": "number"},
        ]
        detail_rows = [
            {"label": "Compute + RAM / month", "value": money(overall.get("total_cpu_ram_monthly_cost")), "kind": "money"},
            {"label": "Block Volume / month", "value": money(overall.get("total_storage_monthly_cost")), "kind": "money"},
            {"label": "License-included Windows VMs", "value": f"{int(overall['total_license_included_vms']):,}"},
            {"label": "Windows license / month", "value": money(overall.get("total_os_license_monthly_cost")), "kind": "money"},
            {"label": "Total storage VPUs", "value": f"{int(overall['total_vpus']):,}"},
            {"label": "IaaS discount", "value": f"{float(context['iaas_discount_pct']):.2f}%"},
        ]
        assumptions = []
    elif scenario_id == "ocvs":
        title = "OCVS"
        intro = "Lift and shift selected VMware workloads to OCVS while preserving the VMware operating model and compatibility assumptions."
        vmware_full = vmware_summary["ocvs"]
        cards = [
            {"label": "Monthly cost", "value": money(scenario.get("monthly_cost")), "kind": "money"},
            {"label": "Annual cost", "value": money(scenario.get("yearly_cost")), "kind": "money"},
            {"label": "Cost / VM / month", "value": money(scenario.get("cost_per_vm")), "kind": "money"},
            {"label": "Total OCVS nodes", "value": int(ocvs_selected.get("host_count", 0) or 0), "kind": "number"},
            {"label": "Capacity driver", "value": ocvs_driver_name(ocvs_selected.get("constraint")), "kind": "text"},
        ]
        detail_rows = [
            {"label": "Selected shape", "value": str(ocvs_selected.get("shape", ""))},
            {"label": "Sizing driver", "value": ocvs_driver_name(ocvs_selected.get("constraint"))},
            {"label": "Workload nodes", "value": f"{int(ocvs_selected.get('base_host_count', 0) or 0):,}"},
            {"label": "Spare nodes", "value": f"+{int(ocvs_selected.get('dr_node_count', 0) or 0):,}"},
            {"label": "Total OCVS nodes", "value": f"{int(ocvs_selected.get('host_count', 0) or 0):,}"},
            {
                "label": "Cluster plan",
                "value": (
                    "Multi-cluster"
                    if bool(ocvs_selected.get("cluster_split_required", False))
                    else "Single cluster"
                ),
            },
            {"label": "Physical cores", "value": f"{int(vmware_full.get('physical_cores', 0) or 0):,}"},
            {"label": "VCF license / month", "value": money(vmware_full.get("monthly_cost")), "kind": "money"},
        ]
        assumptions = []
    else:
        title = "Hybrid"
        intro = "Blend OCI Native and OCVS by placing each VM on the target platform that best fits readiness, dependencies, and risk."
        vmware_hybrid = vmware_summary["hybrid"]
        cards = [
            {"label": "Monthly cost", "value": money(scenario.get("monthly_cost")), "kind": "money"},
            {"label": "Annual cost", "value": money(scenario.get("yearly_cost")), "kind": "money"},
            {"label": "Cost / VM / month", "value": money(scenario.get("cost_per_vm")), "kind": "money"},
            {"label": "OCI Native VMs", "value": int(scenario.get("native_vm_count", 0) or 0), "kind": "number"},
            {"label": "OCVS VMs", "value": int(scenario.get("ocvs_vm_count", 0) or 0), "kind": "number"},
        ]
        detail_rows = [
            {"label": "Native VM count", "value": f"{int(scenario.get('native_vm_count', 0) or 0):,}"},
            {"label": "OCVS VM count", "value": f"{int(scenario.get('ocvs_vm_count', 0) or 0):,}"},
            {"label": "Native monthly run-rate", "value": money(supported_native.get("total_monthly_cost")), "kind": "money"},
            {"label": "OCVS subset shape", "value": str(hybrid_selected.get("shape", ""))},
            {"label": "OCVS subset nodes", "value": f"{int(hybrid_selected.get('host_count', 0) or 0):,}"},
            {
                "label": "OCVS cluster plan",
                "value": (
                    "Multi-cluster"
                    if bool(hybrid_selected.get("cluster_split_required", False))
                    else "Single cluster"
                ),
            },
            {"label": "Hybrid VCF cores", "value": f"{int(vmware_hybrid.get('physical_cores', 0) or 0):,}"},
            {"label": "Hybrid VCF / month", "value": money(vmware_hybrid.get("monthly_cost")), "kind": "money"},
        ]
        assumptions = []

    return {
        "id": scenario_id,
        "title": title,
        "intro": intro,
        "scenario": scenario,
        "cards": cards,
        "composition": composition,
        "detail_rows": detail_rows,
        "assumptions": assumptions,
    }


def build_results_page_context(
    readiness: dict[str, Any],
    scenario_views: list[dict[str, Any]],
    app_state: dict[str, Any],
) -> dict[str, Any]:
    """Compose Results display data from readiness and established scenario views."""
    readiness_scenarios = readiness.get("scenarios", {})
    if not isinstance(readiness_scenarios, dict):
        readiness_scenarios = {}
    views_by_id = {
        str(view.get("id") or ""): view
        for view in scenario_views
        if isinstance(view, dict)
    }
    decision_copy = {
        "native": {
            "benefits": [
                "Direct access to OCI-native services and automation.",
                "Reduces dependency on the VMware operating model.",
            ],
            "tradeoffs": [
                "Guest compatibility and target sizing need workload-level validation.",
                "Some applications may require remediation before migration.",
            ],
            "assumptions": [
                "Uses the saved OCI shape, OCPU, burst, VPU, licensing, and discount inputs.",
            ],
        },
        "ocvs": {
            "benefits": [
                "Preserves VMware tools, skills, and operating patterns.",
                "Supports migration with fewer guest-level changes.",
            ],
            "tradeoffs": [
                "Retains VMware platform and licensing dependencies.",
                "Minimum cluster capacity can dominate the modeled run rate.",
            ],
            "assumptions": [
                "Uses the saved node profile, capacity headroom, commitment term, and spare-node inputs.",
            ],
        },
        "hybrid": {
            "benefits": [
                "Balances modernization with continuity for higher-risk workloads.",
                "Supports phased placement decisions across both landing zones.",
            ],
            "tradeoffs": [
                "Requires governance and operations across two target platforms.",
                "Placement dependencies need validation before migration waves are finalized.",
            ],
            "assumptions": [
                "Uses the saved per-VM placement plan and the same Native and OCVS pricing inputs.",
            ],
        },
    }
    scenario_readiness_copy = {
        "ready": ("Ready", "ready"),
        "needs_attention": ("Needs attention", "attention"),
        "incomplete": ("Incomplete", "blocked"),
    }

    def amount(value: Any, fallback: float = 0.0) -> float:
        if isinstance(value, bool):
            return fallback
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return fallback
        return parsed if math.isfinite(parsed) else fallback

    scenarios: list[dict[str, Any]] = []
    lowest_complete = str(readiness.get("lowest_complete_scenario") or "")
    for scenario_id in ("native", "ocvs", "hybrid"):
        view = views_by_id.get(scenario_id, {})
        scenario = view.get("scenario", {}) if isinstance(view, dict) else {}
        if not isinstance(scenario, dict):
            scenario = {}
        status = readiness_scenarios.get(scenario_id, {})
        if not isinstance(status, dict):
            status = {}
        copy_values = decision_copy[scenario_id]
        readiness_state = str(status.get("state") or "incomplete")
        if readiness_state not in scenario_readiness_copy:
            readiness_state = "incomplete"
        readiness_label, readiness_tone = scenario_readiness_copy[readiness_state]

        monthly_cost = amount(scenario.get("monthly_cost"))
        annual_cost = amount(scenario.get("yearly_cost"), monthly_cost * 12)
        if annual_cost == 0.0 and monthly_cost:
            annual_cost = monthly_cost * 12
        pricing_complete = status.get("pricing_state") == "complete"
        technically_eligible = status.get("technical_eligibility") == "eligible"
        native_vm_count = int(amount(scenario.get("native_vm_count")))
        ocvs_vm_count = int(amount(scenario.get("ocvs_vm_count")))
        affected_names = [
            str(name)
            for name in status.get("affected_vm_names", [])
            if str(name).strip()
        ] if isinstance(status.get("affected_vm_names", []), list) else []

        remediation_requirements: list[str] = []
        if affected_names:
            remediation_requirements.append(
                f"Review treatment for {len(affected_names):,} unsupported Native VM(s): "
                + ", ".join(affected_names)
            )
        if not pricing_complete:
            remediation_requirements.append(
                "Complete the missing pricing inputs before customer-ready use."
            )
        if not remediation_requirements:
            remediation_requirements.append(
                "No unresolved remediation requirement is recorded for this path."
            )

        detail_rows = [
            row
            for row in view.get("detail_rows", [])
            if isinstance(row, dict) and str(row.get("label") or "").strip()
        ] if isinstance(view, dict) else []
        scenarios.append(
            {
                "id": scenario_id,
                "title": str(view.get("title") or scenario_id.upper()),
                "intro": (
                    "Blend OCI Native and OCVS by placing each VM according to readiness, dependencies, and risk."
                    if scenario_id == "hybrid"
                    else str(view.get("intro") or "")
                ),
                "technical_label": "Eligible" if technically_eligible else "Ineligible",
                "technical_tone": "ready" if technically_eligible else "blocked",
                "pricing_label": "Complete pricing" if pricing_complete else "Incomplete pricing",
                "pricing_tone": "ready" if pricing_complete else "attention",
                "readiness_state": readiness_state,
                "readiness_label": readiness_label,
                "readiness_tone": readiness_tone,
                "modeled_cost_label": (
                    "Complete modeled amount"
                    if pricing_complete
                    else "Partial modeled amount"
                ),
                "monthly_cost": monthly_cost,
                "annual_cost": annual_cost,
                "three_year_cost": monthly_cost * 36,
                "cost_per_vm": amount(scenario.get("cost_per_vm")),
                "native_vm_count": native_vm_count,
                "ocvs_vm_count": ocvs_vm_count,
                "workload_count": native_vm_count + ocvs_vm_count,
                "rankable": status.get("rankable") is True,
                "is_lowest_complete": scenario_id == lowest_complete,
                "detail_rows": detail_rows,
                "assumptions": list(copy_values["assumptions"]),
                "benefits": list(copy_values["benefits"]),
                "tradeoffs": list(copy_values["tradeoffs"]),
                "remediation_requirements": remediation_requirements,
            }
        )

    rank_tones = {1: "gold", 2: "silver", 3: "bronze"}
    ranked_scenarios = sorted(
        enumerate(scenarios),
        key=lambda item: (item[1]["monthly_cost"], item[0]),
    )
    for rank, (_order, scenario) in enumerate(ranked_scenarios, start=1):
        scenario["price_rank"] = rank
        scenario["rank_tone"] = rank_tones.get(rank, "standard")
        scenario["rank_qualifier"] = "Complete pricing" if scenario["rankable"] else "Partial pricing"

    overall_state = str(readiness.get("overall_state") or "incomplete")
    has_rankable_results = any(scenario["rankable"] for scenario in scenarios)
    has_blocking_items = bool(readiness.get("blocking_items"))
    overall_display_state = (
        "draft_review_required"
        if (
            overall_state == "incomplete"
            and has_rankable_results
            and not has_blocking_items
        )
        else overall_state
    )
    overall_copy = {
        "customer_ready": (
            "Customer-ready export",
            "The selected path and required treatment notes satisfy the current readiness checks.",
        ),
        "draft_review_required": (
            "Draft results available",
            "Scenario modeling is available for assessor review. Complete setup details and record a decision before customer-ready export.",
        ),
        "incomplete": (
            "Needs attention",
            "Complete outstanding setup, inventory, scenario, or pricing items before customer-ready export.",
        ),
    }
    overall_label, overall_detail = overall_copy.get(
        overall_display_state,
        overall_copy["incomplete"],
    )
    recommendation = app_state.get("assessor_recommendation", "")
    if recommendation not in RESULT_RECOMMENDATION_VALUES:
        recommendation = ""
    rationale = app_state.get("assessor_recommendation_rationale", "")
    if not isinstance(rationale, str):
        rationale = ""

    return {
        "overall_state": overall_state,
        "overall_display_state": overall_display_state,
        "overall_label": overall_label,
        "overall_detail": overall_detail,
        "scenarios": scenarios,
        "recommendation": recommendation,
        "rationale": rationale,
        "recommendation_options": [
            {"value": "native", "label": "Native"},
            {"value": "ocvs", "label": "OCVS"},
            {"value": "hybrid", "label": "Hybrid"},
            {"value": "", "label": "Undecided"},
        ],
        "customer_ready_export": readiness.get("customer_ready_export") is True,
        "excel_export_label": "Export Excel",
        "assessment_name": normalize_assessment_name(
            session.get("active_assessment_name", "")
        )
        or "Untitled assessment",
        "assessment_notes": normalize_assessment_notes(
            session.get("active_assessment_notes", "")
        ),
    }


def _xlsx_currency_format_code(currency_code: str) -> str:
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
    code = str(currency_code or "USD").upper()
    return currency_format_map.get(code, f'"{code} "#,##0.00')


def _xlsx_col_ref(col_idx: int) -> str:
    col_idx += 1
    letters = ""
    while col_idx:
        col_idx, rem = divmod(col_idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _xlsx_clean_text(value: Any) -> str:
    cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", str(value))
    return cleaned[:32767]


class _TrustedXlsxFormula:
    __slots__ = ("expression",)

    def __init__(self, expression: str) -> None:
        if not isinstance(expression, str) or not expression:
            raise ValueError("Trusted XLSX formulas require a nonempty expression.")
        self.expression = expression


def _xlsx_formula(expression: str) -> _TrustedXlsxFormula:
    return _TrustedXlsxFormula(expression)


def _xlsx_cell_xml(value: Any, row_idx: int, col_idx: int, style_idx: int | None = None) -> str:
    ref = f"{_xlsx_col_ref(col_idx)}{row_idx}"
    style_attr = f' s="{style_idx}"' if style_idx is not None else ""
    if value is None:
        return f'<c r="{ref}"{style_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t>{str(value)}</t></is></c>'
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            value = 0
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    if isinstance(value, _TrustedXlsxFormula):
        formula = xml_escape(_xlsx_clean_text(value.expression))
        return f'<c r="{ref}"{style_attr}><f>{formula}</f></c>'

    text = _xlsx_clean_text(value)
    safe = xml_escape(text)
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t>{safe}</t></is></c>'


def _xlsx_styles_xml(currency_fmt_code: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="4">'
        f'<numFmt numFmtId="164" formatCode="{xml_escape(currency_fmt_code)}"/>'
        '<numFmt numFmtId="165" formatCode="0.0%"/>'
        '<numFmt numFmtId="166" formatCode="#,##0"/>'
        '<numFmt numFmtId="167" formatCode="0.000000"/>'
        '</numFmts>'
        '<fonts count="9">'
        '<font><sz val="11"/><color rgb="FF121417"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="11"/><color rgb="FF121417"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="14"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="11"/><color rgb="FF0F62FE"/><name val="Calibri"/><family val="2"/></font>'
        '<font><i/><sz val="10"/><color rgb="FF525C6A"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="12"/><color rgb="FF0F62FE"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="12"/><color rgb="FF00796B"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="12"/><color rgb="FF6D28D9"/><name val="Calibri"/><family val="2"/></font>'
        '</fonts>'
        '<fills count="12">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF0F62FE"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEFF6FF"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF2F4F8"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF8E6"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEFF6FF"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFE7FFF8"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF6F0FF"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF2563EB"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF009688"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF8B5CF6"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="2">'
        '<border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border>'
        '<left style="thin"><color rgb="FFD9DDE3"/></left>'
        '<right style="thin"><color rgb="FFD9DDE3"/></right>'
        '<top style="thin"><color rgb="FFD9DDE3"/></top>'
        '<bottom style="thin"><color rgb="FFD9DDE3"/></bottom>'
        '<diagonal/>'
        '</border>'
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="20">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
        '<xf numFmtId="166" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="4" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="167" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="6" borderId="1" xfId="0" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="7" borderId="1" xfId="0" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="8" borderId="1" xfId="0" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="5" fillId="9" borderId="1" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="5" fillId="10" borderId="1" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="5" fillId="11" borderId="1" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="6" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="7" fillId="7" borderId="1" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="8" fillId="8" borderId="1" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )


def _xlsx_row_height(row: list[Any], row_style: int | None) -> float:
    if not any(str(value or "").strip() for value in row):
        return 6.0
    if row_style == 3:
        return 20.0
    if row_style in {4, 5, 8}:
        return 18.0
    if row_style in {9, 11, 12, 13}:
        max_text_len = max((len(str(value or "")) for value in row), default=0)
        if max_text_len >= 180:
            return 78.0
        if max_text_len >= 100:
            return 56.0
        return 36.0
    return 18.0


def _xlsx_sheet_xml_for_rows(
    rows: list[list[Any]],
    *,
    row_styles: dict[int, int] | None = None,
    cell_styles: dict[tuple[int, int], int] | None = None,
    column_widths: list[float] | None = None,
    freeze_row: int | None = None,
) -> str:
    row_styles = row_styles or {}
    cell_styles = cell_styles or {}
    max_cols = max((len(row) for row in rows), default=1)
    max_rows = max(len(rows), 1)
    dimension = f"A1:{_xlsx_col_ref(max_cols - 1)}{max_rows}"
    cols_xml = ""
    if column_widths:
        col_defs = []
        for idx, width in enumerate(column_widths, start=1):
            col_defs.append(f'<col min="{idx}" max="{idx}" width="{width:.2f}" customWidth="1"/>')
        cols_xml = f"<cols>{''.join(col_defs)}</cols>"

    sheet_views_xml = '<sheetViews><sheetView workbookViewId="0" showGridLines="0"/></sheetViews>'
    if freeze_row and freeze_row > 0:
        top_left = f"A{freeze_row + 1}"
        sheet_views_xml = (
            '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
            f'<pane ySplit="{freeze_row}" topLeftCell="{top_left}" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
        )

    sheet_rows_xml: list[str] = []
    merge_refs: list[str] = []
    for row_idx, row in enumerate(rows, start=1):
        row_cells: list[str] = []
        has_content = any(str(value or "").strip() for value in row)
        row_style = row_styles.get(row_idx)
        row_height = _xlsx_row_height(row, row_style)
        if row_style in {3, 4, 8} and max_cols > 1:
            merge_refs.append(f"A{row_idx}:{_xlsx_col_ref(max_cols - 1)}{row_idx}")
        for col_idx in range(max_cols):
            value = row[col_idx] if col_idx < len(row) else None
            style_idx = cell_styles.get((row_idx, col_idx + 1), row_style)
            if style_idx is None and has_content:
                style_idx = 7
            row_cells.append(_xlsx_cell_xml(value, row_idx, col_idx, style_idx=style_idx))
        sheet_rows_xml.append(
            f'<row r="{row_idx}" ht="{row_height:.1f}" customHeight="1">{"".join(row_cells)}</row>'
        )

    merges_xml = ""
    if merge_refs:
        merge_cells = "".join(f'<mergeCell ref="{ref}"/>' for ref in merge_refs)
        merges_xml = f'<mergeCells count="{len(merge_refs)}">{merge_cells}</mergeCells>'

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/>'
        f"{sheet_views_xml}"
        '<sheetFormatPr defaultRowHeight="18"/>'
        f"{cols_xml}"
        f"<sheetData>{''.join(sheet_rows_xml)}</sheetData>"
        f"{merges_xml}"
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        '</worksheet>'
    )


def _build_xlsx_workbook_bytes(
    sheets: list[dict[str, Any]],
    *,
    currency_fmt_code: str,
) -> bytes:
    workbook_sheet_xml = []
    workbook_rels_xml = []
    content_type_overrides = []
    for idx, sheet in enumerate(sheets, start=1):
        name = xml_escape(str(sheet["name"])[:31])
        workbook_sheet_xml.append(f'<sheet name="{name}" sheetId="{idx}" r:id="rId{idx}"/>')
        workbook_rels_xml.append(
            f'<Relationship Id="rId{idx}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
        )
        content_type_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    styles_rel_id = f"rId{len(sheets) + 1}"
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>'
        f"{''.join(workbook_sheet_xml)}"
        '</sheets>'
        '</workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{''.join(workbook_rels_xml)}"
        f'<Relationship Id="{styles_rel_id}" '
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
        f"{''.join(content_type_overrides)}"
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", root_rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", _xlsx_styles_xml(currency_fmt_code))
        for idx, sheet in enumerate(sheets, start=1):
            sheet_xml = _xlsx_sheet_xml_for_rows(
                sheet["rows"],
                row_styles=sheet.get("row_styles"),
                cell_styles=sheet.get("cell_styles"),
                column_widths=sheet.get("column_widths"),
                freeze_row=sheet.get("freeze_row"),
            )
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", sheet_xml)
    return buffer.getvalue()


def _workbook_readiness_metadata(
    readiness: Any,
    assessor_recommendation: Any = "",
    recommendation_rationale: Any = "",
) -> dict[str, Any]:
    max_issue_count = 1000
    max_affected_names = 1000
    max_text_length = 4000
    readiness_labels = {
        "draft_review_required": "Draft results available",
        "incomplete": "Draft results available",
        "customer_ready": "Customer ready",
    }
    recommendation_labels = {
        "native": "OCI Native",
        "ocvs": "OCVS",
        "hybrid": "Hybrid",
    }
    malformed = not isinstance(readiness, dict)
    source = readiness if isinstance(readiness, dict) else {}

    def bounded_text(value: Any, *, required: bool = False) -> str:
        nonlocal malformed
        if not isinstance(value, str):
            malformed = True
            return ""
        text = value.strip()
        if len(value) > max_text_length or len(text) > max_text_length:
            text = text[:max_text_length]
            malformed = True
        if required and not text:
            malformed = True
        return text

    def normalized_names(value: Any) -> list[str]:
        nonlocal malformed
        if not isinstance(value, list):
            malformed = True
            return []
        if len(value) > max_affected_names:
            malformed = True
        normalized: list[str] = []
        seen: set[str] = set()
        for name in value[:max_affected_names]:
            clean_name = bounded_text(name, required=True)
            if not clean_name:
                continue
            if clean_name in seen:
                malformed = True
                continue
            seen.add(clean_name)
            normalized.append(clean_name)
        return normalized

    def bounded_names_display(names: list[str]) -> str:
        nonlocal malformed
        display = ", ".join(names)
        if len(display) > max_text_length:
            display = display[:max_text_length]
            malformed = True
        return display or "None"

    overall_state = source.get("overall_state")
    if not isinstance(overall_state, str) or overall_state not in readiness_labels:
        overall_state = "incomplete"
        malformed = True

    customer_ready_export = source.get("customer_ready_export")
    if not isinstance(customer_ready_export, bool):
        customer_ready_export = False
        malformed = True
    if (overall_state == "customer_ready") != (customer_ready_export is True):
        malformed = True

    recommendation = bounded_text(assessor_recommendation)
    if recommendation not in {"", *recommendation_labels}:
        recommendation = ""
        malformed = True
    rationale = bounded_text(recommendation_rationale)

    scenario_values = source.get("scenarios")
    if not isinstance(scenario_values, dict):
        scenario_values = {}
        malformed = True
    scenarios: dict[str, dict[str, Any]] = {}
    for scenario_id in ("native", "ocvs", "hybrid"):
        scenario = scenario_values.get(scenario_id)
        if not isinstance(scenario, dict):
            scenario = {}
            malformed = True

        technical_eligibility = scenario.get("technical_eligibility")
        if (
            not isinstance(technical_eligibility, str)
            or technical_eligibility not in {"eligible", "ineligible"}
        ):
            technical_eligibility = "ineligible"
            malformed = True
        pricing_state = scenario.get("pricing_state")
        if (
            not isinstance(pricing_state, str)
            or pricing_state not in {"complete", "incomplete"}
        ):
            pricing_state = "incomplete"
            malformed = True
        rankable = scenario.get("rankable")
        if not isinstance(rankable, bool):
            rankable = False
            malformed = True
        scenario_customer_ready = scenario.get("customer_ready")
        if not isinstance(scenario_customer_ready, bool):
            scenario_customer_ready = False
            malformed = True
        remediation_required = scenario.get("remediation_required")
        if not isinstance(remediation_required, bool):
            remediation_required = False
            malformed = True

        affected_vm_names = normalized_names(scenario.get("affected_vm_names"))
        if rankable != (
            technical_eligibility == "eligible" and pricing_state == "complete"
        ):
            malformed = True
        if scenario_customer_ready and not rankable:
            malformed = True
        if scenario_id == "native":
            if remediation_required != bool(affected_vm_names):
                malformed = True
            remediation_required = remediation_required or bool(affected_vm_names)
        elif remediation_required or affected_vm_names:
            malformed = True
            remediation_required = False
            affected_vm_names = []
        scenarios[scenario_id] = {
            "technical_eligibility": technical_eligibility,
            "pricing_state": pricing_state,
            "rankable": rankable,
            "customer_ready": scenario_customer_ready,
            "remediation_required": remediation_required,
            "affected_vm_names": affected_vm_names,
        }

    if recommendation and not isinstance(scenario_values.get(recommendation), dict):
        recommendation = ""
        malformed = True

    lowest_complete_scenario = source.get("lowest_complete_scenario")
    if (
        not isinstance(lowest_complete_scenario, str)
        or lowest_complete_scenario not in {"", *recommendation_labels}
    ):
        lowest_complete_scenario = ""
        malformed = True
    elif lowest_complete_scenario:
        lowest_scenario = scenarios[lowest_complete_scenario]
        if not (
            lowest_scenario["technical_eligibility"] == "eligible"
            and lowest_scenario["pricing_state"] == "complete"
            and lowest_scenario["rankable"] is True
        ):
            lowest_complete_scenario = ""
            malformed = True

    seen_issue_ids: set[str] = set()
    issue_input_count = 0

    def normalize_issues(key: str) -> list[dict[str, str]]:
        nonlocal issue_input_count, malformed
        values = source.get(key)
        if not isinstance(values, list):
            malformed = True
            return []
        remaining = max_issue_count - issue_input_count
        if len(values) > remaining:
            malformed = True
        normalized: list[dict[str, str]] = []
        for value in values[:remaining]:
            issue_input_count += 1
            if not isinstance(value, dict):
                malformed = True
                continue
            issue_id = bounded_text(value.get("id"), required=True)
            if not issue_id:
                continue
            if issue_id in seen_issue_ids:
                malformed = True
                continue
            seen_issue_ids.add(issue_id)
            title = bounded_text(value.get("title"))
            detail = bounded_text(value.get("detail"))
            title = title or bounded_text(issue_id.replace("-", " ").title())
            title = title or "Readiness item"
            detail = detail or "No additional detail provided."
            names = normalized_names(value.get("affected_vm_names"))
            normalized.append(
                {
                    "id": issue_id,
                    "title": title,
                    "detail": detail,
                    "affected_vms": bounded_names_display(names),
                }
            )
        return normalized

    blockers = normalize_issues("blocking_items")
    advisories = normalize_issues("advisory_items")
    native = scenarios["native"]
    native_affected_vm_names = bounded_names_display(native["affected_vm_names"])
    if (
        native["customer_ready"] is True
        and native["remediation_required"] is True
        and (recommendation != "native" or not rationale)
    ):
        malformed = True
    selected_scenario = scenarios.get(recommendation, {})
    is_customer_ready = bool(
        not malformed
        and overall_state == "customer_ready"
        and customer_ready_export is True
        and recommendation in recommendation_labels
        and selected_scenario.get("technical_eligibility") == "eligible"
        and selected_scenario.get("pricing_state") == "complete"
        and selected_scenario.get("rankable") is True
        and selected_scenario.get("customer_ready") is True
        and not blockers
        and not (
            recommendation == "native"
            and selected_scenario.get("remediation_required") is True
            and not rationale
        )
    )
    if overall_state == "customer_ready" and not is_customer_ready:
        malformed = True
    if (
        recommendation in recommendation_labels
        and selected_scenario.get("customer_ready") is True
        and overall_state != "customer_ready"
    ):
        malformed = True

    workbook_status = "Customer ready" if is_customer_ready else "Draft"
    readiness_label = (
        "Incomplete"
        if malformed
        else readiness_labels.get(overall_state, "Incomplete")
    )
    return {
        "workbook_status": workbook_status,
        "readiness_label": readiness_label,
        "recommendation": recommendation_labels.get(
            recommendation, "Undecided"
        ),
        "recommendation_rationale": rationale or "Not provided",
        "lowest_complete_scenario_id": lowest_complete_scenario,
        "lowest_complete_scenario": recommendation_labels.get(
            lowest_complete_scenario, "No complete modeled price"
        ),
        "native_remediation_status": (
            "Required" if native["remediation_required"] else "Not required"
        ),
        "native_affected_vm_count": len(native["affected_vm_names"]),
        "native_affected_vm_names": native_affected_vm_names,
        "ocvs_pricing_completeness": (
            "Complete" if scenarios["ocvs"]["pricing_state"] == "complete" else "Incomplete"
        ),
        "hybrid_pricing_completeness": (
            "Complete" if scenarios["hybrid"]["pricing_state"] == "complete" else "Incomplete"
        ),
        "blockers": blockers,
        "advisories": advisories,
        "customer_ready_export": is_customer_ready,
    }


def build_migration_price_workbook_xlsx(
    *,
    readiness: dict[str, Any],
    assessor_recommendation: str = "",
    recommendation_rationale: str = "",
    customer_name: str,
    pricing_currency: str,
    source_pricelist_file: str,
    source_vinfo_csv: str,
    export_path_display: str,
    generated_at: str,
    step4_last_updated_at: str,
    vm_rows: list[dict[str, Any]],
    non_selected_vm_rows: list[dict[str, Any]],
    analysis: dict[str, Any],
    migration_waves: dict[str, Any],
    shape_price_rates: dict[str, dict[str, float]],
    iaas_discount_pct: float,
    ocvs_profile_choice: str,
    ocvs_policy: dict[str, Any],
    ocvs_commitment_term: str,
    ocvs_dr_nodes: int,
    vmware_license_price_per_core_yearly: float,
    block_storage_unit_price: float,
    block_perf_unit_price: float,
    windows_os_unit_price: float,
) -> bytes:
    STYLE_CURRENCY = 1
    STYLE_PERCENT = 2
    STYLE_TITLE = 3
    STYLE_SECTION = 4
    STYLE_HEADER = 5
    STYLE_INTEGER = 6
    STYLE_NOTE = 8
    STYLE_CENTER = 9
    STYLE_UNIT_PRICE = 10
    STYLE_PATH_NATIVE = 11
    STYLE_PATH_OCVS = 12
    STYLE_PATH_HYBRID = 13
    STYLE_PATH_NATIVE_LABEL = 14
    STYLE_PATH_OCVS_LABEL = 15
    STYLE_PATH_HYBRID_LABEL = 16
    STYLE_PATH_NATIVE_TITLE = 17
    STYLE_PATH_OCVS_TITLE = 18
    STYLE_PATH_HYBRID_TITLE = 19

    workload_summary = analysis["workload_summary"]
    scenario_comparison = analysis["scenario_comparison"]
    scenario_chart_rows = analysis["scenario_chart_rows"]
    cost_breakdown_rows = analysis["cost_breakdown_rows"]
    fit_warnings = analysis["fit_warnings"]
    executive_summary = analysis["executive_summary"]
    overall = analysis["overall"]
    supported_native_summary = analysis["supported_native_summary"]
    supported_native_rows = analysis["supported_native_rows"]
    unsupported_ocvs_rows = analysis["unsupported_ocvs_rows"]
    ocvs_price = analysis["ocvs_price"]
    hybrid_ocvs_price = analysis["hybrid_ocvs_price"]
    ocvs_shape_comparison = analysis["ocvs_shape_comparison"]
    vmware_license_summary = analysis["vmware_license_summary"]
    hybrid_placement_plan = analysis.get("hybrid_placement_plan", {})
    hybrid_placement_rows = list(hybrid_placement_plan.get("rows", []))
    ocvs_commitment_term = normalize_ocvs_commitment_term(ocvs_commitment_term)
    ocvs_commitment_label = OCVS_COMMITMENT_LABELS.get(ocvs_commitment_term, OCVS_COMMITMENT_LABELS["payg"])
    ocvs_commitment_discount_pct = float(ocvs_price.get("selected", {}).get("commitment_discount_pct", 0.0) or 0.0)
    readiness_metadata = _workbook_readiness_metadata(
        readiness,
        assessor_recommendation,
        recommendation_rationale,
    )

    def money(value: Any) -> float:
        return float(value or 0.0)

    def integer(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def scenario_by_id(scenario_id: str) -> dict[str, Any]:
        rows = list(scenario_comparison.get("rows", []))
        return next((row for row in rows if str(row.get("id", "")) == scenario_id), {})

    def chart_by_id(scenario_id: str) -> dict[str, Any]:
        return next((row for row in scenario_chart_rows if str(row.get("id", "")) == scenario_id), {})

    def vm_monthly(row: dict[str, Any]) -> float:
        return (
            money(row.get("cpu_ram_monthly_cost"))
            + money(row.get("storage_monthly_cost"))
            + money(row.get("os_license_monthly_cost"))
        )

    def burst_factor_for_export(row: dict[str, Any]) -> float:
        return float(BURST_FACTOR_MAP.get(normalize_burst_value(row.get("burst", "100%")), 1.0))

    def vm_costing_detail_rows(rows_to_export: list[dict[str, Any]]) -> list[list[Any]]:
        detail_rows: list[list[Any]] = []
        for row in rows_to_export:
            detail_rows.append(
                [
                    row.get("vm_name", ""),
                    row.get("os_name", ""),
                    row.get("os_license", ""),
                    integer(row.get("cpus")),
                    integer(row.get("ocpu")),
                    burst_factor_for_export(row),
                    integer(row.get("memory_mb")),
                    integer(row.get("provisioned_gb")),
                    integer(row.get("vpu")),
                    row.get("oci_shape", ""),
                    money(row.get("cpu_monthly_cost")),
                    money(row.get("ram_monthly_cost")),
                    money(row.get("cpu_ram_monthly_cost")),
                    money(row.get("storage_capacity_monthly_cost")),
                    money(row.get("storage_performance_monthly_cost")),
                    money(row.get("storage_monthly_cost")),
                    money(row.get("os_license_monthly_cost")),
                    vm_monthly(row),
                ]
            )
        return detail_rows

    def price_list_rows() -> list[list[Any]]:
        shape_rows = [
            [
                shape_name,
                money(rate.get("ocpu_unit_price")),
                money(rate.get("memory_unit_price")),
                pricing_currency or "USD",
            ]
            for shape_name, rate in shape_price_rates.items()
        ]
        parameter_rows = [
            ["Block Storage Unit Price", money(block_storage_unit_price), pricing_currency or "USD"],
            ["Block Performance Unit Price", money(block_perf_unit_price), pricing_currency or "USD"],
            ["Windows OS Unit Price", money(windows_os_unit_price), pricing_currency or "USD"],
            ["IaaS Discount Factor", max(0.0, min(1.0, 1.0 - (float(iaas_discount_pct or 0.0) / 100.0))), ""],
            ["IaaS Discount %", float(iaas_discount_pct or 0.0) / 100.0, ""],
            ["OCVS Commitment Term", ocvs_commitment_label, ""],
            ["OCVS Commitment Discount %", ocvs_commitment_discount_pct / 100.0, ""],
            ["VCF List Price / Core / Year", money(vmware_license_price_per_core_yearly), pricing_currency or "USD"],
        ]
        row_count = max(len(shape_rows), len(parameter_rows))
        combined: list[list[Any]] = []
        for idx in range(row_count):
            shape_part = shape_rows[idx] if idx < len(shape_rows) else ["", "", "", ""]
            parameter_part = parameter_rows[idx] if idx < len(parameter_rows) else ["", "", ""]
            combined.append(shape_part + parameter_part)
        return combined

    def new_sheet() -> tuple[list[list[Any]], dict[int, int], dict[tuple[int, int], int]]:
        return [], {}, {}

    def add_row(
        rows: list[list[Any]],
        row_styles: dict[int, int],
        row: list[Any],
        *,
        style: int | None = None,
    ) -> int:
        rows.append(row)
        row_idx = len(rows)
        if style is not None:
            row_styles[row_idx] = style
        return row_idx

    def add_title(rows: list[list[Any]], row_styles: dict[int, int], title: str) -> None:
        add_row(rows, row_styles, [title], style=STYLE_TITLE)

    def add_section(rows: list[list[Any]], row_styles: dict[int, int], title: str) -> None:
        add_row(rows, row_styles, [title], style=STYLE_SECTION)

    def add_note(rows: list[list[Any]], row_styles: dict[int, int], text: str) -> None:
        add_row(rows, row_styles, [text], style=STYLE_NOTE)
        add_row(rows, row_styles, [])

    def add_table(
        rows: list[list[Any]],
        row_styles: dict[int, int],
        cell_styles: dict[tuple[int, int], int],
        headers: list[str],
        data_rows: list[list[Any]],
        *,
        currency_cols: set[int] | None = None,
        percent_cols: set[int] | None = None,
        integer_cols: set[int] | None = None,
    ) -> int:
        currency_cols = currency_cols or set()
        percent_cols = percent_cols or set()
        integer_cols = integer_cols or set()
        add_row(rows, row_styles, headers, style=STYLE_HEADER)
        first_data_row = len(rows) + 1
        for data_row in data_rows:
            row_idx = add_row(rows, row_styles, data_row)
            for col_idx in currency_cols:
                cell_styles[(row_idx, col_idx)] = STYLE_CURRENCY
            for col_idx in percent_cols:
                cell_styles[(row_idx, col_idx)] = STYLE_PERCENT
            for col_idx in integer_cols:
                cell_styles[(row_idx, col_idx)] = STYLE_INTEGER
        add_row(rows, row_styles, [])
        return first_data_row

    def add_key_values(
        rows: list[list[Any]],
        row_styles: dict[int, int],
        cell_styles: dict[tuple[int, int], int],
        data_rows: list[list[Any]],
        *,
        currency_rows: set[int] | None = None,
        percent_rows: set[int] | None = None,
        integer_rows: set[int] | None = None,
    ) -> None:
        currency_rows = currency_rows or set()
        percent_rows = percent_rows or set()
        integer_rows = integer_rows or set()
        first_data_row = add_table(
            rows,
            row_styles,
            cell_styles,
            ["Metric", "Value", "Notes"],
            data_rows,
            currency_cols=set(),
        )
        for offset in currency_rows:
            cell_styles[(first_data_row + offset - 1, 2)] = STYLE_CURRENCY
        for offset in percent_rows:
            cell_styles[(first_data_row + offset - 1, 2)] = STYLE_PERCENT
        for offset in integer_rows:
            cell_styles[(first_data_row + offset - 1, 2)] = STYLE_INTEGER
        styled_value_rows = currency_rows | percent_rows | integer_rows
        for offset in range(1, len(data_rows) + 1):
            if offset not in styled_value_rows:
                cell_styles[(first_data_row + offset - 1, 2)] = STYLE_CENTER

    def add_migration_path_cards(
        rows: list[list[Any]],
        row_styles: dict[int, int],
        cell_styles: dict[tuple[int, int], int],
    ) -> None:
        specs = migration_path_option_specs()

        def add_card_row(values: list[Any], styles: list[int], *, height_style: int = STYLE_CENTER) -> int:
            row_idx = add_row(rows, row_styles, values, style=height_style)
            for col_idx, style in enumerate(styles, start=1):
                cell_styles[(row_idx, col_idx)] = style
            return row_idx

        label_styles = [int(spec["label_style"]) for spec in specs]
        title_styles = [int(spec["title_style"]) for spec in specs]
        card_styles = [int(spec["card_style"]) for spec in specs]

        add_card_row([spec["label"] for spec in specs], label_styles)
        add_card_row([spec["title"] for spec in specs], title_styles)
        add_card_row([spec["description"] for spec in specs], card_styles)
        add_card_row(["Best suited for", "Best suited for", "Best suited for"], title_styles)
        add_card_row([bullet_text(spec["best_suited_for"]) for spec in specs], card_styles)
        add_card_row(["Benefits", "Benefits", "Benefits"], title_styles)
        add_card_row([bullet_text(spec["benefits"]) for spec in specs], card_styles)
        add_card_row(["Migration tool options", "Migration tool options", "Migration tool options"], title_styles)
        add_card_row([bullet_text(spec["tools"]) for spec in specs], card_styles)
        add_row(rows, row_styles, [])

    def scenario_cost_rows() -> list[list[Any]]:
        native_monthly = money(scenario_by_id("native").get("monthly_cost"))

        def row_for(scenario_id: str, role: str) -> list[Any]:
            scenario = scenario_by_id(scenario_id)
            monthly = money(scenario.get("monthly_cost"))
            return [
                scenario.get("label", scenario_id),
                monthly,
                monthly * 12.0,
                monthly * 36.0,
                monthly / total_vm_count if total_vm_count else 0.0,
                monthly - native_monthly,
                role,
            ]

        return [
            row_for("native", "Modernization baseline"),
            row_for("ocvs", "VMware lift and shift"),
            row_for("hybrid", "Balanced placement"),
        ]

    def migration_path_option_specs() -> list[dict[str, Any]]:
        return [
            {
                "label": "Modernize and Optimize",
                "title": "OCI Native",
                "description": (
                    "Migrate suitable VMware workloads to OCI Compute and Block Volume, reducing dependency on "
                    "virtualization platforms while building a foundation for long-term cloud optimization and "
                    "application modernization."
                ),
                "best_suited_for": [
                    "Workloads fully supported on OCI.",
                    "Organizations pursuing cloud transformation and platform modernization.",
                    "Environments seeking to reduce VMware licensing and operational overhead.",
                ],
                "benefits": [
                    "Maximum cloud adoption and modernization potential.",
                    "Reduced infrastructure complexity.",
                    "Access to OCI-native services, automation, and cost optimization.",
                ],
                "tools": [
                    "OCI Cloud Migrations for discovery, replication, and migration planning.",
                    "OCI Database Migration and Zero Downtime Migration for database workloads.",
                    "Terraform and OCI Resource Manager for repeatable target deployment.",
                ],
                "card_style": STYLE_PATH_NATIVE,
                "label_style": STYLE_PATH_NATIVE_LABEL,
                "title_style": STYLE_PATH_NATIVE_TITLE,
            },
            {
                "label": "Lift & Shift",
                "title": "Oracle Cloud VMware Solution (OCVS)",
                "description": (
                    "Lift and shift VMware workloads to OCVS, moving them to OCI while reusing existing VMware "
                    "investments, tools, skills, and operating processes with minimal business disruption."
                ),
                "best_suited_for": [
                    "Business-critical applications requiring VMware compatibility.",
                    "Complex dependencies, legacy operating systems, or VMware-specific tooling.",
                    "Organizations prioritizing migration speed and operational continuity.",
                ],
                "benefits": [
                    "Minimal application changes.",
                    "Retain VMware skills, processes, and tooling.",
                    "Reduced migration complexity and accelerated cloud adoption.",
                ],
                "tools": [
                    "VMware HCX for large-scale migration and workload mobility.",
                    "Existing VMware backup and replication tools for recovery-based migration.",
                ],
                "card_style": STYLE_PATH_OCVS,
                "label_style": STYLE_PATH_OCVS_LABEL,
                "title_style": STYLE_PATH_OCVS_TITLE,
            },
            {
                "label": "Balance Modernization and Risk",
                "title": "Hybrid",
                "description": (
                    "Adopt a phased strategy by moving OCI-compatible workloads to OCI Native while retaining complex, "
                    "unsupported, or higher-risk workloads on OCVS to balance modernization with stability."
                ),
                "best_suited_for": [
                    "Large and diverse application estates.",
                    "Organizations seeking gradual VMware reduction.",
                    "Customers requiring phased migration waves and dependency validation.",
                ],
                "benefits": [
                    "Balanced risk and modernization approach.",
                    "Incremental cloud transformation with reduced remediation effort.",
                    "Flexibility to modernize workloads over time.",
                ],
                "tools": [
                    "OCI Cloud Migrations for OCI Native candidates.",
                    "VMware HCX for OCVS migration waves.",
                ],
                "card_style": STYLE_PATH_HYBRID,
                "label_style": STYLE_PATH_HYBRID_LABEL,
                "title_style": STYLE_PATH_HYBRID_TITLE,
            },
        ]

    def bullet_text(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items)

    def count_shape_distribution(rows_to_count: list[dict[str, Any]]) -> list[list[Any]]:
        counts: dict[str, int] = {}
        for row in rows_to_count:
            shape = str(row.get("oci_shape", "") or "Unassigned")
            counts[shape] = counts.get(shape, 0) + 1
        return [[shape, count] for shape, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]

    def os_family(row: dict[str, Any]) -> str:
        os_value = str(row.get("os_name", "") or "").lower()
        if "windows" in os_value:
            return "Windows"
        linux_terms = ("linux", "ubuntu", "red hat", "rhel", "oracle linux", "centos", "debian", "suse", "rocky", "alma")
        if any(term in os_value for term in linux_terms):
            return "Linux"
        return "Other / Unknown"

    def count_os_family(rows_to_count: list[dict[str, Any]], family: str) -> int:
        return sum(1 for row in rows_to_count if os_family(row) == family)

    def is_legacy_os(row: dict[str, Any]) -> bool:
        os_value = str(row.get("os_name", "") or "").lower()
        legacy_terms = (
            "windows server 2003",
            "windows server 2008",
            "windows server 2012",
            "windows xp",
            "windows 7",
            "centos 6",
            "red hat enterprise linux 6",
            "rhel 6",
            "oracle linux 6",
            "suse linux enterprise server 11",
        )
        return any(term in os_value for term in legacy_terms)

    def placement_reason_rows() -> list[list[Any]]:
        supported_count = sum(1 for row in hybrid_placement_rows if bool(row.get("hybrid_is_oci_supported")))
        unsupported_count = sum(
            1
            for row in hybrid_placement_rows
            if not bool(row.get("hybrid_is_oci_supported")) and bool(hybrid_placement_plan.get("support_source_available"))
        )
        legacy_count = sum(1 for row in hybrid_placement_rows if is_legacy_os(row))
        manual_count = integer(hybrid_placement_plan.get("manual_override_count"))
        return [
            ["Supported OS", supported_count],
            ["Unsupported OS", unsupported_count],
            ["Legacy OS", legacy_count],
            ["Manual Placement", manual_count],
        ]

    def percent(part: int, total: int) -> float:
        return (float(part) / float(total)) if total else 0.0

    total_vm_count = integer(workload_summary.get("vm_count"))
    hybrid_native_count = integer(hybrid_placement_plan.get("native_count"))
    hybrid_ocvs_priced_count = integer(hybrid_placement_plan.get("ocvs_priced_count"))
    hybrid_review_count = integer(hybrid_placement_plan.get("review_count"))
    scenario_costs = scenario_cost_rows()
    monthly_values = [row[1] for row in scenario_costs]
    lowest_monthly = min(monthly_values, default=0.0)
    highest_monthly = max(monthly_values, default=0.0)
    lowest_complete_scenario = scenario_by_id(
        str(readiness_metadata["lowest_complete_scenario_id"])
    )
    lowest_complete_monthly: Any = (
        money(lowest_complete_scenario.get("monthly_cost"))
        if lowest_complete_scenario
        else "Not available"
    )

    ocvs_selected = ocvs_price["selected"]
    ocvs_totals = ocvs_price["totals"]
    hybrid_selected = hybrid_ocvs_price["selected"]
    hybrid_totals = hybrid_ocvs_price["totals"]
    vmware_full = vmware_license_summary["ocvs"]
    vmware_hybrid = vmware_license_summary["hybrid"]

    # Executive Summary
    rows, row_styles, cell_styles = new_sheet()
    add_title(
        rows,
        row_styles,
        f"Executive Summary - {readiness_metadata['workbook_status']}",
    )
    add_section(rows, row_styles, "Assessment Context")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["Customer", customer_name or "Not provided", ""],
            ["Currency", pricing_currency or "USD", ""],
            ["Selected VM Count", total_vm_count, ""],
            ["Selected vCPU", integer(workload_summary.get("total_vcpus")), ""],
            ["Selected RAM GB", integer(workload_summary.get("total_memory_gb")), ""],
            ["Selected Storage GB", integer(workload_summary.get("total_storage_gb")), ""],
            ["Generated At", generated_at, ""],
            ["Workbook Status", readiness_metadata["workbook_status"], ""],
            ["Assessment Readiness", readiness_metadata["readiness_label"], ""],
            ["Specialist Recommendation", readiness_metadata["recommendation"], ""],
            ["Internal Notes", readiness_metadata["recommendation_rationale"], ""],
            ["Native Remediation Status", readiness_metadata["native_remediation_status"], ""],
            ["Native Affected VM Count", readiness_metadata["native_affected_vm_count"], ""],
            ["Native Affected VMs", readiness_metadata["native_affected_vm_names"], ""],
            ["OCVS Pricing Completeness", readiness_metadata["ocvs_pricing_completeness"], ""],
            ["Hybrid Pricing Completeness", readiness_metadata["hybrid_pricing_completeness"], ""],
        ],
        integer_rows={3, 4, 5, 6, 13},
    )
    add_section(rows, row_styles, "Unresolved Blockers")
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Title", "Detail", "Affected VMs"],
        [
            [item["title"], item["detail"], item["affected_vms"]]
            for item in readiness_metadata["blockers"]
        ]
        or [["None", "No unresolved blockers.", "None"]],
    )
    add_section(rows, row_styles, "Unresolved Advisories")
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Title", "Detail", "Affected VMs"],
        [
            [item["title"], item["detail"], item["affected_vms"]]
            for item in readiness_metadata["advisories"]
        ]
        or [["None", "No unresolved advisories.", "None"]],
    )
    add_section(rows, row_styles, "Migration Path Price Comparison")
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Migration Path", "Monthly Cost", "Annual Cost", "3-Year Cost", "Cost / VM / Month", "Delta vs Native / Month", "Assessment Role"],
        scenario_costs,
        currency_cols={2, 3, 4, 5, 6},
    )
    add_section(rows, row_styles, "Decision Readout")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["Specialist Decision", readiness_metadata["recommendation"], ""],
            [
                "Decision Notes",
                readiness_metadata["recommendation_rationale"],
                "",
            ],
            [
                "Lowest complete modeled price",
                readiness_metadata["lowest_complete_scenario"],
                "Descriptive price result from the central readiness model.",
            ],
            ["Lowest complete monthly cost", lowest_complete_monthly, ""],
            ["Cost Difference Between Options", highest_monthly - lowest_monthly, "Monthly gap between lowest and highest modeled path."],
        ],
        currency_rows={4, 5},
    )
    add_note(
        rows,
        row_styles,
        "The specialist decision is separate from modeled price ranking. Validate application dependencies, migration waves, commercial terms, and official Oracle pricing before sharing externally.",
    )
    add_section(rows, row_styles, "Migration Path Options")
    add_note(
        rows,
        row_styles,
        "Use these migration path options as the executive discussion guide. Open each path in the application to tune sizing and assumptions before relying on the final price comparison.",
    )
    add_migration_path_cards(rows, row_styles, cell_styles)

    add_section(rows, row_styles, "Report Scope")
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Included", "Excluded from This Report"],
        [
            ["Estimated OCI infrastructure and licensing run-rate by migration path", "Professional services, project labor, training, downtime, application remediation"],
            ["Monthly, annual, and 3-year price exposure based on active assumptions", "Support uplift, contractual discounts outside the entered assumptions, and commercial quote adjustments"],
            ["Workload placement decisions and technical implications", "Backup retention, DR architecture, operational staffing, and full business case calculations"],
            ["Specialist decision and modeled migration price context", "Final commercial quotation or binding OCI Cost Estimator import"],
        ],
    )
    executive_sheet = {
        "name": "Executive Summary",
        "rows": rows,
        "row_styles": row_styles,
        "cell_styles": cell_styles,
        "column_widths": [52, 52, 52, 28, 28, 30, 34],
        "freeze_row": 1,
    }

    # Price Comparison
    rows, row_styles, cell_styles = new_sheet()
    add_title(rows, row_styles, "Price Comparison")
    add_section(rows, row_styles, "Price Signal")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            [
                "Lowest complete modeled price",
                readiness_metadata["lowest_complete_scenario"],
                "Descriptive price result from the central readiness model.",
            ],
            ["Lowest complete monthly cost", lowest_complete_monthly, ""],
            ["Monthly Spread", highest_monthly - lowest_monthly, "Gap between lowest and highest modeled path."],
            ["3-Year Spread", (highest_monthly - lowest_monthly) * 36.0, "Straight 36-month infrastructure and licensing exposure gap."],
            [
                "Specialist Decision",
                readiness_metadata["recommendation"],
                readiness_metadata["recommendation_rationale"],
            ],
        ],
        currency_rows={2, 3, 4},
    )
    ranked_rows = []
    for rank, row in enumerate(sorted(scenario_costs, key=lambda item: float(item[1] or 0.0)), start=1):
        scenario = scenario_by_id(str(row[0]).lower().replace("oci native", "native"))
        if not scenario:
            scenario = next((item for item in scenario_comparison.get("rows", []) if item.get("label") == row[0]), {})
        ranked_rows.append(
            [
                rank,
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                scenario.get("sizing_basis", ""),
            ]
        )
    add_section(rows, row_styles, "Ranked Migration Path Price Comparison")
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Rank", "Migration Path", "Monthly Cost", "Annual Cost", "3-Year Cost", "Cost / VM / Month", "Delta vs Native / Month", "Sizing Basis"],
        ranked_rows,
        integer_cols={1},
        currency_cols={3, 4, 5, 6, 7},
    )
    add_section(rows, row_styles, "Interpretation")
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Question", "What to Check"],
        [
            ["Is the lowest-cost path also the right migration path?", "Validate application dependencies, operational readiness, migration waves, and VMware feature dependency."],
            ["Why does a path cost more?", "Review compute shape assumptions, OCVS node count, datastore sizing, Windows licensing, and VCF price per core."],
            ["Can this be used as a quote?", "No. Use this as an assessment report, then validate final pricing with Oracle commercial tools."],
        ],
    )
    price_comparison_sheet = {
        "name": "Price Comparison",
        "rows": rows,
        "row_styles": row_styles,
        "cell_styles": cell_styles,
        "column_widths": [34, 38, 30, 30, 30, 28, 30, 118],
        "freeze_row": 1,
    }

    # OCI Native Analysis
    rows, row_styles, cell_styles = new_sheet()
    add_title(rows, row_styles, "OCI Native Analysis")
    add_section(rows, row_styles, "Path Readout")
    native_scenario = scenario_by_id("native")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["Monthly Cost", money(native_scenario.get("monthly_cost")), ""],
            ["Annual Cost", money(native_scenario.get("monthly_cost")) * 12.0, ""],
            ["3-Year Cost", money(native_scenario.get("monthly_cost")) * 36.0, "Straight 36-month price exposure."],
            ["Assessment Role", "Modernization baseline", ""],
        ],
        currency_rows={1, 2, 3},
    )
    add_note(
        rows,
        row_styles,
        "OCI Native sizing is indicative. Validate final shape, OS, licensing, storage, and performance assumptions with the official OCI pricing and architecture tools.",
    )
    add_section(rows, row_styles, "Resource Summary")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["VM Count", integer(overall["vm_count"]), ""],
            ["vCPU Count", integer(overall["total_cpus"]), ""],
            ["Memory GB", integer(overall["total_memory_gb"]), ""],
            ["Storage GB", integer(overall["total_provisioned_gb"]), ""],
        ],
        integer_rows={1, 2, 3, 4},
    )
    add_section(rows, row_styles, "Shape Distribution")
    add_table(rows, row_styles, cell_styles, ["Shape", "VM Count"], count_shape_distribution(vm_rows), integer_cols={2})
    add_section(rows, row_styles, "Operating System Analysis")
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Metric", "Count"],
        [
            ["Windows", count_os_family(vm_rows, "Windows")],
            ["Linux", count_os_family(vm_rows, "Linux")],
            ["Other / Unknown", count_os_family(vm_rows, "Other / Unknown")],
            ["OCI Supported", integer(workload_summary["oci_supported_count"])],
            ["OCI Unsupported", integer(workload_summary["oci_not_supported_count"])],
        ],
        integer_cols={2},
    )
    add_section(rows, row_styles, "Cost Breakdown")
    native_total = money(overall["total_monthly_cost"])
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Component", "Monthly Cost", "Annual Cost", "3-Year Cost", "Share of Monthly", "Notes"],
        [
            [
                "Compute + RAM",
                money(overall["total_cpu_ram_monthly_cost"]),
                money(overall["total_cpu_ram_monthly_cost"]) * 12.0,
                money(overall["total_cpu_ram_monthly_cost"]) * 36.0,
                percent(money(overall["total_cpu_ram_monthly_cost"]), native_total),
                "Flexible compute shape assumptions from Migration Paths.",
            ],
            [
                "Block Volume",
                money(overall["total_storage_monthly_cost"]),
                money(overall["total_storage_monthly_cost"]) * 12.0,
                money(overall["total_storage_monthly_cost"]) * 36.0,
                percent(money(overall["total_storage_monthly_cost"]), native_total),
                f"{integer(overall.get('total_vpus')):,} total VPUs modeled.",
            ],
            [
                "Windows Licensing",
                money(overall["total_os_license_monthly_cost"]),
                money(overall["total_os_license_monthly_cost"]) * 12.0,
                money(overall["total_os_license_monthly_cost"]) * 36.0,
                percent(money(overall["total_os_license_monthly_cost"]), native_total),
                f"{integer(overall.get('total_license_included_vms')):,} license-included Windows VM(s).",
            ],
            ["Total", native_total, native_total * 12.0, native_total * 36.0, 1.0 if native_total else 0.0, ""],
        ],
        currency_cols={2, 3, 4},
        percent_cols={5},
    )
    native_sheet = {
        "name": "OCI Native Analysis",
        "rows": rows,
        "row_styles": row_styles,
        "cell_styles": cell_styles,
        "column_widths": [34, 26, 26, 26, 22, 96],
        "freeze_row": 1,
    }

    # OCVS Analysis
    rows, row_styles, cell_styles = new_sheet()
    add_title(rows, row_styles, "OCVS Analysis")
    add_section(rows, row_styles, "Path Readout")
    ocvs_scenario = scenario_by_id("ocvs")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["Monthly Cost", money(ocvs_scenario.get("monthly_cost")), ""],
            ["Annual Cost", money(ocvs_scenario.get("monthly_cost")) * 12.0, ""],
            ["3-Year Cost", money(ocvs_scenario.get("monthly_cost")) * 36.0, "Straight 36-month price exposure."],
            ["Assessment Role", "VMware lift and shift", ""],
        ],
        currency_rows={1, 2, 3},
    )
    add_note(
        rows,
        row_styles,
        "OCVS sizing is indicative. Validate node count, cluster design, storage policy, VMware licensing, and final commercial pricing with Oracle and VMware/Broadcom guidance.",
    )
    add_section(rows, row_styles, "Workload Capacity Requirements")
    dense_usable_capacity = (
        integer(ocvs_selected.get("host_count")) * integer(ocvs_selected.get("usable_storage_gb_per_host"))
        if str(ocvs_selected.get("host_type", "")).lower() == "dense"
        else 0
    )
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["VM Count", total_vm_count, ""],
            ["vCPU Requirement", integer(ocvs_totals.get("vcpus")), "Inventory vCPU"],
            ["RAM Requirement GB", integer(ocvs_totals.get("memory_gb")), ""],
            ["Storage Requirement GB", integer(ocvs_totals.get("storage_gb")), ""],
            [
                "Dense Usable Capacity GB",
                dense_usable_capacity if dense_usable_capacity else "Not applicable",
                "Shown only for dense shapes with local vSAN capacity.",
            ],
        ],
        integer_rows={1, 2, 3, 4},
    )
    add_section(rows, row_styles, "OCVS Sizing Decision")
    selected_shape_reason = (
        f"{ocvs_selected.get('shape')} is selected by the active profile. "
        f"The capacity driver is {str(ocvs_selected.get('constraint', 'cost')).lower()}."
    )
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["Selected Shape", ocvs_selected.get("shape", ""), ocvs_selected.get("label", "")],
            ["Host Type", ocvs_selected.get("host_type", ""), ""],
            ["OCVS Commitment Term", ocvs_selected.get("commitment_label", ocvs_commitment_label), ""],
            [
                "OCVS Commitment Discount",
                float(ocvs_selected.get("commitment_discount_pct", 0.0) or 0.0) / 100.0,
                "Applied to OCVS host compute only.",
            ],
            ["Required Nodes Before Spare", integer(ocvs_selected.get("base_host_count")), ""],
            ["Spare Nodes", integer(ocvs_selected.get("dr_node_count")), ""],
            ["Total Nodes Including Spare", integer(ocvs_selected.get("host_count")), selected_shape_reason],
            ["Cluster Planning Note", "Multi-cluster planning required" if ocvs_selected.get("cluster_split_required") else "Single cluster", ""],
            ["OCPUs / Node", integer(ocvs_selected.get("ocpus_per_host")), ""],
            ["RAM GB / Node", integer(ocvs_selected.get("memory_gb_per_host")), ""],
        ],
        percent_rows={4},
        integer_rows={5, 6, 7, 9, 10},
    )
    add_section(rows, row_styles, "Capacity Drivers")
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Driver", "Inventory Requirement", "Required Nodes", "Utilization", "Notes"],
        [
            [
                "CPU",
                f"{integer(ocvs_totals.get('vcpus')):,} vCPU",
                integer(ocvs_selected.get("hosts_by_cpu")),
                float(ocvs_selected.get("cpu_utilization_pct", 0.0) or 0.0) / 100.0,
                f"{float(ocvs_policy.get('vcpu_per_ocpu', 0.0) or 0.0):.1f}:1 vCPU/OCPU, {float(ocvs_policy.get('cpu_headroom_pct', 0.0) or 0.0):.0f}% CPU headroom.",
            ],
            [
                "RAM",
                f"{integer(ocvs_totals.get('memory_gb')):,} GB",
                integer(ocvs_selected.get("hosts_by_memory")),
                float(ocvs_selected.get("memory_utilization_pct", 0.0) or 0.0) / 100.0,
                f"{float(ocvs_policy.get('memory_headroom_pct', 0.0) or 0.0):.0f}% RAM headroom.",
            ],
            [
                "Storage",
                f"{integer(ocvs_totals.get('storage_gb')):,} GB",
                "Block Volume" if str(ocvs_selected.get("host_type", "")).lower() == "standard" else integer(ocvs_selected.get("hosts_by_storage")),
                float(ocvs_selected.get("storage_utilization_pct", 0.0) or 0.0) / 100.0,
                "Standard shapes use Block Volume datastore; dense shapes use local vSAN usable capacity.",
            ],
        ],
        percent_cols={4},
    )
    add_section(rows, row_styles, "Cost Breakdown")
    ocvs_total = money(ocvs_scenario.get("monthly_cost"))
    ocvs_host_total = integer(ocvs_selected.get("host_count")) * money(ocvs_selected.get("host_monthly_cost"))
    ocvs_storage_total = money(ocvs_selected.get("storage_monthly_cost"))
    ocvs_vcf_total = money(vmware_full.get("monthly_cost"))
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Component", "Monthly Cost", "Annual Cost", "3-Year Cost", "Share of Monthly", "Notes"],
        [
            ["BM.Compute", ocvs_host_total, ocvs_host_total * 12.0, ocvs_host_total * 36.0, percent(ocvs_host_total, ocvs_total), f"{integer(ocvs_selected.get('host_count')):,} x {ocvs_selected.get('shape')}"],
            ["Datastore", ocvs_storage_total, ocvs_storage_total * 12.0, ocvs_storage_total * 36.0, percent(ocvs_storage_total, ocvs_total), "Block Volume datastore for Standard shapes; included in dense local vSAN model when zero."],
            ["VCF License", ocvs_vcf_total, ocvs_vcf_total * 12.0, ocvs_vcf_total * 36.0, percent(ocvs_vcf_total, ocvs_total), "User-entered VMware/Broadcom list price per physical core/year."],
            ["Total", ocvs_total, ocvs_total * 12.0, ocvs_total * 36.0, 1.0 if ocvs_total else 0.0, ""],
        ],
        currency_cols={2, 3, 4},
        percent_cols={5},
    )
    ocvs_sheet = {
        "name": "OCVS Analysis",
        "rows": rows,
        "row_styles": row_styles,
        "cell_styles": cell_styles,
        "column_widths": [34, 26, 26, 26, 22, 104],
        "freeze_row": 1,
    }

    # Hybrid Analysis
    rows, row_styles, cell_styles = new_sheet()
    add_title(rows, row_styles, "Hybrid Analysis")
    add_section(rows, row_styles, "Path Readout")
    hybrid_scenario = scenario_by_id("hybrid")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["Monthly Cost", money(hybrid_scenario.get("monthly_cost")), ""],
            ["Annual Cost", money(hybrid_scenario.get("monthly_cost")) * 12.0, ""],
            ["3-Year Cost", money(hybrid_scenario.get("monthly_cost")) * 36.0, "Straight 36-month price exposure."],
            ["Assessment Role", "Balanced placement", ""],
        ],
        currency_rows={1, 2, 3},
    )
    add_note(
        rows,
        row_styles,
        "Hybrid placement is indicative. Keep dependency groups together, validate manual placement decisions, and confirm both OCI Native and OCVS sizing in the official pricing tools.",
    )
    add_section(rows, row_styles, "Placement Summary")
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Target", "VM Count", "Share", "Meaning"],
        [
            ["OCI Native", hybrid_native_count, percent(hybrid_native_count, total_vm_count), "Workloads priced on OCI Compute and Block Volume."],
            ["OCVS", integer(hybrid_placement_plan.get("ocvs_count")), percent(integer(hybrid_placement_plan.get("ocvs_count")), total_vm_count), "Workloads priced on Oracle Cloud VMware Solution."],
        ],
        integer_cols={2},
        percent_cols={3},
    )
    add_section(rows, row_styles, "Placement Reasoning")
    add_table(rows, row_styles, cell_styles, ["Reason", "VM Count"], placement_reason_rows(), integer_cols={2})
    add_section(rows, row_styles, "Cost Breakdown")
    hybrid_total = money(hybrid_scenario.get("monthly_cost"))
    hybrid_native_total = money(supported_native_summary["total_monthly_cost"])
    hybrid_ocvs_total = money(hybrid_selected.get("total_monthly_cost")) + money(vmware_hybrid.get("monthly_cost"))
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Component", "Monthly Cost", "Annual Cost", "3-Year Cost", "Share of Monthly", "Notes"],
        [
            ["OCI Native Portion", hybrid_native_total, hybrid_native_total * 12.0, hybrid_native_total * 36.0, percent(hybrid_native_total, hybrid_total), f"{hybrid_native_count:,} VM(s) placed on OCI Native."],
            ["OCVS Portion", hybrid_ocvs_total, hybrid_ocvs_total * 12.0, hybrid_ocvs_total * 36.0, percent(hybrid_ocvs_total, hybrid_total), f"{hybrid_ocvs_priced_count:,} VM(s) placed on OCVS."],
            ["Total", hybrid_total, hybrid_total * 12.0, hybrid_total * 36.0, 1.0 if hybrid_total else 0.0, ""],
        ],
        currency_cols={2, 3, 4},
        percent_cols={5},
    )
    add_section(rows, row_styles, "OCI Native Portion")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["Native VM Count", hybrid_native_count, ""],
            ["Native vCPU", integer(supported_native_summary.get("total_cpus")), ""],
            ["Native RAM GB", integer(supported_native_summary.get("total_memory_gb")), ""],
            ["Native Storage GB", integer(supported_native_summary.get("total_provisioned_gb")), ""],
            ["Native Monthly Cost", hybrid_native_total, ""],
        ],
        integer_rows={1, 2, 3, 4},
        currency_rows={5},
    )
    add_section(rows, row_styles, "OCVS Portion")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["OCVS VM Count", integer(hybrid_ocvs_priced_count), ""],
            ["OCVS vCPU", integer(hybrid_totals.get("vcpus")), ""],
            ["OCVS RAM GB", integer(hybrid_totals.get("memory_gb")), ""],
            ["OCVS Storage GB", integer(hybrid_totals.get("storage_gb")), ""],
            ["Selected OCVS Shape", hybrid_selected.get("shape", ""), hybrid_selected.get("label", "")],
            ["Required Nodes Before Spare", integer(hybrid_selected.get("base_host_count")), ""],
            ["Spare Nodes", integer(hybrid_selected.get("dr_node_count")), ""],
            ["Total Nodes Including Spare", integer(hybrid_selected.get("host_count")), ""],
            ["Sizing Driver", str(hybrid_selected.get("constraint", "")).capitalize(), ""],
            ["Cluster Planning Note", "Multi-cluster planning required" if hybrid_selected.get("cluster_split_required") else "Single cluster", ""],
        ],
        integer_rows={1, 2, 3, 4, 6, 7, 8},
    )
    hybrid_sheet = {
        "name": "Hybrid Analysis",
        "rows": rows,
        "row_styles": row_styles,
        "cell_styles": cell_styles,
        "column_widths": [34, 26, 26, 26, 22, 104],
        "freeze_row": 1,
    }

    # Hybrid Placement Detail
    rows, row_styles, cell_styles = new_sheet()
    add_title(rows, row_styles, "Hybrid Placement Detail")
    add_note(
        rows,
        row_styles,
        "Hybrid migration path VM-level view for architecture review. Use this sheet to validate OS support, manual Hybrid placement, and Native sizing assumptions before customer sign-off.",
    )
    support_source_available = bool(hybrid_placement_plan.get("support_source_available"))
    placement_detail_rows: list[list[Any]] = []
    for row in sorted(hybrid_placement_rows or vm_rows, key=lambda item: str(item.get("vm_name", "")).lower()):
        if support_source_available:
            oci_supported = "Yes" if bool(row.get("hybrid_is_oci_supported")) else "No"
        else:
            oci_supported = "Unknown"
        placement_detail_rows.append(
            [
                row.get("vm_name", ""),
                row.get("os_name", ""),
                row.get("power_state", ""),
                integer(row.get("cpus")),
                integer(row.get("memory_gb")),
                integer(row.get("provisioned_gb")),
                oci_supported,
                row.get("hybrid_recommended_label", ""),
                row.get("hybrid_placement_label", ""),
                "Yes" if row.get("hybrid_manual_override") else "",
                row.get("oci_shape", ""),
                integer(row.get("ocpu")),
                integer(row.get("vpu")),
                vm_monthly(row),
                row.get("hybrid_reason", ""),
            ]
        )
    add_table(
        rows,
        row_styles,
        cell_styles,
        [
            "VM Name",
            "OS",
            "Power State",
            "vCPU",
            "RAM GB",
            "Storage GB",
            "OCI Supported",
            "Recommended Target",
            "Hybrid Target",
            "Manual Override",
            "OCI Shape",
            "OCPUs",
            "VPU",
            "Native Monthly Cost",
            "Placement Reason",
        ],
        placement_detail_rows,
        integer_cols={4, 5, 6, 12, 13},
        currency_cols={14},
    )
    placement_sheet = {
        "name": "Hybrid Placement",
        "rows": rows,
        "row_styles": row_styles,
        "cell_styles": cell_styles,
        "column_widths": [32, 56, 18, 12, 14, 16, 18, 24, 20, 20, 26, 12, 12, 22, 82],
        "freeze_row": 4,
    }

    # Selected VMs
    vm_cost_headers = [
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
    rows, row_styles, cell_styles = new_sheet()
    add_table(
        rows,
        row_styles,
        cell_styles,
        vm_cost_headers,
        vm_costing_detail_rows(vm_rows),
        integer_cols={4, 5, 7, 8, 9},
        currency_cols={11, 12, 13, 14, 15, 16, 17, 18},
    )
    selected_vms_sheet = {
        "name": "Selected VMs",
        "rows": rows,
        "row_styles": row_styles,
        "cell_styles": cell_styles,
        "column_widths": [32, 56, 18, 12, 12, 12, 16, 16, 12, 24, 20, 20, 24, 28, 20, 24, 26, 24],
        "freeze_row": 1,
    }

    # Non-Selected VMs
    rows, row_styles, cell_styles = new_sheet()
    add_table(
        rows,
        row_styles,
        cell_styles,
        vm_cost_headers,
        vm_costing_detail_rows(non_selected_vm_rows),
        integer_cols={4, 5, 7, 8, 9},
        currency_cols={11, 12, 13, 14, 15, 16, 17, 18},
    )
    non_selected_vms_sheet = {
        "name": "Non-Selected VMs",
        "rows": rows,
        "row_styles": row_styles,
        "cell_styles": cell_styles,
        "column_widths": [32, 56, 18, 12, 12, 12, 16, 16, 12, 24, 20, 20, 24, 28, 20, 24, 26, 24],
        "freeze_row": 1,
    }

    # Price List
    rows, row_styles, cell_styles = new_sheet()
    first_price_row = add_table(
        rows,
        row_styles,
        cell_styles,
        ["OCI Target Shape", "OCPU Unit Price", "Memory Unit Price", "Currency", "Parameter", "Value", "Currency"],
        price_list_rows(),
    )
    for offset, price_row in enumerate(price_list_rows(), start=0):
        parameter = str(price_row[4] if len(price_row) > 4 else "")
        row_idx = first_price_row + offset
        if str(price_row[0] if len(price_row) > 0 else ""):
            cell_styles[(row_idx, 2)] = STYLE_UNIT_PRICE
            cell_styles[(row_idx, 3)] = STYLE_UNIT_PRICE
        if parameter in {
            "Block Storage Unit Price",
            "Block Performance Unit Price",
            "Windows OS Unit Price",
        }:
            cell_styles[(row_idx, 6)] = STYLE_UNIT_PRICE
        elif parameter in {
            "VCF List Price / Core / Year",
        }:
            cell_styles[(row_idx, 6)] = STYLE_CURRENCY
        elif parameter == "IaaS Discount %":
            cell_styles[(row_idx, 6)] = STYLE_PERCENT
        elif parameter == "OCVS Commitment Discount %":
            cell_styles[(row_idx, 6)] = STYLE_PERCENT
    price_list_sheet = {
        "name": "Price List",
        "rows": rows,
        "row_styles": row_styles,
        "cell_styles": cell_styles,
        "column_widths": [26, 22, 22, 14, 44, 22, 14],
        "freeze_row": 1,
    }

    # Technical Details
    rows, row_styles, cell_styles = new_sheet()
    add_title(rows, row_styles, "Technical Details")
    add_section(rows, row_styles, "Export Metadata")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["Customer", customer_name or "Not provided", ""],
            ["Generated at", generated_at, ""],
            ["Export file", export_path_display, ""],
            ["Currency", pricing_currency or "USD", ""],
            ["Source price list", source_pricelist_file or "No price list loaded", ""],
            ["VM inventory source", source_vinfo_csv or "No VM inventory source loaded", ""],
            ["Last saved settings", step4_last_updated_at or "Not saved", ""],
        ],
    )
    add_section(rows, row_styles, "Pricing Assumptions")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["IaaS discount", float(iaas_discount_pct) / 100.0, "Applied to OCI compute/storage run-rate where modeled."],
            ["OCVS Commitment Term", ocvs_commitment_label, "Applied to OCVS host compute only."],
            ["OCVS Commitment Discount", ocvs_commitment_discount_pct / 100.0, "Selected shape discount for the active term."],
            ["Block Volume capacity unit price", money(block_storage_unit_price), pricing_currency or "USD"],
            ["Block Volume performance unit price", money(block_perf_unit_price), pricing_currency or "USD"],
            ["Windows OS unit price", money(windows_os_unit_price), pricing_currency or "USD"],
            ["VCF list price / core / year", money(vmware_license_price_per_core_yearly), pricing_currency or "USD"],
        ],
        percent_rows={1, 3},
        currency_rows={4, 5, 6, 7},
    )
    add_section(rows, row_styles, "OCVS Profile Assumptions")
    add_key_values(
        rows,
        row_styles,
        cell_styles,
        [
            ["OCVS profile", "Lowest cost" if ocvs_profile_choice == "best_fit" else ocvs_profile_choice, ""],
            ["OCVS Commitment Term", ocvs_commitment_label, ""],
            ["OCVS Commitment Discount", ocvs_commitment_discount_pct / 100.0, ""],
            ["vCPU per OCPU", float(ocvs_policy["vcpu_per_ocpu"]), ""],
            ["CPU headroom", float(ocvs_policy["cpu_headroom_pct"]) / 100.0, ""],
            ["RAM headroom", float(ocvs_policy["memory_headroom_pct"]) / 100.0, ""],
            ["Storage headroom", float(ocvs_policy["storage_headroom_pct"]) / 100.0, ""],
            ["Dense vSAN usable", float(ocvs_policy["dense_vsan_usable_pct"]) / 100.0, ""],
            ["Standard datastore VPU", integer(ocvs_policy["standard_storage_vpu"]), "VPU/GB"],
            ["Spare nodes", integer(ocvs_dr_nodes), ""],
        ],
        percent_rows={3, 5, 6, 7, 8},
        integer_rows={9, 10},
    )
    add_section(rows, row_styles, "Shape Mapping and Pricing")
    add_table(
        rows,
        row_styles,
        cell_styles,
        [
            "Shape",
            "Label",
            "Type",
            "OCPUs / Host",
            "RAM GB / Host",
            "Max Hosts",
            "Selected Full OCVS",
            "Selected Hybrid OCVS",
            "Host Monthly",
            "Datastore Monthly",
            "VCF Monthly",
            "Total Monthly",
        ],
        [
            [
                row.get("shape", ""),
                row.get("label", ""),
                row.get("host_type", ""),
                integer(row.get("ocpus_per_host")),
                integer(row.get("memory_gb_per_host")),
                integer(row.get("max_hosts")),
                "Yes" if row.get("is_selected") else "",
                "Yes" if row.get("shape") == hybrid_selected.get("shape") else "",
                money(row.get("host_total_monthly_cost")),
                money(row.get("storage_monthly_cost")),
                money(row.get("vmware_license_monthly_cost")),
                money(row.get("selection_monthly_cost")),
            ]
            for row in ocvs_shape_comparison.get("rows", [])
        ],
        integer_cols={4, 5, 6},
        currency_cols={9, 10, 11, 12},
    )
    add_section(rows, row_styles, "Placement Rules")
    add_table(
        rows,
        row_styles,
        cell_styles,
        ["Rule", "Treatment"],
        [
            ["OCI-supported OS", "Recommended for OCI Native placement."],
            ["Unsupported OS", "Recommended for OCVS placement."],
            ["Manual placement", "Overrides the recommendation and is preserved in the Hybrid planner."],
        ],
    )
    add_section(rows, row_styles, "Sizing Summary Notes")
    sizing_note_rows = (
        [[note["title"], note["detail"]] for note in fit_warnings]
        if fit_warnings
        else [["Sizing summary", "No sizing notes were generated for the current assumptions."]]
    )
    add_table(rows, row_styles, cell_styles, ["Topic", "Detail"], sizing_note_rows)
    technical_sheet = {
        "name": "Technical Details",
        "rows": rows,
        "row_styles": row_styles,
        "cell_styles": cell_styles,
        "column_widths": [38, 38, 30, 18, 18, 16, 20, 22, 20, 20, 20, 20],
        "freeze_row": 1,
    }

    sheets = [
        executive_sheet,
        price_comparison_sheet,
        native_sheet,
        ocvs_sheet,
        hybrid_sheet,
        placement_sheet,
        selected_vms_sheet,
        non_selected_vms_sheet,
        price_list_sheet,
        technical_sheet,
    ]
    return _build_xlsx_workbook_bytes(
        sheets,
        currency_fmt_code=_xlsx_currency_format_code(pricing_currency),
    )


@app.post("/assessment/import")
def import_assessment_route() -> Any:
    content_length = request.content_length
    if content_length is None or content_length <= 0:
        flash(
            "Portable assessment upload requires a Content-Length header.",
            "error",
        )
        return redirect(url_for("index"), code=303)
    if content_length > MAX_PORTABLE_REQUEST_BYTES:
        flash(
            "Portable assessment upload exceeds the 25 MiB package limit.",
            "error",
        )
        return redirect(url_for("index"), code=303)

    try:
        request.max_content_length = MAX_PORTABLE_REQUEST_BYTES
    except (AttributeError, TypeError):
        pass
    try:
        valid_form = (
            set(request.form) == {"action"}
            and request.form.getlist("action") == ["import_assessment"]
        )
        valid_files = (
            set(request.files) == {"assessment_file"}
            and len(request.files.getlist("assessment_file")) == 1
        )
    except RequestEntityTooLarge:
        flash(
            "Portable assessment upload exceeds the 25 MiB package limit.",
            "error",
        )
        return redirect(url_for("index"), code=303)

    if not valid_form or not valid_files:
        flash(
            "Submit exactly one portable assessment JSON file and no other fields.",
            "error",
        )
        return redirect(url_for("index"), code=303)

    upload = request.files.get("assessment_file")
    original_name = secure_filename(upload.filename if upload else "")
    if not upload or not original_name:
        flash("Choose a portable assessment JSON file to import.", "error")
    elif Path(original_name).suffix.lower() != ".json":
        flash("Only .json portable assessment files can be imported.", "error")
    else:
        try:
            raw_package = upload.stream.read(MAX_PACKAGE_BYTES + 1)
            if len(raw_package) > MAX_PACKAGE_BYTES:
                raise PortableAssessmentError(
                    "Portable assessment exceeds the 25 MiB size limit."
                )
            decoded = raw_package.decode("utf-8-sig")
            parsed_package = json.loads(decoded)
            validated_package = validate_portable_package(parsed_package)
            import_result = import_portable_assessment(validated_package)
        except UnicodeDecodeError:
            flash(
                "Portable assessment JSON must use UTF-8 encoding.",
                "error",
            )
        except json.JSONDecodeError:
            flash("Portable assessment JSON is malformed.", "error")
        except PortableAssessmentError as exc:
            flash(str(exc), "error")
        except Exception:
            app.logger.exception("Portable assessment import failed")
            flash(
                "The portable assessment could not be imported. "
                "The current assessment was kept.",
                "error",
            )
        else:
            currency_label = import_result.get("currency") or "no currency"
            flash(
                f"Assessment imported: {import_result['name']} - "
                f"{int(import_result['vm_count']):,} VM(s) - {currency_label}.",
                "success",
            )
            for warning in import_result.get("warnings", []):
                flash(str(warning), "info")

    return redirect(url_for("index"), code=303)


@app.route("/", methods=["GET", "POST"])
def index() -> Any:
    _cleanup_legacy_session_keys()

    download_info: dict[str, Any] | None = None
    selected_rvtools_file = str(session.get("selected_rvtools_file", ""))
    rvtools_file_info: dict[str, Any] | None = session.get("rvtools_file_info")
    rvtools_import_summary: dict[str, Any] | None = session.get("rvtools_import_summary")
    rvtools_rejected_info: dict[str, Any] | None = session.get("rvtools_rejected_info")
    selected_currency = str(session.get("selected_currency", "")).upper().strip()
    rvtools_files = list_rvtools_export_files()
    downloaded_price_lists = list_downloaded_price_lists()
    selected_pricelist_file = str(session.get("selected_pricelist_file", "")).strip().replace("\\", "/")
    customer_name = normalize_customer_name(session.get("customer_name", ""))
    active_assessment_id = _clean_assessment_id(session.get("active_assessment_id", ""))
    active_assessment_name = normalize_assessment_name(session.get("active_assessment_name", ""))
    active_assessment_notes = normalize_assessment_notes(session.get("active_assessment_notes", ""))
    field_errors: dict[str, str] = {}
    manual_sizing_values: dict[str, Any] | None = None
    inventory_mode = "manual" if is_manual_inventory_path(selected_rvtools_file) else "upload"

    if not selected_pricelist_file:
        preferences = load_preferences()
        last_price_file = str(preferences.get("last_selected_pricelist_file", "")).strip().replace("\\", "/")
        if last_price_file and last_price_file in downloaded_price_lists:
            selected_pricelist_file = last_price_file
            session["selected_pricelist_file"] = selected_pricelist_file
            selected_currency = str(preferences.get("last_selected_currency", selected_currency)).upper().strip()
            if selected_currency:
                session["selected_currency"] = selected_currency

    if selected_pricelist_file and selected_pricelist_file not in downloaded_price_lists:
        selected_pricelist_file = ""
        session.pop("selected_pricelist_file", None)

    price_list_options = downloaded_price_lists[:MAX_VISIBLE_PRICE_LISTS]

    selected_pricelist_info: dict[str, Any] | None = None
    if selected_pricelist_file:
        price_lookup_preview, selected_pricing_currency, source_file = load_price_lookup(selected_pricelist_file)
        if source_file:
            source_info = build_source_file_info(source_file)
            selected_pricelist_info = {
                **source_info,
                "currency": selected_pricing_currency or "Unknown",
                "item_count": len(price_lookup_preview),
            }

    def render_index_response() -> str:
        current_inventory_rows: list[dict[str, Any]] = []
        current_inventory_issues: list[dict[str, Any]] = []
        if selected_rvtools_file:
            try:
                current_inventory_rows, _ = load_vms_from_vinfo(selected_rvtools_file)
            except Exception:
                current_inventory_rows = []
            else:
                current_inventory_issues = build_inventory_review_issues(
                    current_inventory_rows
                )
        current_state = load_app_state()
        current_selected_names = current_state.get("selected_vm_names", [])
        if not isinstance(current_selected_names, list):
            current_selected_names = []
        readiness = build_current_readiness_context(
            inventory_rows=current_inventory_rows,
            selected_vm_names=current_selected_names,
            scenario_analysis=None,
            scenario_views=None,
            app_state=current_state,
            setup_metadata={
                "assessment_name": active_assessment_name,
                "customer_name": customer_name,
                "has_price_list": bool(
                    selected_pricelist_info
                    and int(selected_pricelist_info.get("item_count", 0) or 0) > 0
                ),
                "has_inventory": bool(current_inventory_rows),
            },
            has_unsaved_scenario_changes=False,
            inventory_issues=current_inventory_issues,
            pricing_inputs=None,
        )
        return render_template(
            "index.html",
            **build_workspace_context(
                "setup",
                readiness=readiness,
                currencies=SUPPORTED_CURRENCIES,
                selected_currency=selected_currency,
                download_info=download_info,
                downloaded_price_lists=downloaded_price_lists,
                price_list_options=price_list_options,
                price_list_choices=build_catalog_choices(price_list_options, "pricing"),
                selected_pricelist_file=selected_pricelist_file,
                selected_pricelist_info=selected_pricelist_info,
                rvtools_files=rvtools_files,
                rvtools_file_choices=build_catalog_choices(rvtools_files, "inventory"),
                selected_rvtools_file=selected_rvtools_file,
                rvtools_file_info=rvtools_file_info,
                rvtools_import_summary=rvtools_import_summary,
                rvtools_rejected_info=rvtools_rejected_info,
                customer_name=customer_name,
                manual_sizing_form=build_manual_sizing_form(selected_rvtools_file, manual_sizing_values),
                inventory_mode=inventory_mode,
                field_errors=field_errors,
                rvtools_catalog_path=str(RVTOOLS_DIR).replace("\\", "/"),
                inventory_review_issues=current_inventory_issues,
                saved_assessments=list_saved_assessments(),
                active_assessment_id=active_assessment_id,
                active_assessment_name=active_assessment_name,
                active_assessment_notes=active_assessment_notes,
            ),
        )

    if request.method == "POST":
        action = request.form.get("action", "")
        requested_inventory_mode = str(request.form.get("inventory_mode", "")).strip().lower()
        if requested_inventory_mode in {"upload", "manual"}:
            inventory_mode = requested_inventory_mode

        def clear_rejected_inventory() -> None:
            nonlocal rvtools_rejected_info
            rvtools_rejected_info = None
            session.pop("rvtools_rejected_info", None)

        def reject_inventory_candidate(
            candidate_path: str,
            file_info: dict[str, Any],
            reason: str,
            field_id: str,
            owned_candidate_path: str,
        ) -> None:
            nonlocal rvtools_rejected_info
            cleanup_owned_inventory_candidate(
                candidate_path,
                owned_candidate_path,
                selected_rvtools_file,
            )
            rvtools_rejected_info = build_rejected_inventory_info(file_info, reason)
            field_errors[field_id] = "This file could not be used as VM inventory. Review Source Details."
            flash(
                f"Input not accepted for sizing: {rvtools_rejected_info['category']}. Your current inventory was kept.",
                "rvtools_error",
            )

        def validate_and_select_inventory(
            path_text: str,
            file_info: dict[str, Any],
            success_message: str,
            *,
            field_id: str,
            owned_candidate_path: str = "",
            select_all_rows: bool = False,
        ) -> bool:
            nonlocal selected_rvtools_file, rvtools_file_info, rvtools_import_summary
            nonlocal rvtools_rejected_info
            try:
                vm_rows, source = load_vms_from_vinfo(path_text)
                candidate_summary = build_inventory_import_summary(vm_rows, source)
                build_inventory_review_issues(vm_rows)
            except Exception as exc:
                app.logger.exception("Stage 1 inventory candidate validation failed")
                reject_inventory_candidate(
                    path_text,
                    file_info,
                    str(exc),
                    field_id,
                    owned_candidate_path,
                )
                return False

            replacement_state = _default_app_state()
            if select_all_rows:
                replacement_state["selected_vm_names"] = [
                    str(row.get("name", ""))
                    for row in vm_rows
                    if str(row.get("name", ""))
                ]

            prior_app_state = load_app_state()
            inventory_session_keys = (
                "selected_rvtools_file",
                "rvtools_file_info",
                "rvtools_import_summary",
                "rvtools_rejected_info",
            )
            prior_session_values = {
                key: (key in session, copy.deepcopy(session.get(key)))
                for key in inventory_session_keys
            }
            prior_selected_file = selected_rvtools_file
            prior_file_info = copy.deepcopy(rvtools_file_info)
            prior_import_summary = copy.deepcopy(rvtools_import_summary)
            prior_rejected_info = copy.deepcopy(rvtools_rejected_info)
            prior_step4_snapshot = load_step4_snapshot()

            try:
                save_app_state(replacement_state)
            except Exception:
                app.logger.exception("Stage 1 inventory activation failed")
                cleanup_owned_inventory_candidate(
                    path_text,
                    owned_candidate_path,
                    prior_selected_file,
                )
                rvtools_rejected_info = build_rejected_inventory_info(
                    file_info,
                    "Inventory activation could not be completed.",
                )
                field_errors[field_id] = "The inventory source could not be activated."
                flash("Inventory source could not be activated. Your current inventory was kept.", "rvtools_error")
                return False

            try:
                clear_rejected_inventory()
                selected_rvtools_file = path_text
                rvtools_file_info = file_info
                rvtools_import_summary = candidate_summary
                session["selected_rvtools_file"] = selected_rvtools_file
                session["rvtools_file_info"] = rvtools_file_info
                session["rvtools_import_summary"] = rvtools_import_summary
                clear_step4_snapshot()
            except Exception:
                app.logger.exception("Stage 1 inventory post-persistence activation failed")
                try:
                    save_app_state(prior_app_state)
                except Exception:
                    app.logger.exception("Stage 1 inventory state rollback failed")
                for key, (was_present, prior_value) in prior_session_values.items():
                    if was_present:
                        session[key] = prior_value
                    else:
                        session.pop(key, None)
                selected_rvtools_file = prior_selected_file
                rvtools_file_info = prior_file_info
                rvtools_import_summary = prior_import_summary
                rvtools_rejected_info = prior_rejected_info
                try:
                    if prior_step4_snapshot:
                        save_step4_snapshot(prior_step4_snapshot)
                    else:
                        clear_step4_snapshot()
                except Exception:
                    app.logger.exception("Stage 1 inventory snapshot rollback failed")
                cleanup_owned_inventory_candidate(
                    path_text,
                    owned_candidate_path,
                    prior_selected_file,
                )
                field_errors[field_id] = "The inventory source could not be activated."
                flash("Inventory source could not be activated. Your current inventory was kept.", "rvtools_error")
                return False

            flash(success_message, "rvtools_success")
            return True

        if action == "export_assessment":
            try:
                requested_name = request.form.get(
                    "assessment_name",
                    active_assessment_name,
                )
                requested_notes = request.form.get(
                    "assessment_notes",
                    active_assessment_notes,
                )
                package, filename = build_current_portable_assessment(
                    requested_name,
                    requested_notes,
                )
                payload = dumps_portable_package(package).encode("utf-8")
                response = send_file(
                    io.BytesIO(payload),
                    mimetype="application/json",
                    as_attachment=True,
                    download_name=filename,
                )
                response.headers["Content-Type"] = "application/json; charset=utf-8"
                if _clean_assessment_id(session.get("active_assessment_id", "")):
                    save_current_assessment(requested_name, requested_notes)
            except PortableAssessmentError as exc:
                flash(str(exc), "error")
            except Exception:
                app.logger.exception("Portable assessment export failed")
                flash(
                    "The current assessment could not be exported as portable JSON.",
                    "error",
                )
            else:
                return response

        elif action == "save_customer_name":
            customer_name = normalize_customer_name(request.form.get("customer_name", ""))
            if customer_name:
                session["customer_name"] = customer_name
                flash("Customer name saved.", "customer_success")
            else:
                session.pop("customer_name", None)
                flash("Customer name cleared.", "customer_success")

        elif action == "start_fresh_assessment":
            reset_active_assessment_state()
            selected_rvtools_file = ""
            rvtools_file_info = None
            rvtools_import_summary = None
            rvtools_rejected_info = None
            customer_name = ""
            active_assessment_id = ""
            active_assessment_name = ""
            active_assessment_notes = ""
            manual_sizing_values = None
            inventory_mode = "upload"
            flash("Started a fresh assessment. The selected OCI price list was kept.", "success")

        elif action == "save_assessment":
            return_to = _safe_internal_return_path(request.form.get("return_to", ""))
            if not return_to:
                return_to = _safe_internal_referrer_path(
                    request.headers.get("Referer", ""),
                    request.host_url,
                )
            prior_customer = (
                "customer_name" in session,
                session.get("customer_name"),
            )
            if "customer_name" in request.form:
                customer_name = normalize_customer_name(request.form.get("customer_name", ""))
                if customer_name:
                    session["customer_name"] = customer_name
                else:
                    session.pop("customer_name", None)
            try:
                saved_snapshot = save_current_assessment(
                    request.form.get("assessment_name", active_assessment_name),
                    request.form.get("assessment_notes", active_assessment_notes),
                )
            except Exception:
                app.logger.exception("Stage 1 assessment save failed")
                if prior_customer[0]:
                    session["customer_name"] = prior_customer[1]
                else:
                    session.pop("customer_name", None)
                flash("Assessment could not be saved. Try again.", "error")
            else:
                active_assessment_id = str(saved_snapshot.get("id") or "")
                active_assessment_name = normalize_assessment_name(saved_snapshot.get("name"))
                active_assessment_notes = normalize_assessment_notes(saved_snapshot.get("notes"))
                flash("Assessment saved.", "success")
            if return_to:
                return redirect(return_to, code=303)

        elif action == "load_assessment":
            try:
                result = load_saved_assessment(request.form.get("assessment_id", ""))
            except Exception:
                app.logger.exception("Stage 1 assessment load failed")
                flash("Saved assessment could not be loaded. Try again.", "error")
            else:
                if result.get("ok"):
                    restored_inventory_path = str(session.get("selected_rvtools_file", ""))
                    inventory_mode = "manual" if is_manual_inventory_path(restored_inventory_path) else "upload"
                    flash("Assessment loaded.", "success")
                    for warning in result.get("warnings", []):
                        flash(str(warning), "info")
                else:
                    flash("Saved assessment could not be loaded.", "error")

        elif action == "delete_assessment":
            try:
                result = delete_saved_assessment(request.form.get("assessment_id", ""))
            except Exception:
                app.logger.exception("Stage 1 assessment delete failed")
                flash("Saved assessment could not be deleted. Try again.", "error")
            else:
                if result.get("ok"):
                    flash("Assessment deleted.", "success")
                else:
                    flash("Saved assessment could not be deleted.", "error")

        elif action == "download_pricing":
            selected_currency = request.form.get("currency_code", "USD").upper().strip()

            if selected_currency not in SUPPORTED_CURRENCIES:
                field_errors["currency_code"] = "Select a supported currency."
                flash("Please select a supported currency.", "pricing_error")
                return render_index_response()

            def use_local_price_list_fallback(_reason: str) -> bool:
                nonlocal selected_pricelist_file, selected_pricelist_info
                try:
                    fallback_file = find_downloaded_price_list_for_currency(selected_currency)
                    if not fallback_file:
                        return False
                    price_lookup_preview, fallback_currency, source_file = load_price_lookup(fallback_file)
                    if not source_file:
                        return False

                    selected_pricelist_file = source_file
                    session["selected_pricelist_file"] = source_file
                    remember_price_list_selection(source_file, fallback_currency or selected_currency)
                    selected_pricelist_info = {
                        **build_source_file_info(source_file),
                        "currency": fallback_currency or selected_currency,
                        "item_count": len(price_lookup_preview),
                    }
                except Exception:
                    app.logger.exception("Stage 1 pricing fallback failed")
                    return False

                flash(
                    f"Live {selected_currency} price-list download did not complete. "
                    f"Using existing local {selected_currency} price list.",
                    "pricing_info",
                )
                return True

            def persist_downloaded_price_list(payload: dict[str, Any], message: str, category: str) -> None:
                nonlocal download_info, selected_pricelist_file, selected_pricelist_info
                payload = filter_compute_vm_items(payload)
                saved_file = save_price_list(selected_currency, payload)
                item_count = len(payload.get("items", []))
                selected_pricelist_file = str(saved_file).replace("\\", "/")

                download_info = {
                    **build_source_file_info(saved_file),
                    "currency": selected_currency,
                    "last_updated": payload.get("lastUpdated", "Unknown"),
                    "item_count": item_count,
                }
                selected_pricelist_info = {
                    **build_source_file_info(selected_pricelist_file),
                    "currency": selected_currency,
                    "item_count": item_count,
                }
                session["selected_pricelist_file"] = selected_pricelist_file
                remember_price_list_selection(selected_pricelist_file, selected_currency)
                flash(message, category)

            try:
                session["selected_currency"] = selected_currency
                payload = fetch_oci_price_list(selected_currency)
                persist_downloaded_price_list(payload, "OCI price list downloaded successfully.", "pricing_success")
            except HTTPError as exc:
                app.logger.exception("Stage 1 pricing download HTTP failure")
                if not use_local_price_list_fallback(f"HTTP {exc.code}"):
                    field_errors["currency_code"] = "The latest price list could not be downloaded for this currency."
                    flash(
                        f"Oracle API returned an HTTP error ({exc.code}). No local {selected_currency} price list was found.",
                        "pricing_error",
                    )
            except URLError as exc:
                app.logger.exception("Stage 1 pricing download connection failure")
                reason = getattr(exc, "reason", None)
                guidance = ""
                if reason and "CERTIFICATE_VERIFY_FAILED" in str(reason):
                    guidance = " Please install/update trusted CA certificates (or certifi)."
                if not use_local_price_list_fallback("API timeout/connectivity issue"):
                    field_errors["currency_code"] = "The pricing service could not be reached for this currency."
                    flash(
                        "No price list was downloaded because the Oracle pricing API could not be reached "
                        f"after several {PRICE_LIST_DOWNLOAD_TIMEOUT_SECONDS}-second attempts. "
                        f"No local {selected_currency} price list was found. Check internet/proxy access or select another existing local price list."
                        f"{guidance}",
                        "pricing_error",
                    )
            except (TimeoutError, ValueError, json.JSONDecodeError):
                app.logger.exception("Stage 1 pricing response processing failed")
                if not use_local_price_list_fallback("Pricing response processing failed"):
                    field_errors["currency_code"] = "The pricing response could not be processed for this currency."
                    flash(
                        f"The OCI pricing response could not be processed. No local {selected_currency} price list was found.",
                        "pricing_error",
                    )
            except Exception:  # pragma: no cover - fallback guard
                app.logger.exception("Stage 1 pricing download failed")
                field_errors["currency_code"] = "The latest price list could not be downloaded."
                flash(
                    "The latest OCI price list could not be downloaded. Try again or use an existing local price list.",
                    "pricing_error",
                )

        elif action == "select_rvtools_file":
            inventory_mode = "upload"
            candidate_path = resolve_catalog_selection(request.form.get("rvtools_file", ""), rvtools_files)
            if not candidate_path:
                field_errors["rvtools_file"] = "Select an available inventory file."
                flash("Please select a valid VM inventory export file.", "rvtools_error")
            else:
                candidate_info = build_source_file_info(candidate_path)
                validate_and_select_inventory(
                    candidate_path,
                    candidate_info,
                    "VM inventory export file selected and validated successfully.",
                    field_id="rvtools_file",
                )

        elif action == "upload_rvtools_file":
            inventory_mode = "upload"
            upload = request.files.get("rvtools_upload")
            original_name = secure_filename(upload.filename if upload else "")
            suffix = Path(original_name).suffix.lower()
            if not upload or not original_name:
                field_errors["rvtools_upload"] = "Choose an inventory file to upload."
                flash("Please choose a VM inventory export file to upload.", "rvtools_error")
            elif original_name.startswith("~$") or original_name.startswith("."):
                field_errors["rvtools_upload"] = "Temporary or hidden files cannot be used."
                flash("Temporary or hidden workbook files cannot be used as VM inventory input.", "rvtools_error")
            elif suffix not in SUPPORTED_RVTOOLS_EXTENSIONS:
                field_errors["rvtools_upload"] = "Use an .xlsx, .xlsm, or .csv inventory file."
                flash("Only .xlsx, .xlsm, and .csv VM inventory files are supported.", "rvtools_error")
            else:
                RVTOOLS_DIR.mkdir(parents=True, exist_ok=True)
                target = RVTOOLS_DIR / original_name
                reused_existing = False
                if target.exists():
                    try:
                        reused_existing = file_sha256(target) == upload_sha256(upload)
                    except (OSError, ValueError):
                        reused_existing = False

                if target.exists() and reused_existing:
                    candidate_path = str(target).replace("\\", "/")
                    candidate_info = build_source_file_info(candidate_path)
                    validate_and_select_inventory(
                        candidate_path,
                        candidate_info,
                        "VM inventory export file already exists in the rvtools catalog and was selected successfully.",
                        field_id="rvtools_upload",
                    )
                else:
                    if target.exists():
                        try:
                            upload.stream.seek(0)
                        except (OSError, ValueError):
                            pass
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        target = RVTOOLS_DIR / f"{target.stem}_{timestamp}_{uuid4().hex[:8]}{target.suffix}"
                    try:
                        upload.save(target)
                    except Exception:
                        app.logger.exception("Stage 1 inventory upload storage failed")
                        cleanup_owned_inventory_candidate(
                            target,
                            target,
                            selected_rvtools_file,
                        )
                        field_errors["rvtools_upload"] = "The inventory file could not be stored."
                        flash("Inventory upload could not be stored. Your current inventory was kept.", "rvtools_error")
                    else:
                        candidate_path = str(target).replace("\\", "/")
                        candidate_info = build_source_file_info(candidate_path)
                        validate_and_select_inventory(
                            candidate_path,
                            candidate_info,
                            "VM inventory export file uploaded, selected, and validated successfully.",
                            field_id="rvtools_upload",
                            owned_candidate_path=candidate_path,
                        )

        elif action == "create_manual_inventory":
            inventory_mode = "manual"
            is_update = is_manual_inventory_path(selected_rvtools_file)
            manual_sizing_values = request.form.to_dict()
            try:
                manual_path, _generated_names = create_manual_inventory_csv_from_form()
            except SetupFieldError as exc:
                field_errors[exc.field_id] = str(exc)
                flash(str(exc), "rvtools_error")
            except Exception:
                app.logger.exception("Stage 1 manual inventory generation failed")
                field_errors["manual_vm_count"] = "The manual summary could not be created."
                flash("Manual workload summary could not be created. Your current inventory was kept.", "rvtools_error")
            else:
                action_word = "updated" if is_update else "created"
                manual_candidate_path = str(manual_path).replace("\\", "/")
                validate_and_select_inventory(
                    manual_candidate_path,
                    build_source_file_info(manual_candidate_path),
                    f"Manual workload summary {action_word}.",
                    field_id="manual_vm_count",
                    owned_candidate_path=manual_candidate_path,
                    select_all_rows=True,
                )

        elif action == "select_pricelist":
            chosen_price_file = resolve_catalog_selection(
                request.form.get("price_list_file", ""),
                price_list_options,
            )
            if not chosen_price_file:
                field_errors["price_list_file"] = "Select an available OCI price list."
                flash("Please select an OCI price list file.", "pricing_error")
            else:
                refreshed_lists = list_downloaded_price_lists()
                if chosen_price_file not in refreshed_lists:
                    field_errors["price_list_file"] = "The selected OCI price list is no longer available."
                    flash("Selected OCI price list file is not available anymore.", "pricing_error")
                else:
                    try:
                        _, chosen_currency, _ = load_price_lookup(chosen_price_file)
                        remember_price_list_selection(chosen_price_file, chosen_currency)
                    except Exception:
                        app.logger.exception("Stage 1 local pricing selection failed")
                        field_errors["price_list_file"] = "The selected OCI price list could not be used."
                        flash("The selected OCI price list could not be used. Try another local price list.", "pricing_error")
                    else:
                        session["selected_pricelist_file"] = chosen_price_file
                        if chosen_currency:
                            selected_currency = chosen_currency.upper().strip()
                            session["selected_currency"] = selected_currency
                        flash("OCI price list file selected.", "pricing_success")

        downloaded_price_lists = list_downloaded_price_lists()
        rvtools_files = list_rvtools_export_files()
        customer_name = normalize_customer_name(session.get("customer_name", ""))
        selected_rvtools_file = str(session.get("selected_rvtools_file", ""))
        rvtools_file_info = session.get("rvtools_file_info")
        rvtools_import_summary = session.get("rvtools_import_summary")
        if not field_errors or action not in {
            "upload_rvtools_file",
            "select_rvtools_file",
            "create_manual_inventory",
        }:
            rvtools_rejected_info = session.get("rvtools_rejected_info")
        selected_currency = str(session.get("selected_currency", "")).upper().strip()
        active_assessment_id = _clean_assessment_id(session.get("active_assessment_id", ""))
        active_assessment_name = normalize_assessment_name(session.get("active_assessment_name", ""))
        active_assessment_notes = normalize_assessment_notes(session.get("active_assessment_notes", ""))
        selected_pricelist_file = str(session.get("selected_pricelist_file", "")).strip().replace("\\", "/")
        if selected_pricelist_file and selected_pricelist_file not in downloaded_price_lists:
            selected_pricelist_file = ""
            session.pop("selected_pricelist_file", None)

        price_list_options = downloaded_price_lists[:MAX_VISIBLE_PRICE_LISTS]

        selected_pricelist_info = None
        if selected_pricelist_file:
            price_lookup_preview, selected_pricing_currency, source_file = load_price_lookup(selected_pricelist_file)
            if source_file:
                selected_pricelist_info = {
                    **build_source_file_info(source_file),
                    "currency": selected_pricing_currency or "Unknown",
                    "item_count": len(price_lookup_preview),
                }

    return render_index_response()


@app.route("/step3", methods=["GET", "POST"])
def step3() -> str:
    _cleanup_legacy_session_keys()

    selected_rvtools_file = str(session.get("selected_rvtools_file", ""))
    if not selected_rvtools_file:
        flash("Select a VM inventory export in Step 1 to continue.", "rvtools_info")
        return redirect(url_for("index"))

    try:
        all_vms, source_vinfo_csv = load_vms_from_vinfo(selected_rvtools_file)
    except Exception as exc:
        flash(f"Could not load VM inventory data: {exc}", "rvtools_error")
        return redirect(url_for("index"))

    vm_index = {str(vm["name"]): vm for vm in all_vms}
    app_state = load_app_state()
    selected_vm_names = app_state.get("selected_vm_names", [])
    if not isinstance(selected_vm_names, list):
        selected_vm_names = []
    selected_vm_names = [n for n in selected_vm_names if n in vm_index]
    supported_signatures = load_supported_os_signatures()
    inventory_issues = build_inventory_review_issues(all_vms)
    advisory_issue_ids = [
        str(issue["id"])
        for issue in inventory_issues
        if issue.get("severity") == "advisory"
    ]
    critical_issues = [issue for issue in inventory_issues if issue.get("severity") == "critical"]
    inventory_errors: list[str] = []
    placement_errors: dict[str, str] = {}

    placement_field_names = {
        vm_name: inventory_placement_field_name("placement", vm_name)
        for vm_name in vm_index
    }

    if request.method == "POST":
        action = str(request.form.get("action", ""))
        redirect_to = str(request.form.get("redirect_to", "")).strip()
        if action == "save_inventory_review":
            submitted_names = request.form.getlist("included_vm_names")
            submitted_name_set = set(submitted_names)
            invalid_names = sorted({name for name in submitted_names if name not in vm_index})
            candidate_names = [str(vm["name"]) for vm in all_vms if str(vm["name"]) in submitted_name_set]

            if invalid_names:
                inventory_errors.append(
                    "Some submitted VMs are no longer present in the current inventory. Review the refreshed list."
                )
            if len(submitted_names) != len(submitted_name_set):
                inventory_errors.append("The submitted inventory contains duplicate VM selections.")
            if not candidate_names:
                inventory_errors.append("Include at least one VM before saving Inventory Review.")
            candidate_placements, keyed_errors, placement_errors = parse_exact_placement_fields(
                request.form,
                "placement",
                candidate_names,
                list(vm_index),
                {
                    vm_name: default_inventory_placement(vm_index[vm_name], supported_signatures)
                    for vm_name in candidate_names
                },
            )
            inventory_errors.extend(keyed_errors)

            submitted_acknowledgments = set(request.form.getlist("acknowledged_warning_ids"))
            acknowledged_warning_ids = [
                issue_id
                for issue_id in advisory_issue_ids
                if issue_id in submitted_acknowledgments
            ]
            candidate_state = copy.deepcopy(app_state)
            candidate_state["selected_vm_names"] = candidate_names
            candidate_state["step4_hybrid_placements"] = candidate_placements
            candidate_state["acknowledged_warning_ids"] = acknowledged_warning_ids
            continue_to_scenarios = request.form.get("continue_to_scenarios") == "1"
            if not inventory_errors:
                try:
                    save_app_state(candidate_state)
                except Exception:
                    app.logger.exception("Inventory Review persistence failed")
                    inventory_errors.append(
                        "Inventory Review could not be saved. Your previous selections were preserved."
                    )
                else:
                    app_state = candidate_state
                    selected_vm_names = candidate_names
                    if continue_to_scenarios:
                        readiness_errors = inventory_review_readiness_errors(
                            all_vms,
                            candidate_state,
                            inventory_issues,
                        )
                        if readiness_errors:
                            inventory_errors.extend(readiness_errors)
                        else:
                            return redirect(url_for("step4", tab="native"))
        else:
            chosen_vm_names = request.form.getlist("vm_names")
            single_vm_name = str(request.form.get("vm_name") or "").strip()
            if single_vm_name:
                chosen_vm_names = [single_vm_name]

            legacy_action_handled = action in {"add", "remove", "remove_duplicates"}
            if action == "add":
                selected_set = set(selected_vm_names)
                selected_set.update(name for name in chosen_vm_names if name in vm_index)
                selected_vm_names = [str(vm["name"]) for vm in all_vms if str(vm["name"]) in selected_set]
            elif action == "remove":
                removed_names = set(chosen_vm_names)
                selected_vm_names = [name for name in selected_vm_names if name not in removed_names]
            elif action == "remove_unsupported":
                flash(
                    "The remove unsupported action is no longer supported. Use the inventory inclusion controls instead.",
                    "error",
                )
            elif action == "remove_duplicates":
                before_count = len(selected_vm_names)
                selected_set_for_dedupe = set(selected_vm_names)
                deduped_names: list[str] = []
                seen_source_names: set[str] = set()
                for vm in all_vms:
                    vm_name = str(vm.get("name") or "").strip()
                    if vm_name not in selected_set_for_dedupe:
                        continue
                    source_name = str(vm.get("source_name") or vm_name).strip()
                    if source_name in seen_source_names:
                        continue
                    seen_source_names.add(source_name)
                    deduped_names.append(vm_name)

                selected_vm_names = deduped_names
                removed_count = before_count - len(selected_vm_names)
                if removed_count:
                    flash(
                        f"Removed {removed_count:,} duplicate VM name row(s) from the selected workload. First occurrence was kept.",
                        "success",
                    )
                else:
                    flash("No duplicate VM names were found in the selected workload.", "info")

            if legacy_action_handled:
                existing_placements = app_state.get("step4_hybrid_placements", {})
                if not isinstance(existing_placements, dict):
                    existing_placements = {}
                candidate_placements = {}
                for vm_name in selected_vm_names:
                    saved_placement = str(existing_placements.get(vm_name, "")).strip().lower()
                    candidate_placements[vm_name] = (
                        saved_placement
                        if saved_placement in HYBRID_PLACEMENT_VALUES
                        else default_inventory_placement(vm_index[vm_name], supported_signatures)
                    )
                current_acknowledgments = app_state.get("acknowledged_warning_ids", [])
                acknowledged_set = set(current_acknowledgments) if isinstance(current_acknowledgments, list) else set()
                candidate_state = copy.deepcopy(app_state)
                candidate_state["selected_vm_names"] = selected_vm_names
                candidate_state["step4_hybrid_placements"] = candidate_placements
                candidate_state["acknowledged_warning_ids"] = [
                    issue_id for issue_id in advisory_issue_ids if issue_id in acknowledged_set
                ]
                try:
                    save_app_state(candidate_state)
                except Exception:
                    app.logger.exception("Legacy Inventory Review action persistence failed")
                    inventory_errors.append(
                        "Inventory Review could not be saved. Your previous selections were preserved."
                    )
                    selected_vm_names = [
                        name for name in app_state.get("selected_vm_names", []) if name in vm_index
                    ]
                else:
                    app_state = candidate_state
                    if redirect_to == "step4":
                        readiness_errors = inventory_review_readiness_errors(
                            all_vms,
                            candidate_state,
                            inventory_issues,
                        )
                        if readiness_errors:
                            inventory_errors.extend(readiness_errors)
                        else:
                            return redirect(step4_tab_redirect("paths"))

    selected_set = set(selected_vm_names)
    saved_placements = app_state.get("step4_hybrid_placements", {})
    if not isinstance(saved_placements, dict):
        saved_placements = {}
    acknowledged_warning_ids = [
        warning_id
        for warning_id in app_state.get("acknowledged_warning_ids", [])
        if warning_id in advisory_issue_ids
    ]
    issues_by_vm: dict[str, list[dict[str, Any]]] = {}
    warning_details_by_vm: dict[str, list[dict[str, str]]] = {}
    for issue in inventory_issues:
        rows_by_name = issue.get("vm_rows_by_name", {})
        if not isinstance(rows_by_name, dict):
            rows_by_name = {}
        for vm_name in issue.get("vm_names", []):
            vm_name_key = str(vm_name)
            issues_by_vm.setdefault(vm_name_key, []).append(issue)
            note_row = rows_by_name.get(vm_name_key, {})
            if not isinstance(note_row, dict):
                note_row = {}
            warning_details_by_vm.setdefault(vm_name_key, []).append(
                {
                    "id": str(issue.get("id", "")),
                    "title": str(issue.get("title", "Inventory note")),
                    "severity": str(issue.get("severity", "advisory")),
                    "detail": str(issue.get("detail", "")),
                    "detected_value": str(note_row.get("detected_value") or "Not provided"),
                    "reason": str(
                        note_row.get("reason")
                        or note_row.get("issue")
                        or issue.get("detail")
                        or "Review required"
                    ),
                    "recommendation": str(
                        note_row.get("recommendation")
                        or issue.get("default_action")
                        or "Review this VM before final placement."
                    ),
                }
            )

    inventory_rows: list[dict[str, Any]] = []
    supported_count = 0
    review_vm_names: set[str] = set()
    for row_index, vm in enumerate(all_vms):
        vm_name = str(vm["name"])
        raw_os = str(vm.get("raw_os") or "Unknown / Empty")
        if _is_unknown_os(raw_os) or not supported_signatures:
            support_state = "review"
            support_label = "Review"
        elif is_oci_supported_os(raw_os, supported_signatures):
            support_state = "supported"
            support_label = "Supported"
            supported_count += 1
        else:
            support_state = "unsupported"
            support_label = "Requires remediation"

        row_issues = issues_by_vm.get(vm_name, [])
        if row_issues:
            review_vm_names.add(vm_name)
        placement = str(saved_placements.get(vm_name, "")).strip().lower()
        if placement not in {"native", "ocvs", "review"}:
            placement = default_inventory_placement(vm, supported_signatures)
        if vm_name in placement_errors:
            placement = default_inventory_placement(vm, supported_signatures)
        power_state = str(vm.get("power_state") or "Unknown")
        power_key = power_state.strip().lower().replace("powered", "")
        if power_key not in {"on", "off"}:
            power_key = "unknown"
        memory_mb = _to_number(vm.get("memory_mb"))
        storage_mib = _to_number(vm.get("provisioned_mib"))
        inventory_rows.append(
            {
                **vm,
                "row_index": row_index,
                "included": vm_name in selected_set,
                "support_state": support_state,
                "support_label": support_label,
                "placement": placement,
                "placement_label": HYBRID_PLACEMENT_LABELS.get(placement, "Review"),
                "placement_field_name": placement_field_names[vm_name],
                "warning_ids": " ".join(str(issue["id"]) for issue in row_issues),
                "warning_titles": [str(issue["title"]) for issue in row_issues],
                "warning_details": warning_details_by_vm.get(vm_name, []),
                "power_key": power_key,
                "memory_gb": memory_mb / 1024.0,
                "storage_gb": storage_mib / 1024.0,
                "placement_error": placement_errors.get(vm_name, ""),
            }
        )

    total_memory_mb = int(sum(_to_number(vm.get("memory_mb")) for vm in all_vms))
    total_storage_mib = int(sum(_to_number(vm.get("provisioned_mib")) for vm in all_vms))
    inventory_summary = {
        "vm_count": len(all_vms),
        "total_vcpus": int(sum(_to_number(vm.get("cpus")) for vm in all_vms)),
        "total_memory": format_total_memory_gb_or_tb(total_memory_mb),
        "total_storage_gb": int(math.ceil(total_storage_mib / 1024.0)) if total_storage_mib else 0,
        "powered_on_count": sum(1 for row in inventory_rows if row["power_key"] == "on"),
        "native_supported_count": supported_count,
        "review_count": len(review_vm_names),
    }
    readiness = build_current_readiness_context(
        inventory_rows=all_vms,
        selected_vm_names=selected_vm_names,
        scenario_analysis=None,
        scenario_views=None,
        app_state=app_state,
        setup_metadata={
            "assessment_name": normalize_assessment_name(
                session.get("active_assessment_name", "")
            ),
            "customer_name": normalize_customer_name(
                session.get("customer_name", "")
            ),
            "has_price_list": bool(
                str(session.get("selected_pricelist_file", "")).strip()
            ),
            "has_inventory": bool(all_vms),
        },
        has_unsaved_scenario_changes=False,
        inventory_issues=inventory_issues,
        pricing_inputs=None,
    )

    return render_template(
        "step3.html",
        **build_workspace_context(
            "inventory",
            readiness=readiness,
            selected_rvtools_file=selected_rvtools_file,
            source_vinfo_csv=source_vinfo_csv,
            inventory_rows=inventory_rows,
            inventory_summary=inventory_summary,
            inventory_issues=inventory_issues,
            inventory_errors=inventory_errors,
            acknowledged_warning_ids=acknowledged_warning_ids,
            selected_vm_count=len(selected_vm_names),
            critical_issue_count=len(critical_issues),
            advisory_issue_count=len(advisory_issue_ids),
            workspace_continue_form_id="continue_step4_form",
            workspace_continue_submit_name="continue_to_scenarios",
            workspace_continue_submit_value="1",
            workspace_continue_label="Save & Continue",
        ),
    )


@app.route("/step4", methods=["GET", "POST"])
def step4() -> str:
    _cleanup_legacy_session_keys()
    has_unsaved_scenario_changes = False
    requested_scenario = normalize_step4_scenario_tab(
        request.args.get("tab", "native"),
        "native",
    )
    if request.method == "GET" and requested_scenario == "paths":
        return redirect(url_for("step3"))

    selected_rvtools_file = str(session.get("selected_rvtools_file", ""))
    customer_name = normalize_customer_name(session.get("customer_name", ""))
    if not selected_rvtools_file:
        flash("Select a VM inventory export in Step 1 to continue.", "rvtools_info")
        return redirect(url_for("index"))

    try:
        all_vms, source_vinfo_csv = load_vms_from_vinfo(selected_rvtools_file)
    except Exception as exc:
        flash(f"Could not load VM inventory data: {exc}", "rvtools_error")
        return redirect(url_for("index"))

    vm_index = {vm["name"]: vm for vm in all_vms}
    app_state = load_app_state()
    persisted_app_state = copy.deepcopy(app_state)
    selected_vm_names = app_state.get("selected_vm_names", [])
    if not isinstance(selected_vm_names, list):
        selected_vm_names = []
    inventory_issues = build_inventory_review_issues(all_vms)
    boundary_errors = inventory_review_readiness_errors(
        all_vms,
        app_state,
        inventory_issues,
    )
    if boundary_errors:
        flash(
            "Complete Inventory Review before opening scenarios. " + " ".join(boundary_errors),
            "error",
        )
        return redirect(url_for("step3"))

    if (
        request.method == "POST"
        and "save_recommendation" in request.form.getlist("action")
    ):
        recommendation_input, recommendation_errors = parse_recommendation_submission(
            request.form
        )
        if recommendation_errors:
            flash(
                f"{recommendation_errors[0]} The prior recommendation was kept.",
                "error",
            )
            return redirect(step4_tab_redirect("price")), 303

        staged_state = copy.deepcopy(app_state)
        staged_state["assessor_recommendation"] = recommendation_input[
            "recommendation"
        ]
        staged_state["assessor_recommendation_rationale"] = recommendation_input[
            "recommendation_rationale"
        ]
        try:
            save_app_state(staged_state)
        except Exception:
            app.logger.exception("Specialist decision persistence failed")
            try:
                _write_json_atomically(_state_file_path(), persisted_app_state)
            except Exception:
                app.logger.exception("Specialist decision rollback failed")
            flash(
                "The decision could not be saved. The prior decision was kept.",
                "error",
            )
            return redirect(step4_tab_redirect("price")), 303

        flash("Decision saved.", "success")
        return redirect(step4_tab_redirect("price")), 303

    submitted_hybrid_placements: dict[str, str] | None = None
    submitted_step4_scalars: dict[str, Any] = {}
    if request.method == "POST":
        submitted_step4_scalars, scalar_errors = parse_step4_scalar_submission(
            request.form
        )
        posted_scenario = str(
            submitted_step4_scalars.get("active_scenario", "native")
        )
        if scalar_errors:
            session[STEP4_UNSAVED_READINESS_SESSION_KEY] = True
            flash(
                f"{scalar_errors[0]} No scenario settings were saved.",
                "error",
            )
            return redirect(step4_tab_redirect(posted_scenario, **request.form))
        has_hybrid_fields = any(
            str(key).startswith("hybrid_placement:") for key in request.form.keys()
        )
        hybrid_field_errors: list[str] = []
        if posted_scenario == "hybrid" or has_hybrid_fields:
            submitted_hybrid_placements, hybrid_field_errors, _field_errors = parse_exact_placement_fields(
                request.form,
                "hybrid_placement",
                selected_vm_names,
                list(vm_index),
            )
        if "hybrid_vm_name" in request.form or "hybrid_placement" in request.form:
            hybrid_field_errors.append("Legacy positional Hybrid placement fields are not accepted.")
        if hybrid_field_errors:
            active_tab = normalize_step4_scenario_tab(
                request.form.get("active_scenario", "native"),
                "native",
            )
            session[STEP4_UNSAVED_READINESS_SESSION_KEY] = True
            flash(
                "Choose a valid placement for every included VM. No scenario settings were saved.",
                "error",
            )
            return redirect(step4_tab_redirect(active_tab, **request.form))

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

    pricing_unit_prices = resolve_pricing_unit_prices(price_lookup)
    block_storage_unit_price = pricing_unit_prices["block_storage_unit_price"]
    block_perf_unit_price = pricing_unit_prices["block_perf_unit_price"]
    windows_os_unit_price = pricing_unit_prices["windows_os_unit_price"]

    valid_shape_values = set(shape_options)
    vpu_options = VPU_OPTIONS
    valid_vpu_values = set(vpu_options)
    valid_burst_values = VALID_BURST_VALUES
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
    hybrid_placement_selection = app_state.get("step4_hybrid_placements", {})
    if not isinstance(hybrid_placement_selection, dict):
        hybrid_placement_selection = {}
    try:
        iaas_discount_pct = float(app_state.get("step4_iaas_discount_pct", 0.0))
    except (TypeError, ValueError):
        iaas_discount_pct = 0.0
    iaas_discount_pct = max(0.0, min(100.0, iaas_discount_pct))
    ocvs_profile_choice = normalize_ocvs_profile(app_state.get("step4_ocvs_profile", "best_fit"))
    ocvs_policy = normalize_ocvs_policy(app_state.get("step4_ocvs_policy", {}))
    ocvs_commitment_term = normalize_ocvs_commitment_term(app_state.get("step4_ocvs_commitment_term", "payg"))
    vmware_license_price_per_core_yearly = _bounded_float(
        app_state.get("step4_vmware_license_price_per_core_yearly"),
        0.0,
        0.0,
        1_000_000.0,
    )
    ocvs_dr_nodes = normalize_ocvs_dr_nodes(app_state.get("step4_ocvs_dr_nodes", 0))
    hybrid_ocvs_assumptions = effective_hybrid_ocvs_assumptions(
        app_state,
        ocvs_profile_choice=ocvs_profile_choice,
        ocvs_policy=ocvs_policy,
        ocvs_commitment_term=ocvs_commitment_term,
        vmware_license_price_per_core_yearly=vmware_license_price_per_core_yearly,
        ocvs_dr_nodes=ocvs_dr_nodes,
    )
    hybrid_ocvs_customized = bool(hybrid_ocvs_assumptions["customized"])
    hybrid_ocvs_profile_choice = str(hybrid_ocvs_assumptions["profile_choice"])
    hybrid_ocvs_policy = dict(hybrid_ocvs_assumptions["policy"])
    hybrid_ocvs_commitment_term = str(hybrid_ocvs_assumptions["commitment_term"])
    hybrid_vmware_license_price_per_core_yearly = float(
        hybrid_ocvs_assumptions["vmware_license_price_per_core_yearly"]
    )
    hybrid_ocvs_dr_nodes = int(hybrid_ocvs_assumptions["dr_nodes"])

    # Restore last saved Step 4 sizing/costing settings. Step 3 remains the
    # source of truth for which VMs are selected.
    snapshot = load_step4_snapshot()
    persisted_step4_snapshot = copy.deepcopy(snapshot)
    snapshot_source = str(snapshot.get("source_vinfo_csv", ""))
    snapshot_settings = snapshot.get("vm_settings", {}) if isinstance(snapshot.get("vm_settings", {}), dict) else {}
    if snapshot_settings and snapshot_source == source_vinfo_csv:
        restored_shapes = dict(vm_shape_selection)
        restored_ocpus = dict(vm_ocpu_selection)
        restored_bursts = dict(vm_burst_selection)
        restored_vpus = dict(vm_vpu_selection)
        restored_license = dict(vm_os_license_selection)
        restored_ocvs_profile = normalize_ocvs_profile(snapshot.get("ocvs_profile", ocvs_profile_choice))
        restored_ocvs_policy = normalize_ocvs_policy(snapshot.get("ocvs_policy", ocvs_policy))
        restored_ocvs_commitment_term = normalize_ocvs_commitment_term(
            snapshot.get("ocvs_commitment_term", ocvs_commitment_term)
        )
        restored_vmware_license_price = _bounded_float(
            snapshot.get("vmware_license_price_per_core_yearly", vmware_license_price_per_core_yearly),
            vmware_license_price_per_core_yearly,
            0.0,
            1_000_000.0,
        )
        restored_ocvs_dr_nodes = normalize_ocvs_dr_nodes(snapshot.get("ocvs_dr_nodes", ocvs_dr_nodes))
        restored_hybrid_ocvs_customized = snapshot.get("hybrid_ocvs_customized") is True
        restored_hybrid_ocvs_profile = normalize_ocvs_profile(
            snapshot.get("hybrid_ocvs_profile", hybrid_ocvs_profile_choice)
        )
        restored_hybrid_ocvs_policy = normalize_ocvs_policy(
            snapshot.get("hybrid_ocvs_policy", hybrid_ocvs_policy)
        )
        restored_hybrid_ocvs_commitment_term = normalize_ocvs_commitment_term(
            snapshot.get("hybrid_ocvs_commitment_term", hybrid_ocvs_commitment_term)
        )
        restored_hybrid_vmware_license_price = _bounded_float(
            snapshot.get(
                "hybrid_vmware_license_price_per_core_yearly",
                hybrid_vmware_license_price_per_core_yearly,
            ),
            hybrid_vmware_license_price_per_core_yearly,
            0.0,
            1_000_000.0,
        )
        restored_hybrid_ocvs_dr_nodes = normalize_ocvs_dr_nodes(
            snapshot.get("hybrid_ocvs_dr_nodes", hybrid_ocvs_dr_nodes)
        )

        for vm_name, cfg in snapshot_settings.items():
            if vm_name not in vm_index or not isinstance(cfg, dict):
                continue

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

        vm_shape_selection = restored_shapes
        vm_ocpu_selection = restored_ocpus
        vm_burst_selection = restored_bursts
        vm_vpu_selection = restored_vpus
        vm_os_license_selection = restored_license
        ocvs_profile_choice = restored_ocvs_profile
        ocvs_policy = restored_ocvs_policy
        ocvs_commitment_term = restored_ocvs_commitment_term
        vmware_license_price_per_core_yearly = restored_vmware_license_price
        ocvs_dr_nodes = restored_ocvs_dr_nodes
        hybrid_ocvs_customized = restored_hybrid_ocvs_customized
        hybrid_ocvs_profile_choice = restored_hybrid_ocvs_profile
        hybrid_ocvs_policy = restored_hybrid_ocvs_policy
        hybrid_ocvs_commitment_term = restored_hybrid_ocvs_commitment_term
        hybrid_vmware_license_price_per_core_yearly = restored_hybrid_vmware_license_price
        hybrid_ocvs_dr_nodes = restored_hybrid_ocvs_dr_nodes

        app_state["step4_vm_shapes"] = vm_shape_selection
        app_state["step4_vm_ocpus"] = vm_ocpu_selection
        app_state["step4_vm_bursts"] = vm_burst_selection
        app_state["step4_vm_vpus"] = vm_vpu_selection
        app_state["step4_vm_os_license"] = vm_os_license_selection
        app_state["step4_ocvs_profile"] = ocvs_profile_choice
        app_state["step4_ocvs_policy"] = ocvs_policy
        app_state["step4_ocvs_commitment_term"] = ocvs_commitment_term
        app_state["step4_vmware_license_price_per_core_yearly"] = vmware_license_price_per_core_yearly
        app_state["step4_ocvs_dr_nodes"] = ocvs_dr_nodes
        app_state["step4_hybrid_ocvs_customized"] = hybrid_ocvs_customized
        app_state["step4_hybrid_ocvs_profile"] = hybrid_ocvs_profile_choice
        app_state["step4_hybrid_ocvs_policy"] = hybrid_ocvs_policy
        app_state["step4_hybrid_ocvs_commitment_term"] = hybrid_ocvs_commitment_term
        app_state["step4_hybrid_vmware_license_price_per_core_yearly"] = hybrid_vmware_license_price_per_core_yearly
        app_state["step4_hybrid_ocvs_dr_nodes"] = hybrid_ocvs_dr_nodes
        if snapshot.get("saved_at") and not app_state.get("step4_last_updated_at"):
            app_state["step4_last_updated_at"] = str(snapshot.get("saved_at"))
        if request.method == "GET":
            save_app_state(app_state)

    selected_vms = [vm_index[name] for name in selected_vm_names if name in vm_index]
    if not selected_vms:
        flash("No VMs selected yet. Please select VMs in Step 2 first.", "error")
        return redirect(url_for("step3"))

    export_format: str | None = None
    active_scenario = requested_scenario
    native_editor_query = normalize_native_editor_query(
        request.form if request.method == "POST" else request.args
    )
    native_scope_rows = sorted(
        [
            {
                "vm_name": str(vm.get("name") or ""),
                "os_name": str(vm.get("raw_os") or "Unknown / Empty"),
                "raw_os": str(vm.get("raw_os") or ""),
            }
            for vm in selected_vms
        ],
        key=lambda row: str(row["vm_name"]).lower(),
    )
    native_editor_scope = build_native_editor_page(
        native_scope_rows,
        native_editor_query,
        load_supported_os_signatures(),
    )

    if request.method == "POST":
        action = str(submitted_step4_scalars["action"])
        active_scenario = str(submitted_step4_scalars["active_scenario"])
        continue_to_results = request.form.get("continue_to_results") == "1"
        submitted_native_settings, native_field_errors = parse_native_editor_page_fields(
            request.form,
            native_editor_scope["rows"],
            valid_shape_values,
            valid_burst_values,
            valid_vpu_values,
        )
        bulk_apply_shape = str(request.form.get("bulk_apply_oci_shape", "")).strip()
        bulk_apply_burst = str(request.form.get("bulk_apply_burst", "")).strip()
        bulk_apply_vpu_raw = str(request.form.get("bulk_apply_vpu", "")).strip()
        bulk_apply_os_license = str(request.form.get("bulk_apply_os_license", "")).strip()
        native_shape_strategy_enabled = str(request.form.get("native_shape_strategy_enabled", "")).strip() == "1"
        native_strategy_os = request.form.getlist("native_strategy_os")
        native_strategy_shapes = request.form.getlist("native_strategy_shape")
        native_strategy_bursts = request.form.getlist("native_strategy_burst")
        native_post_errors = list(native_field_errors)
        if bulk_apply_shape and bulk_apply_shape not in valid_shape_values:
            native_post_errors.append("Choose a valid bulk OCI shape.")
        if bulk_apply_burst and bulk_apply_burst not in valid_burst_values:
            native_post_errors.append("Choose a valid bulk burst setting.")
        if bulk_apply_vpu_raw:
            if not re.fullmatch(r"[0-9]+", bulk_apply_vpu_raw) or int(bulk_apply_vpu_raw) not in valid_vpu_values:
                native_post_errors.append("Choose a valid bulk VPU setting.")
        if bulk_apply_os_license and bulk_apply_os_license not in OS_LICENSE_VALUES:
            native_post_errors.append("Choose a valid bulk Windows license setting.")
        if native_shape_strategy_enabled:
            strategy_count = len(native_strategy_os)
            if (
                not strategy_count
                or len(native_strategy_shapes) != strategy_count
                or len(native_strategy_bursts) != strategy_count
                or len({str(value).strip().lower() for value in native_strategy_os}) != strategy_count
            ):
                native_post_errors.append("Default shape strategy rows are incomplete or duplicated.")
            elif any(str(value).strip() not in valid_shape_values for value in native_strategy_shapes):
                native_post_errors.append("Choose a valid shape for every default shape strategy row.")
            elif any(normalize_burst_value(value) not in valid_burst_values for value in native_strategy_bursts):
                native_post_errors.append("Choose a valid burst for every default shape strategy row.")

        scenario_setting_fields = (
            "iaas_discount_pct",
            "vmware_license_price_per_core_yearly",
            "ocvs_profile",
            "ocvs_commitment_term",
            "ocvs_dr_nodes",
            "ocvs_vcpu_per_ocpu",
            "ocvs_cpu_headroom_pct",
            "ocvs_memory_headroom_pct",
            "ocvs_storage_headroom_pct",
            "ocvs_dense_vsan_usable_pct",
            "ocvs_standard_storage_vpu",
            "hybrid_ocvs_profile",
            "hybrid_ocvs_commitment_term",
            "hybrid_ocvs_dr_nodes",
            "hybrid_ocvs_vcpu_per_ocpu",
            "hybrid_ocvs_cpu_headroom_pct",
            "hybrid_ocvs_memory_headroom_pct",
            "hybrid_ocvs_storage_headroom_pct",
            "hybrid_ocvs_dense_vsan_usable_pct",
            "hybrid_ocvs_standard_storage_vpu",
            "hybrid_vmware_license_price_per_core_yearly",
        )
        has_scenario_setting = any(
            field_name in request.form
            and str(request.form.get(field_name, "")).strip()
            for field_name in scenario_setting_fields
        )
        has_bulk_setting = any(
            str(request.form.get(field_name, "")).strip()
            for field_name in (
                "bulk_apply_oci_shape",
                "bulk_apply_burst",
                "bulk_apply_vpu",
                "bulk_apply_os_license",
            )
        )
        if action == "save" and not any(
            (
                submitted_native_settings is not None,
                has_scenario_setting,
                has_bulk_setting,
                native_shape_strategy_enabled,
                bool(submitted_hybrid_placements),
            )
        ):
            native_post_errors.append("No scenario settings were submitted.")
        if native_post_errors:
            session[STEP4_UNSAVED_READINESS_SESSION_KEY] = True
            flash(
                f"{native_post_errors[0]} No scenario settings were saved.",
                "error",
            )
            return redirect(step4_tab_redirect(active_scenario, **request.form))

        iaas_discount_pct = float(
            submitted_step4_scalars.get("iaas_discount_pct", iaas_discount_pct)
        )
        ocvs_profile_choice = str(
            submitted_step4_scalars.get("ocvs_profile", ocvs_profile_choice)
        )
        ocvs_commitment_term = str(
            submitted_step4_scalars.get(
                "ocvs_commitment_term",
                ocvs_commitment_term,
            )
        )
        ocvs_dr_nodes = int(
            submitted_step4_scalars.get("ocvs_dr_nodes", ocvs_dr_nodes)
        )
        ocvs_policy = {
            "vcpu_per_ocpu": float(
                submitted_step4_scalars.get(
                    "ocvs_vcpu_per_ocpu",
                    ocvs_policy["vcpu_per_ocpu"],
                )
            ),
            "cpu_headroom_pct": float(
                submitted_step4_scalars.get(
                    "ocvs_cpu_headroom_pct",
                    ocvs_policy["cpu_headroom_pct"],
                )
            ),
            "memory_headroom_pct": float(
                submitted_step4_scalars.get(
                    "ocvs_memory_headroom_pct",
                    ocvs_policy["memory_headroom_pct"],
                )
            ),
            "storage_headroom_pct": float(
                submitted_step4_scalars.get(
                    "ocvs_storage_headroom_pct",
                    ocvs_policy["storage_headroom_pct"],
                )
            ),
            "dense_vsan_usable_pct": float(
                submitted_step4_scalars.get(
                    "ocvs_dense_vsan_usable_pct",
                    ocvs_policy["dense_vsan_usable_pct"],
                )
            ),
            "standard_storage_vpu": int(
                submitted_step4_scalars.get(
                    "ocvs_standard_storage_vpu",
                    ocvs_policy["standard_storage_vpu"],
                )
            ),
        }
        vmware_license_price_per_core_yearly = float(
            submitted_step4_scalars.get(
                "vmware_license_price_per_core_yearly",
                vmware_license_price_per_core_yearly,
            )
        )
        has_hybrid_ocvs_submission = any(
            field_name in request.form
            for field_name in (
                "hybrid_ocvs_profile",
                "hybrid_ocvs_commitment_term",
                "hybrid_ocvs_vcpu_per_ocpu",
                "hybrid_ocvs_cpu_headroom_pct",
                "hybrid_ocvs_memory_headroom_pct",
                "hybrid_ocvs_storage_headroom_pct",
                "hybrid_ocvs_dense_vsan_usable_pct",
                "hybrid_ocvs_standard_storage_vpu",
                "hybrid_ocvs_dr_nodes",
                "hybrid_vmware_license_price_per_core_yearly",
            )
        )
        if active_scenario == "hybrid" and has_hybrid_ocvs_submission:
            hybrid_ocvs_customized = True
            hybrid_ocvs_profile_choice = str(
                submitted_step4_scalars.get(
                    "hybrid_ocvs_profile",
                    hybrid_ocvs_profile_choice,
                )
            )
            hybrid_ocvs_commitment_term = str(
                submitted_step4_scalars.get(
                    "hybrid_ocvs_commitment_term",
                    hybrid_ocvs_commitment_term,
                )
            )
            hybrid_ocvs_dr_nodes = int(
                submitted_step4_scalars.get(
                    "hybrid_ocvs_dr_nodes",
                    hybrid_ocvs_dr_nodes,
                )
            )
            hybrid_ocvs_policy = {
                "vcpu_per_ocpu": float(
                    submitted_step4_scalars.get(
                        "hybrid_ocvs_vcpu_per_ocpu",
                        hybrid_ocvs_policy["vcpu_per_ocpu"],
                    )
                ),
                "cpu_headroom_pct": float(
                    submitted_step4_scalars.get(
                        "hybrid_ocvs_cpu_headroom_pct",
                        hybrid_ocvs_policy["cpu_headroom_pct"],
                    )
                ),
                "memory_headroom_pct": float(
                    submitted_step4_scalars.get(
                        "hybrid_ocvs_memory_headroom_pct",
                        hybrid_ocvs_policy["memory_headroom_pct"],
                    )
                ),
                "storage_headroom_pct": float(
                    submitted_step4_scalars.get(
                        "hybrid_ocvs_storage_headroom_pct",
                        hybrid_ocvs_policy["storage_headroom_pct"],
                    )
                ),
                "dense_vsan_usable_pct": float(
                    submitted_step4_scalars.get(
                        "hybrid_ocvs_dense_vsan_usable_pct",
                        hybrid_ocvs_policy["dense_vsan_usable_pct"],
                    )
                ),
                "standard_storage_vpu": int(
                    submitted_step4_scalars.get(
                        "hybrid_ocvs_standard_storage_vpu",
                        hybrid_ocvs_policy["standard_storage_vpu"],
                    )
                ),
            }
            hybrid_vmware_license_price_per_core_yearly = float(
                submitted_step4_scalars.get(
                    "hybrid_vmware_license_price_per_core_yearly",
                    hybrid_vmware_license_price_per_core_yearly,
                )
            )
        if not hybrid_ocvs_customized:
            hybrid_ocvs_profile_choice = ocvs_profile_choice
            hybrid_ocvs_policy = dict(ocvs_policy)
            hybrid_ocvs_commitment_term = ocvs_commitment_term
            hybrid_vmware_license_price_per_core_yearly = vmware_license_price_per_core_yearly
            hybrid_ocvs_dr_nodes = ocvs_dr_nodes

        updated_shapes = dict(vm_shape_selection)
        updated_ocpus = dict(vm_ocpu_selection)
        updated_bursts = dict(vm_burst_selection)
        updated_vpus = dict(vm_vpu_selection)
        updated_os_license = dict(vm_os_license_selection)
        updated_hybrid_placements = dict(hybrid_placement_selection)
        if submitted_hybrid_placements is not None:
            updated_hybrid_placements.update(submitted_hybrid_placements)
        for vm_name, settings in (submitted_native_settings or {}).items():
            updated_shapes[vm_name] = str(settings["oci_shape"])
            updated_ocpus[vm_name] = int(settings["ocpu"])
            updated_bursts[vm_name] = str(settings["burst"])
            updated_vpus[vm_name] = int(settings["vpu"])
            if str(settings["os_license"]):
                updated_os_license[vm_name] = str(settings["os_license"])

        if native_shape_strategy_enabled:
            strategy_shape_by_os: dict[str, str] = {}
            strategy_burst_by_os: dict[str, str] = {}
            for os_name_raw, shape_raw, burst_raw in zip(
                native_strategy_os,
                native_strategy_shapes,
                native_strategy_bursts,
            ):
                os_key = (str(os_name_raw or "").strip() or "Unknown / Empty").lower()
                shape_val = str(shape_raw or "").strip()
                burst_val = normalize_burst_value(burst_raw)
                if os_key and shape_val in valid_shape_values:
                    strategy_shape_by_os[os_key] = shape_val
                if os_key and burst_val in valid_burst_values:
                    strategy_burst_by_os[os_key] = burst_val

            if strategy_shape_by_os or strategy_burst_by_os:
                for vm in selected_vms:
                    vm_name = str(vm.get("name") or "").strip()
                    os_key = (str(vm.get("raw_os") or "").strip() or "Unknown / Empty").lower()
                    if not vm_name:
                        continue
                    if os_key in strategy_shape_by_os:
                        updated_shapes[vm_name] = strategy_shape_by_os[os_key]
                    if os_key in strategy_burst_by_os:
                        updated_bursts[vm_name] = strategy_burst_by_os[os_key]

        if bulk_apply_shape in valid_shape_values:
            for vm in selected_vms:
                vm_name = str(vm.get("name") or "").strip()
                if vm_name:
                    updated_shapes[vm_name] = bulk_apply_shape

        if bulk_apply_burst in valid_burst_values:
            for vm in selected_vms:
                vm_name = str(vm.get("name") or "").strip()
                if vm_name:
                    updated_bursts[vm_name] = bulk_apply_burst

        try:
            bulk_apply_vpu = int(float(bulk_apply_vpu_raw)) if bulk_apply_vpu_raw else None
        except (TypeError, ValueError):
            bulk_apply_vpu = None
        if bulk_apply_vpu in valid_vpu_values:
            for vm in selected_vms:
                vm_name = str(vm.get("name") or "").strip()
                if vm_name:
                    updated_vpus[vm_name] = int(bulk_apply_vpu)

        if bulk_apply_os_license in OS_LICENSE_VALUES:
            for vm in selected_vms:
                vm_name = str(vm.get("name") or "").strip()
                raw_os = str(vm.get("raw_os") or "").lower()
                if vm_name and "windows server" in raw_os:
                    updated_os_license[vm_name] = bulk_apply_os_license

        app_state["step4_vm_shapes"] = updated_shapes
        app_state["step4_vm_ocpus"] = updated_ocpus
        app_state["step4_vm_bursts"] = updated_bursts
        app_state["step4_vm_vpus"] = updated_vpus
        app_state["step4_vm_os_license"] = updated_os_license
        app_state["step4_hybrid_placements"] = updated_hybrid_placements
        app_state["step4_iaas_discount_pct"] = iaas_discount_pct
        app_state["step4_ocvs_profile"] = ocvs_profile_choice
        app_state["step4_ocvs_policy"] = ocvs_policy
        app_state["step4_ocvs_commitment_term"] = ocvs_commitment_term
        app_state["step4_vmware_license_price_per_core_yearly"] = vmware_license_price_per_core_yearly
        app_state["step4_ocvs_dr_nodes"] = ocvs_dr_nodes
        app_state["step4_hybrid_ocvs_customized"] = hybrid_ocvs_customized
        app_state["step4_hybrid_ocvs_profile"] = hybrid_ocvs_profile_choice
        app_state["step4_hybrid_ocvs_policy"] = hybrid_ocvs_policy
        app_state["step4_hybrid_ocvs_commitment_term"] = hybrid_ocvs_commitment_term
        app_state["step4_hybrid_vmware_license_price_per_core_yearly"] = hybrid_vmware_license_price_per_core_yearly
        app_state["step4_hybrid_ocvs_dr_nodes"] = hybrid_ocvs_dr_nodes
        step4_last_updated_at = datetime.now().isoformat(timespec="seconds")
        app_state["step4_last_updated_at"] = step4_last_updated_at
        staged_step4_snapshot: dict[str, Any] | None = None
        if action == "save":
            all_vm_settings: dict[str, dict[str, Any]] = {}
            for vm in all_vms:
                vm_name = str(vm.get("name") or "").strip()
                if not vm_name:
                    continue

                cpu_val = int(_to_number(vm.get("cpus")))
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
                    "hybrid_placement": normalize_hybrid_placement(updated_hybrid_placements.get(vm_name), ""),
                }
            staged_step4_snapshot = {
                "saved_at": step4_last_updated_at,
                "source_vinfo_csv": source_vinfo_csv,
                "ocvs_profile": ocvs_profile_choice,
                "ocvs_policy": ocvs_policy,
                "ocvs_commitment_term": ocvs_commitment_term,
                "ocvs_dr_nodes": ocvs_dr_nodes,
                "vmware_license_price_per_core_yearly": vmware_license_price_per_core_yearly,
                "hybrid_ocvs_customized": hybrid_ocvs_customized,
                "hybrid_ocvs_profile": hybrid_ocvs_profile_choice,
                "hybrid_ocvs_policy": hybrid_ocvs_policy,
                "hybrid_ocvs_commitment_term": hybrid_ocvs_commitment_term,
                "hybrid_ocvs_dr_nodes": hybrid_ocvs_dr_nodes,
                "hybrid_vmware_license_price_per_core_yearly": hybrid_vmware_license_price_per_core_yearly,
                "vm_settings": all_vm_settings,
            }

        try:
            save_app_state(app_state)
            if staged_step4_snapshot is not None:
                save_step4_snapshot(staged_step4_snapshot)
        except Exception:
            app.logger.exception("Native scenario persistence failed")
            try:
                _write_json_atomically(_state_file_path(), persisted_app_state)
            except Exception:
                app.logger.exception("Native scenario app state rollback failed")
            try:
                if persisted_step4_snapshot:
                    _write_json_atomically(
                        _step4_snapshot_file_path(),
                        persisted_step4_snapshot,
                    )
                else:
                    clear_step4_snapshot()
            except Exception:
                app.logger.exception("Native scenario snapshot rollback failed")
            session[STEP4_UNSAVED_READINESS_SESSION_KEY] = True
            flash("Scenario settings could not be saved. Your prior saved settings were kept.", "error")
            return redirect(step4_tab_redirect(active_scenario, **request.form))

        session.pop(STEP4_UNSAVED_READINESS_SESSION_KEY, None)
        vm_shape_selection = updated_shapes
        vm_ocpu_selection = updated_ocpus
        vm_burst_selection = updated_bursts
        vm_vpu_selection = updated_vpus
        vm_os_license_selection = updated_os_license
        hybrid_placement_selection = updated_hybrid_placements

        if action == "export_excel":
            export_format = "excel"
        elif action == "save":
            flash("Migration path settings saved.", "success")
            if continue_to_results:
                return redirect(step4_tab_redirect("price"))
            return redirect(step4_tab_redirect(active_scenario, **request.form))

    cost_context = {
        "shape_options": shape_options,
        "shape_pricing_map": shape_pricing_map,
        "price_lookup": price_lookup,
        "block_storage_unit_price": block_storage_unit_price,
        "block_perf_unit_price": block_perf_unit_price,
        "windows_os_unit_price": windows_os_unit_price,
        "iaas_discount_pct": iaas_discount_pct,
        "vm_shape_selection": vm_shape_selection,
        "vm_ocpu_selection": vm_ocpu_selection,
        "vm_burst_selection": vm_burst_selection,
        "vm_vpu_selection": vm_vpu_selection,
        "vm_os_license_selection": vm_os_license_selection,
        "valid_shape_values": valid_shape_values,
        "valid_vpu_values": valid_vpu_values,
    }

    vm_rows: list[dict[str, Any]] = build_vm_cost_rows(selected_vms, **cost_context)
    selected_vm_set = set(selected_vm_names)
    non_selected_vms = [vm for vm in all_vms if str(vm.get("name") or "") not in selected_vm_set]
    non_selected_vm_rows: list[dict[str, Any]] = build_vm_cost_rows(non_selected_vms, **cost_context)

    vm_rows.sort(key=lambda r: str(r["vm_name"]).lower())
    non_selected_vm_rows.sort(key=lambda r: str(r["vm_name"]).lower())
    native_editor = build_native_editor_page(
        vm_rows,
        native_editor_query,
        load_supported_os_signatures(),
    )
    native_vm_input_rows = native_editor["rows"]
    native_shape_strategy_rows = build_native_shape_strategy_rows(vm_rows)

    analysis = build_price_analysis_from_rows(
        vm_rows=vm_rows,
        price_lookup=price_lookup,
        block_storage_unit_price=block_storage_unit_price,
        block_perf_unit_price=block_perf_unit_price,
        windows_os_unit_price=windows_os_unit_price,
        iaas_discount_pct=iaas_discount_pct,
        ocvs_policy=ocvs_policy,
        ocvs_profile_choice=ocvs_profile_choice,
        source_pricelist_file=source_pricelist_file,
        vmware_license_price_per_core_yearly=vmware_license_price_per_core_yearly,
        ocvs_dr_nodes=ocvs_dr_nodes,
        ocvs_commitment_term=ocvs_commitment_term,
        hybrid_ocvs_policy=hybrid_ocvs_policy,
        hybrid_ocvs_profile_choice=hybrid_ocvs_profile_choice,
        hybrid_vmware_license_price_per_core_yearly=hybrid_vmware_license_price_per_core_yearly,
        hybrid_ocvs_dr_nodes=hybrid_ocvs_dr_nodes,
        hybrid_ocvs_commitment_term=hybrid_ocvs_commitment_term,
        hybrid_placement_selection=hybrid_placement_selection,
    )
    overall = analysis["overall"]
    ocvs_price = analysis["ocvs_price"]
    hybrid_ocvs_price = analysis["hybrid_ocvs_price"]
    scenario_comparison = analysis["scenario_comparison"]
    fit_warnings = analysis["fit_warnings"]
    executive_summary = analysis["executive_summary"]
    price_comparison = analysis["price_comparison"]
    ocvs_shape_comparison = analysis["ocvs_shape_comparison"]
    vmware_license_summary = analysis["vmware_license_summary"]
    workload_summary = analysis["workload_summary"]
    scenario_view_context = {**analysis, "iaas_discount_pct": iaas_discount_pct}
    scenario_views = [
        build_scenario_view("native", scenario_view_context),
        build_scenario_view("ocvs", scenario_view_context),
        build_scenario_view("hybrid", scenario_view_context),
    ]
    step4_last_updated_at = str(app_state.get("step4_last_updated_at", "") or "")
    if not step4_last_updated_at and snapshot.get("saved_at") and snapshot_source == source_vinfo_csv:
        step4_last_updated_at = str(snapshot.get("saved_at"))
    migration_waves = build_migration_waves(
        vm_rows=vm_rows,
        supported_native_rows=analysis["supported_native_rows"],
        unsupported_ocvs_rows=analysis["unsupported_ocvs_rows"],
    )

    if request.method == "GET":
        has_unsaved_scenario_changes = bool(
            session.pop(STEP4_UNSAVED_READINESS_SESSION_KEY, False) is True
        )
    readiness = build_current_readiness_context(
        inventory_rows=all_vms,
        selected_vm_names=selected_vm_names,
        scenario_analysis=analysis,
        scenario_views=scenario_views,
        app_state=app_state,
        setup_metadata={
            "assessment_name": normalize_assessment_name(
                session.get("active_assessment_name", "")
            ),
            "customer_name": customer_name,
            "has_price_list": bool(source_pricelist_file and price_lookup),
            "has_inventory": bool(all_vms),
        },
        has_unsaved_scenario_changes=has_unsaved_scenario_changes,
        inventory_issues=inventory_issues,
        pricing_inputs={
            "source_pricelist_file": source_pricelist_file,
            "price_lookup": price_lookup,
            "modeled_vm_rows": vm_rows,
            "block_storage_unit_price": block_storage_unit_price,
            "block_perf_unit_price": block_perf_unit_price,
            "windows_os_unit_price": windows_os_unit_price,
        },
    )

    if export_format == "excel":
        generated_at = datetime.now().isoformat(timespec="seconds")
        filename = build_export_filename(customer_name, "migration_price_comparison", "xlsx")
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        export_path = EXPORTS_DIR / filename
        export_path_display = str(export_path.resolve())
        workbook_bytes = build_migration_price_workbook_xlsx(
            readiness=readiness,
            assessor_recommendation=app_state.get("assessor_recommendation", ""),
            recommendation_rationale=app_state.get(
                "assessor_recommendation_rationale", ""
            ),
            customer_name=customer_name,
            pricing_currency=pricing_currency,
            source_pricelist_file=source_pricelist_file,
            source_vinfo_csv=source_vinfo_csv,
            export_path_display=export_path_display,
            generated_at=generated_at,
            step4_last_updated_at=step4_last_updated_at,
            vm_rows=vm_rows,
            non_selected_vm_rows=non_selected_vm_rows,
            analysis=analysis,
            migration_waves=migration_waves,
            shape_price_rates=shape_price_rates,
            iaas_discount_pct=iaas_discount_pct,
            ocvs_profile_choice=ocvs_profile_choice,
            ocvs_policy=ocvs_policy,
            ocvs_commitment_term=ocvs_commitment_term,
            ocvs_dr_nodes=ocvs_dr_nodes,
            vmware_license_price_per_core_yearly=vmware_license_price_per_core_yearly,
            block_storage_unit_price=block_storage_unit_price,
            block_perf_unit_price=block_perf_unit_price,
            windows_os_unit_price=windows_os_unit_price,
        )
        export_path.write_bytes(workbook_bytes)
        session["last_export_file"] = export_path_display
        return send_file(
            export_path,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            max_age=0,
        )
    native_readiness = readiness.get("scenarios", {}).get("native", {})
    if not isinstance(native_readiness, dict):
        native_readiness = {}
    if native_readiness.get("remediation_required") is True:
        native_status_label = "Requires remediation"
        native_status_tone = "remediation"
    elif native_readiness.get("state") == "ready":
        native_status_label = "Ready"
        native_status_tone = "ready"
    elif native_readiness.get("state") == "incomplete":
        native_status_label = "Incomplete"
        native_status_tone = "incomplete"
    else:
        native_status_label = "Needs attention"
        native_status_tone = "attention"
    if has_unsaved_scenario_changes:
        native_change_summary = "Pending changes require recalculation"
    elif step4_last_updated_at:
        native_change_summary = f"Saved {step4_last_updated_at}"
    else:
        native_change_summary = "Not saved yet"
    native_header = {
        "readiness_state": str(native_readiness.get("state") or "incomplete"),
        "status_label": native_status_label,
        "status_tone": native_status_tone,
        "monthly_cost": float(overall.get("total_monthly_cost", 0.0) or 0.0),
        "workload_count": len(vm_rows),
        "capacity_outcome": (
            f"{int(overall.get('total_cpus', 0) or 0):,} vCPU / "
            f"{int(overall.get('total_memory_gb', 0) or 0):,} GB RAM / "
            f"{int(overall.get('total_provisioned_gb', 0) or 0):,} GB storage"
        ),
        "change_summary": native_change_summary,
        "remediation_count": len(native_readiness.get("affected_vm_names", [])),
    }
    ocvs_display, hybrid_display = build_scenario_configuration_display(
        analysis,
        readiness,
        ocvs_commitment_term,
        hybrid_ocvs_customized,
    )
    results = build_results_page_context(readiness, scenario_views, app_state)

    return render_template(
        "step4.html",
        **build_workspace_context(
            "results" if active_scenario == "price" else "scenarios",
            readiness=readiness,
            selected_rvtools_file=selected_rvtools_file,
            source_vinfo_csv=source_vinfo_csv,
            vm_rows=vm_rows,
            native_vm_input_rows=native_vm_input_rows,
            native_vm_input_row_limit=native_editor["page_size"],
            native_vm_input_total=native_editor["workload_count"],
            native_editor=native_editor,
            native_header=native_header,
            ocvs_display=ocvs_display,
            hybrid_display=hybrid_display,
            native_shape_strategy_rows=native_shape_strategy_rows,
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
            ocvs_price=ocvs_price,
            hybrid_ocvs_price=hybrid_ocvs_price,
            ocvs_profiles=OCVS_HOST_PROFILES,
            ocvs_profile_choice=ocvs_profile_choice,
            ocvs_commitment_options=[
                {"value": value, "label": OCVS_COMMITMENT_LABELS[value]}
                for value in ["payg", "1_year", "3_year"]
            ],
            ocvs_commitment_term=ocvs_commitment_term,
            ocvs_policy=ocvs_policy,
            ocvs_dr_nodes=ocvs_dr_nodes,
            vmware_license_price_per_core_yearly=vmware_license_price_per_core_yearly,
            hybrid_ocvs_customized=hybrid_ocvs_customized,
            hybrid_ocvs_profile_choice=hybrid_ocvs_profile_choice,
            hybrid_ocvs_commitment_term=hybrid_ocvs_commitment_term,
            hybrid_ocvs_policy=hybrid_ocvs_policy,
            hybrid_ocvs_dr_nodes=hybrid_ocvs_dr_nodes,
            hybrid_vmware_license_price_per_core_yearly=hybrid_vmware_license_price_per_core_yearly,
            scenario_comparison=scenario_comparison,
            executive_summary=executive_summary,
            fit_warnings=fit_warnings,
            price_comparison=price_comparison,
            ocvs_shape_comparison=ocvs_shape_comparison,
            vmware_license_summary=vmware_license_summary,
            workload_summary=workload_summary,
            supported_native_summary=analysis["supported_native_summary"],
            scenario_chart_rows=analysis["scenario_chart_rows"],
            scenario_views=scenario_views,
            migration_waves=migration_waves,
            hybrid_placement_plan=analysis["hybrid_placement_plan"],
            hybrid_placement_options=HYBRID_PLACEMENT_OPTIONS,
            last_export_file=session.get("last_export_file", ""),
            customer_name=customer_name,
            active_scenario=active_scenario,
            results=results,
            workspace_continue_form_id="step4-form" if active_scenario != "price" else "",
            workspace_continue_submit_name="continue_to_results",
            workspace_continue_submit_value="1",
            workspace_continue_label="Save & Continue",
        ),
    )


@app.route("/open-last-export", methods=["POST"])
def open_last_export() -> dict[str, Any] | tuple[dict[str, Any], int]:
    """Open the most recent Excel export from the local exports directory."""
    export_value = str(session.get("last_export_file", "") or "").strip()
    if not export_value:
        return {"ok": False, "message": "No Excel export is available to open yet."}, 404

    try:
        export_path = Path(export_value).expanduser().resolve()
        export_root = EXPORTS_DIR.resolve()
    except OSError:
        return {"ok": False, "message": "The export path is not valid."}, 400

    if not export_path.exists() or not export_path.is_file():
        return {"ok": False, "message": "The exported workbook is no longer available."}, 404
    if export_root != export_path.parent and export_root not in export_path.parents:
        return {"ok": False, "message": "Only files created in the local exports folder can be opened."}, 403

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(export_path)])
        elif os.name == "nt":
            os.startfile(str(export_path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(export_path)])
    except OSError as exc:
        return {"ok": False, "message": f"Could not open the workbook: {exc}"}, 500

    return {"ok": True, "path": str(export_path)}


@app.route("/scenario/<scenario_id>", methods=["GET"])
def scenario_page(scenario_id: str) -> str:
    _cleanup_legacy_session_keys()

    raw_scenario_id = str(scenario_id or "").strip().lower()
    scenario_id = normalize_step4_scenario_tab(raw_scenario_id, "")
    if scenario_id == "paths":
        return redirect(url_for("step3"))
    if scenario_id not in {"native", "ocvs", "hybrid", "price"}:
        flash("Please select a valid migration path.", "error")
        return redirect(url_for("step3"))

    return redirect(step4_tab_redirect(scenario_id))


@app.route("/step5", methods=["GET"])
def step5() -> str:
    _cleanup_legacy_session_keys()
    return redirect(step4_tab_redirect("price"))


if __name__ == "__main__":
    app.run(
        host=_first_env("MIGRATION_ASSESSMENT_HOST", "VMW2OCI_HOST") or "127.0.0.1",
        port=_env_int("MIGRATION_ASSESSMENT_PORT", "VMW2OCI_PORT", default=5000, min_value=1, max_value=65535),
        debug=_env_bool("MIGRATION_ASSESSMENT_DEBUG", "VMW2OCI_DEBUG", default=False),
    )
