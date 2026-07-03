# Portable Assessment JSON Design

## Goal

Users can export the currently open migration assessment as one self-contained JSON file, store it outside the application folder, share it with teammates, and import it into another installation without access to the original computer's inventory or price-list paths.

## Scope

This feature extends the existing local Saved Assessments workflow. It adds browser download and upload actions for a versioned portable package. It does not add cloud storage, accounts, access control, collaboration locking, or automatic synchronization.

The package contains normalized assessment data, not the original RVTools workbook or generated Excel proposal exports. A partially completed assessment may be exported as long as every required package section is present; inventory and pricing sections may be empty when the user has not configured them yet.

## User Experience

The Step 1 Saved Assessments panel contains four workflows:

- Save Current Assessment stores or updates the active assessment in the local library.
- Export Current Assessment downloads the current open state as a `.json` package. It does not require a prior local save. If the open assessment already has a local saved id, its local snapshot is updated before download.
- Import Assessment accepts one `.json` file, validates it, creates a new local saved assessment, and loads it immediately.
- Load Previous Assessment remains visible even when the library is empty. Its selector and Load/Delete buttons are disabled until a saved assessment exists.

After a successful import, the user remains on Step 1 and sees a success message containing the imported assessment name, VM count, and currency. The restored customer, inventory summary, price-list selection, notes, warnings, and next action are visible immediately.

## Package Format

The top-level JSON object uses an independent portable package schema:

- `package_type`: fixed value `vmware_to_oci_assessment`.
- `schema_version`: integer version, initially `1`.
- `exported_at`: ISO 8601 timestamp.
- `source`: optional provenance containing the source assessment id and application format version. It is informational and never reused as the imported local id.
- `assessment`: name, notes, customer name, original saved/updated timestamps, selected currency, app state, and Step 4 snapshot.
- `inventory`: normalized VM rows plus source display metadata and the import summary.
- `pricing`: the active OCI price-list document plus currency and source display metadata.

Normalized inventory rows contain the fields already consumed by the sizing engine: unique name, source name, duplicate index, power state, raw OS, mapped OS, vCPU, memory MiB, and provisioned storage MiB. Numeric values are serialized as bounded JSON numbers or normalized numeric strings compatible with the existing loader.

The pricing section embeds the selected local price-list JSON document required by `load_price_lookup()`. The import process reconstructs a generated price-list file inside `downloads/imported_assessments/` before selecting it. This preserves the same calculation behavior without relying on the sender's path.

Machine-specific inventory paths, price-list paths, and generated export paths are excluded from the portable contract. Original filenames may be retained only as sanitized display metadata.

## Export Flow

1. Read the current browser session, app state, and Step 4 snapshot.
2. Load and normalize the selected inventory when one exists.
3. Read and validate the selected OCI price-list JSON when one exists.
4. Build the portable package and serialize it with deterministic keys and indentation.
5. Return it through `send_file()` as an attachment using a safe filename derived from the assessment name and export timestamp.

If an active local saved assessment exists, export first refreshes that local snapshot from the current state. Exporting an unsaved assessment creates no local saved entry and uses the entered assessment name or the existing timestamp-based default.

Export fails without downloading a partial or corrupt file if a selected inventory or price-list dependency cannot be read. The user sees a specific error and the current assessment remains unchanged.

## Import Flow

1. Accept one `.json` upload through the existing Flask upload limit, with an additional portable-package size limit of 25 MiB.
2. Parse JSON and validate package type, exact supported schema version, required sections, field types, collection sizes, normalized VM rows, pricing content, and app-state structure.
3. Ignore any path-like values outside the supported schema and never read a filesystem path supplied by the package.
4. Generate a new local assessment id. Never overwrite an existing local assessment, even when the source id or name matches.
5. Create generated inventory and price-list files under `downloads/imported_assessments/<new-id>/` using server-generated filenames.
6. Build a normal local saved-assessment snapshot that references only those generated local files.
7. Write the local snapshot atomically after all validation and reconstruction succeeds.
8. Load the new snapshot through the existing load workflow and show the success summary.

If the imported name already exists, append ` (Imported 2)`, ` (Imported 3)`, and so on. A failed import writes no saved assessment and does not alter the current session or active assessment.

## Validation and Security

The importer treats uploaded JSON as untrusted data.

- Only `.json` files are accepted.
- The decoded value must be an object with the expected package type and supported integer schema version.
- Required sections must exist even when inventory or pricing is empty.
- VM names must be non-empty and unique after normalization.
- VM counts and numeric sizing values use explicit upper and lower bounds consistent with the existing manual and inventory loaders.
- Collection lengths and string lengths are bounded before reconstruction.
- Embedded pricing must be valid JSON in the format already accepted by the price loader.
- Imported values cannot select arbitrary filesystem paths, overwrite files, or control generated filenames.
- Temporary files are removed when validation or reconstruction fails.

Unsupported newer schema versions produce a clear message telling the user that the application must be updated. Malformed packages identify the first useful validation error without exposing stack traces.

## Components

Portable package creation and validation are implemented as focused helper functions separate from the Flask route branches. Inventory serialization/reconstruction and pricing serialization/reconstruction each have their own helpers. The existing local snapshot functions remain the authority for local save/load behavior.

The index route adds three POST actions:

- `export_assessment` returns the JSON attachment.
- `import_assessment` validates, stores, and loads an uploaded package.
- Existing `save_assessment`, `load_assessment`, and `delete_assessment` behavior remains unchanged.

## Testing

Regression coverage extends `tests/regression_check.py` with:

- Exporting the current open assessment without first saving it locally.
- Verifying the package type, schema version, metadata, inventory rows, pricing payload, selected VM state, placement state, discounts, and commitment term.
- Deleting or changing the original inventory and pricing sources, then importing the package and confirming complete restoration from embedded data.
- Confirming import creates a new local saved assessment and loads it immediately.
- Confirming duplicate names receive deterministic imported suffixes and never overwrite an existing assessment.
- Confirming malformed JSON, wrong package type, unsupported versions, missing sections, duplicate VM names, invalid numbers, and oversized input leave the current assessment and saved library unchanged.
- Confirming imported data can proceed through workload selection and pricing calculations.
- Confirming the Load Previous Assessment controls remain visible in an empty library.

Live browser verification covers the Redwood panel layout, disabled empty state, JSON download, file-input labeling, success/error messages, and responsive behavior.
