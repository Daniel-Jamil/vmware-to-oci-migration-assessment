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

_MAX_NUMBER = 1_000_000_000_000_000.0

_APP_STATE_TEXT_LIST_FIELDS = {
    "selected_vm_names",
    "acknowledged_warning_ids",
}
_APP_STATE_TEXT_MAP_FIELDS = {
    "step4_os_shapes",
    "step4_vm_shapes",
    "step4_vm_bursts",
    "step4_vm_os_license",
    "step4_hybrid_placements",
}
_APP_STATE_NUMBER_MAP_FIELDS = {
    "step4_vm_ocpus",
    "step4_vm_vpus",
}
_APP_STATE_TEXT_FIELDS = {
    "assessor_recommendation",
    "assessor_recommendation_rationale",
    "step4_ocvs_profile",
    "step4_ocvs_commitment_term",
}
_APP_STATE_NUMBER_FIELDS = {
    "step4_iaas_discount_pct",
    "step4_vmware_license_price_per_core_yearly",
    "step4_ocvs_dr_nodes",
}
_OCVS_POLICY_FIELDS = {
    "vcpu_per_ocpu",
    "cpu_headroom_pct",
    "memory_headroom_pct",
    "storage_headroom_pct",
    "dense_vsan_usable_pct",
    "standard_storage_vpu",
}
_INVENTORY_SUMMARY_NUMBER_FIELDS = {
    "vm_count",
    "total_vcpus",
    "total_memory_gb",
    "total_storage_gb",
    "unknown_power_count",
    "unknown_os_count",
    "missing_cpu_count",
    "missing_memory_count",
    "missing_storage_count",
    "duplicate_name_count",
    "duplicate_row_count",
}
_SNAPSHOT_TEXT_FIELDS = {
    "ocvs_profile",
    "ocvs_commitment_term",
}
_SNAPSHOT_NUMBER_FIELDS = {
    "ocvs_dr_nodes",
    "vmware_license_price_per_core_yearly",
}
_SNAPSHOT_VM_TEXT_FIELDS = {
    "oci_shape",
    "burst",
    "os_license",
    "hybrid_placement",
}
_SNAPSHOT_VM_NUMBER_FIELDS = {
    "ocpu",
    "vpu",
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


def _clean_text_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise PortableAssessmentError(f"{field} must be a JSON array.")
    return [
        _clean_text(item, f"{field}[{index}]")
        for index, item in enumerate(value)
    ]


def _clean_text_map(value: Any, field: str) -> dict[str, str]:
    mapping = _require_mapping(value, field)
    cleaned: dict[str, str] = {}
    for key, item in mapping.items():
        clean_key = _clean_text(key, f"{field} key")
        if clean_key:
            cleaned[clean_key] = _clean_text(item, f"{field}.{clean_key}")
    return cleaned


def _clean_number_map(value: Any, field: str) -> dict[str, int | float]:
    mapping = _require_mapping(value, field)
    cleaned: dict[str, int | float] = {}
    for key, item in mapping.items():
        clean_key = _clean_text(key, f"{field} key")
        if clean_key:
            cleaned[clean_key] = _clean_vm_number(item, f"{field}.{clean_key}")
    return cleaned


def _clean_ocvs_policy(value: Any, field: str) -> dict[str, int | float]:
    policy = _require_mapping(value, field)
    return {
        key: _clean_vm_number(policy[key], f"{field}.{key}")
        for key in _OCVS_POLICY_FIELDS
        if key in policy
    }


def _clean_app_state(value: Any) -> dict[str, Any]:
    state = _require_mapping(value, "assessment.app_state")
    cleaned: dict[str, Any] = {}
    for key in _APP_STATE_TEXT_LIST_FIELDS:
        if key in state:
            cleaned[key] = _clean_text_list(
                state[key],
                f"assessment.app_state.{key}",
            )
    for key in _APP_STATE_TEXT_MAP_FIELDS:
        if key in state:
            cleaned[key] = _clean_text_map(
                state[key],
                f"assessment.app_state.{key}",
            )
    for key in _APP_STATE_NUMBER_MAP_FIELDS:
        if key in state:
            cleaned[key] = _clean_number_map(
                state[key],
                f"assessment.app_state.{key}",
            )
    for key in _APP_STATE_TEXT_FIELDS:
        if key in state:
            cleaned[key] = _clean_text(
                state[key],
                f"assessment.app_state.{key}",
            )
    for key in _APP_STATE_NUMBER_FIELDS:
        if key in state:
            cleaned[key] = _clean_vm_number(
                state[key],
                f"assessment.app_state.{key}",
            )
    if "step4_ocvs_policy" in state:
        cleaned["step4_ocvs_policy"] = _clean_ocvs_policy(
            state["step4_ocvs_policy"],
            "assessment.app_state.step4_ocvs_policy",
        )
    if "step4_last_updated_at" in state:
        cleaned["step4_last_updated_at"] = _clean_timestamp(
            state["step4_last_updated_at"],
            "assessment.app_state.step4_last_updated_at",
        )
    return cleaned


def _clean_snapshot_vm_settings(value: Any) -> dict[str, dict[str, Any]]:
    settings = _require_mapping(value, "assessment.step4_snapshot.vm_settings")
    cleaned: dict[str, dict[str, Any]] = {}
    for vm_name, raw_config in settings.items():
        clean_name = _clean_text(
            vm_name,
            "assessment.step4_snapshot.vm_settings key",
        )
        config = _require_mapping(
            raw_config,
            f"assessment.step4_snapshot.vm_settings.{clean_name}",
        )
        clean_config: dict[str, Any] = {}
        if "selected" in config:
            if not isinstance(config["selected"], bool):
                raise PortableAssessmentError(
                    f"assessment.step4_snapshot.vm_settings.{clean_name}.selected must be a boolean."
                )
            clean_config["selected"] = config["selected"]
        for key in _SNAPSHOT_VM_TEXT_FIELDS:
            if key in config:
                clean_config[key] = _clean_text(
                    config[key],
                    f"assessment.step4_snapshot.vm_settings.{clean_name}.{key}",
                )
        for key in _SNAPSHOT_VM_NUMBER_FIELDS:
            if key in config:
                clean_config[key] = _clean_vm_number(
                    config[key],
                    f"assessment.step4_snapshot.vm_settings.{clean_name}.{key}",
                )
        if clean_name and clean_config:
            cleaned[clean_name] = clean_config
    return cleaned


def _clean_step4_snapshot(value: Any) -> dict[str, Any]:
    snapshot = _require_mapping(value, "assessment.step4_snapshot")
    cleaned: dict[str, Any] = {}
    if "saved_at" in snapshot:
        cleaned["saved_at"] = _clean_timestamp(
            snapshot["saved_at"],
            "assessment.step4_snapshot.saved_at",
        )
    for key in _SNAPSHOT_TEXT_FIELDS:
        if key in snapshot:
            cleaned[key] = _clean_text(
                snapshot[key],
                f"assessment.step4_snapshot.{key}",
            )
    for key in _SNAPSHOT_NUMBER_FIELDS:
        if key in snapshot:
            cleaned[key] = _clean_vm_number(
                snapshot[key],
                f"assessment.step4_snapshot.{key}",
            )
    if "ocvs_policy" in snapshot:
        cleaned["ocvs_policy"] = _clean_ocvs_policy(
            snapshot["ocvs_policy"],
            "assessment.step4_snapshot.ocvs_policy",
        )
    if "vm_settings" in snapshot:
        cleaned["vm_settings"] = _clean_snapshot_vm_settings(
            snapshot["vm_settings"]
        )
    return cleaned


def _clean_inventory_summary(value: Any) -> dict[str, Any]:
    summary = _require_mapping(value, "inventory.import_summary")
    cleaned: dict[str, Any] = {
        key: _clean_vm_number(summary[key], f"inventory.import_summary.{key}")
        for key in _INVENTORY_SUMMARY_NUMBER_FIELDS
        if key in summary
    }
    if "warning_messages" in summary:
        cleaned["warning_messages"] = _clean_text_list(
            summary["warning_messages"],
            "inventory.import_summary.warning_messages",
        )
    return cleaned


def _clean_pricing_document(value: Any) -> dict[str, Any]:
    document = _require_mapping(value, "pricing.document")
    cleaned: dict[str, Any] = {}
    if "lastUpdated" in document:
        cleaned["lastUpdated"] = _clean_text(
            document["lastUpdated"],
            "pricing.document.lastUpdated",
        )
    if "items" not in document:
        return cleaned
    items = document["items"]
    if not isinstance(items, list):
        raise PortableAssessmentError("pricing.document.items must be a JSON array.")
    cleaned_items: list[dict[str, Any]] = []
    for item_index, raw_item in enumerate(items):
        item = _require_mapping(raw_item, f"pricing.document.items[{item_index}]")
        clean_item: dict[str, Any] = {}
        if "displayName" in item:
            clean_item["displayName"] = _clean_text(
                item["displayName"],
                f"pricing.document.items[{item_index}].displayName",
            )
        if "currencyCodeLocalizations" in item:
            localizations = item["currencyCodeLocalizations"]
            if not isinstance(localizations, list):
                raise PortableAssessmentError(
                    f"pricing.document.items[{item_index}].currencyCodeLocalizations must be a JSON array."
                )
            clean_localizations: list[dict[str, Any]] = []
            for localization_index, raw_localization in enumerate(localizations):
                localization = _require_mapping(
                    raw_localization,
                    f"pricing.document.items[{item_index}].currencyCodeLocalizations[{localization_index}]",
                )
                clean_localization: dict[str, Any] = {}
                if "currencyCode" in localization:
                    clean_localization["currencyCode"] = _clean_currency(
                        localization["currencyCode"],
                        f"pricing.document.items[{item_index}].currencyCodeLocalizations[{localization_index}].currencyCode",
                    )
                if "prices" in localization:
                    prices = localization["prices"]
                    if not isinstance(prices, list):
                        raise PortableAssessmentError(
                            f"pricing.document.items[{item_index}].currencyCodeLocalizations[{localization_index}].prices must be a JSON array."
                        )
                    clean_prices: list[dict[str, Any]] = []
                    for price_index, raw_price in enumerate(prices):
                        price = _require_mapping(
                            raw_price,
                            f"pricing.document.items[{item_index}].currencyCodeLocalizations[{localization_index}].prices[{price_index}]",
                        )
                        clean_price: dict[str, Any] = {}
                        if "model" in price:
                            clean_price["model"] = _clean_text(
                                price["model"],
                                f"pricing.document.items[{item_index}].currencyCodeLocalizations[{localization_index}].prices[{price_index}].model",
                            )
                        if "value" in price:
                            clean_price["value"] = _clean_vm_number(
                                price["value"],
                                f"pricing.document.items[{item_index}].currencyCodeLocalizations[{localization_index}].prices[{price_index}].value",
                            )
                        clean_prices.append(clean_price)
                    clean_localization["prices"] = clean_prices
                clean_localizations.append(clean_localization)
            clean_item["currencyCodeLocalizations"] = clean_localizations
        cleaned_items.append(clean_item)
    cleaned["items"] = cleaned_items
    return cleaned


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
        "app_state": _clean_app_state(app_state),
        "step4_snapshot": _clean_step4_snapshot(step4_snapshot),
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
        "import_summary": _clean_inventory_summary(import_summary),
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
    clean_document = _clean_pricing_document(document)
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
