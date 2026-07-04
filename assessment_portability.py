from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping


PACKAGE_TYPE = "vmware_to_oci_assessment"
SCHEMA_VERSION = 1
MAX_PACKAGE_BYTES = 25 * 1024 * 1024
MAX_VM_ROWS = 100000
MAX_TEXT_LENGTH = 4000

_MAX_JSON_DEPTH = 40
_MAX_NUMBER = 1_000_000_000_000_000.0
_LOCAL_ONLY_KEYS = {
    "file_path",
    "last_export_file",
    "local_id",
    "path",
    "selected_pricelist_file",
    "selected_rvtools_file",
    "source_path",
    "source_vinfo_csv",
}


class PortableAssessmentError(ValueError):
    """Raised when a portable assessment package is invalid or unsafe."""


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PortableAssessmentError(f"{field} must be a JSON object.")
    return value


def _clean_text(value: Any, field: str, *, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise PortableAssessmentError(f"{field} must be text.")
    if len(value) > MAX_TEXT_LENGTH:
        raise PortableAssessmentError(
            f"{field} exceeds the maximum length of {MAX_TEXT_LENGTH} characters."
        )
    return value


def _clean_display_filename(value: Any, field: str) -> str:
    clean = _clean_text(value, field).replace("\\", "/").rsplit("/", 1)[-1]
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", clean).strip(" ._")
    return clean[:255]


def _is_local_only_key(key: str) -> bool:
    normalized = key.strip().lower()
    return (
        normalized in _LOCAL_ONLY_KEYS
        or normalized.endswith("_file_path")
        or normalized.endswith("_local_path")
    )


def _clean_json_value(value: Any, field: str, *, depth: int = 0) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise PortableAssessmentError(f"{field} is nested too deeply.")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _clean_text(value, field)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise PortableAssessmentError(f"{field} must contain finite numbers.")
        if value < 0:
            raise PortableAssessmentError(f"{field} cannot contain negative numbers.")
        if value > _MAX_NUMBER:
            raise PortableAssessmentError(f"{field} contains a number that is too large.")
        return value
    if isinstance(value, list):
        return [
            _clean_json_value(item, f"{field}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PortableAssessmentError(f"{field} contains a non-text key.")
            _clean_text(key, f"{field} key")
            if _is_local_only_key(key):
                continue
            cleaned[key] = _clean_json_value(
                item,
                f"{field}.{key}",
                depth=depth + 1,
            )
        return cleaned
    raise PortableAssessmentError(f"{field} contains an unsupported JSON value.")


def _clean_timestamp(value: Any, field: str, *, required: bool = False) -> str:
    clean = _clean_text(value, field).strip()
    if not clean:
        if required:
            raise PortableAssessmentError(f"{field} is required.")
        return ""
    try:
        datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortableAssessmentError(f"{field} must be an ISO 8601 timestamp.") from exc
    return clean


def _clean_currency(value: Any, field: str) -> str:
    clean = _clean_text(value, field).strip().upper()
    if clean and not re.fullmatch(r"[A-Z]{3}", clean):
        raise PortableAssessmentError(f"{field} must be a three-letter currency code.")
    return clean


def _clean_vm_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool):
        raise PortableAssessmentError(f"{field} must be a number.")
    if isinstance(value, str):
        clean = value.strip().replace(",", "")
        if not clean:
            return 0
        try:
            number = float(clean)
        except ValueError as exc:
            raise PortableAssessmentError(f"{field} must be a number.") from exc
    elif isinstance(value, (int, float)):
        number = float(value)
    elif value is None:
        return 0
    else:
        raise PortableAssessmentError(f"{field} must be a number.")
    if not math.isfinite(number):
        raise PortableAssessmentError(f"{field} must be a finite number.")
    if number < 0:
        raise PortableAssessmentError(f"{field} cannot be negative.")
    if number > _MAX_NUMBER:
        raise PortableAssessmentError(f"{field} is too large.")
    return int(number) if number.is_integer() else number


def _clean_assessment(value: Any) -> dict[str, Any]:
    assessment = _require_mapping(value, "assessment")
    app_state = assessment.get("app_state", {})
    step4_snapshot = assessment.get("step4_snapshot", {})
    _require_mapping(app_state, "assessment.app_state")
    _require_mapping(step4_snapshot, "assessment.step4_snapshot")
    return {
        "name": _clean_text(assessment.get("name", ""), "assessment.name").strip(),
        "notes": _clean_text(assessment.get("notes", ""), "assessment.notes"),
        "customer_name": _clean_text(
            assessment.get("customer_name", ""),
            "assessment.customer_name",
        ).strip(),
        "saved_at": _clean_timestamp(
            assessment.get("saved_at", ""),
            "assessment.saved_at",
        ),
        "updated_at": _clean_timestamp(
            assessment.get("updated_at", ""),
            "assessment.updated_at",
        ),
        "selected_currency": _clean_currency(
            assessment.get("selected_currency", ""),
            "assessment.selected_currency",
        ),
        "app_state": _clean_json_value(app_state, "assessment.app_state"),
        "step4_snapshot": _clean_json_value(
            step4_snapshot,
            "assessment.step4_snapshot",
        ),
    }


def _clean_inventory_row(value: Any, index: int) -> dict[str, Any]:
    row = _require_mapping(value, f"inventory.rows[{index}]")
    name = _clean_text(row.get("name", ""), f"inventory.rows[{index}].name").strip()
    if not name:
        raise PortableAssessmentError(f"inventory.rows[{index}].name is required.")
    duplicate_index = _clean_vm_number(
        row.get("duplicate_index", 1),
        f"inventory.rows[{index}].duplicate_index",
    )
    if not isinstance(duplicate_index, int) or duplicate_index < 1:
        raise PortableAssessmentError(
            f"inventory.rows[{index}].duplicate_index must be a positive whole number."
        )
    return {
        "name": name,
        "source_name": _clean_text(
            row.get("source_name", name),
            f"inventory.rows[{index}].source_name",
        ).strip()
        or name,
        "duplicate_index": duplicate_index,
        "power_state": _clean_text(
            row.get("power_state", "Unknown"),
            f"inventory.rows[{index}].power_state",
        ).strip()
        or "Unknown",
        "raw_os": _clean_text(
            row.get("raw_os", ""),
            f"inventory.rows[{index}].raw_os",
        ),
        "mapped_os": _clean_text(
            row.get("mapped_os", ""),
            f"inventory.rows[{index}].mapped_os",
        ),
        "cpus": _clean_vm_number(
            row.get("cpus", 0),
            f"inventory.rows[{index}].cpus",
        ),
        "memory_mb": _clean_vm_number(
            row.get("memory_mb", 0),
            f"inventory.rows[{index}].memory_mb",
        ),
        "provisioned_mib": _clean_vm_number(
            row.get("provisioned_mib", 0),
            f"inventory.rows[{index}].provisioned_mib",
        ),
    }


def _clean_inventory(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        inventory: Mapping[str, Any] = {"rows": value}
    else:
        inventory = _require_mapping(value, "inventory")
    rows_value = inventory.get("rows", inventory.get("vm_rows", []))
    if not isinstance(rows_value, list):
        raise PortableAssessmentError("inventory.rows must be a JSON array.")
    if len(rows_value) > MAX_VM_ROWS:
        raise PortableAssessmentError(
            f"inventory contains too many VM rows; the maximum is {MAX_VM_ROWS}."
        )
    rows = [_clean_inventory_row(row, index) for index, row in enumerate(rows_value)]
    normalized_names: set[str] = set()
    for row in rows:
        normalized_name = str(row["name"]).strip().casefold()
        if normalized_name in normalized_names:
            raise PortableAssessmentError(
                "inventory VM names must be non-empty and unique after normalization."
            )
        normalized_names.add(normalized_name)
    import_summary = inventory.get("import_summary", {})
    _require_mapping(import_summary, "inventory.import_summary")
    return {
        "source_file_name": _clean_display_filename(
            inventory.get("source_file_name", ""),
            "inventory.source_file_name",
        ),
        "source_label": _clean_text(
            inventory.get("source_label", ""),
            "inventory.source_label",
        ),
        "import_summary": _clean_json_value(
            import_summary,
            "inventory.import_summary",
        ),
        "rows": rows,
    }


def _clean_pricing(value: Any) -> dict[str, Any]:
    pricing = _require_mapping(value, "pricing")
    if "document" in pricing or "payload" in pricing:
        document = pricing.get("document", pricing.get("payload", {}))
    elif "items" in pricing:
        document = pricing
    else:
        document = {}
    _require_mapping(document, "pricing.document")
    clean_document = _clean_json_value(document, "pricing.document")
    items = clean_document.get("items") if isinstance(clean_document, dict) else None
    if items is not None and not isinstance(items, list):
        raise PortableAssessmentError("pricing.document.items must be a JSON array.")
    return {
        "currency": _clean_currency(
            pricing.get("currency", ""),
            "pricing.currency",
        ),
        "source_file_name": _clean_display_filename(
            pricing.get("source_file_name", ""),
            "pricing.source_file_name",
        ),
        "document": clean_document,
    }


def _clean_source(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    source = _require_mapping(value, "source")
    cleaned: dict[str, Any] = {}
    if "assessment_id" in source:
        cleaned["assessment_id"] = _clean_text(
            source.get("assessment_id"),
            "source.assessment_id",
        )
    if "application_schema_version" in source:
        version = source.get("application_schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise PortableAssessmentError(
                "source.application_schema_version must be a non-negative integer."
            )
        cleaned["application_schema_version"] = version
    return cleaned


def validate_portable_package(package: Any) -> dict[str, Any]:
    """Validate untrusted package data and return a path-free canonical copy."""
    root = _require_mapping(package, "portable assessment package")
    if root.get("package_type") != PACKAGE_TYPE:
        raise PortableAssessmentError(
            f"Unsupported package type; expected {PACKAGE_TYPE}."
        )
    version = root.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise PortableAssessmentError("Portable assessment schema version must be an integer.")
    if version != SCHEMA_VERSION:
        raise PortableAssessmentError(
            f"Unsupported portable assessment schema version {version}; update the application to import this file."
        )
    for section in ("assessment", "inventory", "pricing"):
        if section not in root:
            raise PortableAssessmentError(f"{section} section is required.")

    canonical = {
        "package_type": PACKAGE_TYPE,
        "schema_version": SCHEMA_VERSION,
        "exported_at": _clean_timestamp(
            root.get("exported_at", ""),
            "exported_at",
            required=True,
        ),
        "source": _clean_source(root.get("source")),
        "assessment": _clean_assessment(root["assessment"]),
        "inventory": _clean_inventory(root["inventory"]),
        "pricing": _clean_pricing(root["pricing"]),
    }
    try:
        compact = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PortableAssessmentError("Portable assessment is not valid JSON data.") from exc
    if len(compact) > MAX_PACKAGE_BYTES:
        raise PortableAssessmentError(
            "Portable assessment exceeds the 25 MiB size limit."
        )
    return canonical


def build_portable_package(
    assessment: Mapping[str, Any],
    inventory: Mapping[str, Any] | list[Any],
    pricing: Mapping[str, Any],
    *,
    exported_at: str | None = None,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate a self-contained portable assessment package."""
    timestamp = exported_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    return validate_portable_package(
        {
            "package_type": PACKAGE_TYPE,
            "schema_version": SCHEMA_VERSION,
            "exported_at": timestamp,
            "source": dict(source or {}),
            "assessment": assessment,
            "inventory": inventory,
            "pricing": pricing,
        }
    )


def dumps_portable_package(package: Any) -> str:
    """Serialize a portable package as deterministic, human-readable JSON."""
    canonical = validate_portable_package(package)
    serialized = json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if len(serialized.encode("utf-8")) > MAX_PACKAGE_BYTES:
        raise PortableAssessmentError(
            "Portable assessment exceeds the 25 MiB size limit."
        )
    return serialized
