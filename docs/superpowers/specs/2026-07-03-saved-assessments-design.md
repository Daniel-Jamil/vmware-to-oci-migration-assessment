# Saved Assessments Design

## Goal

Users can save a complete migration assessment locally and reload it later to review assumptions, continue sizing, or remind themselves what was selected without rebuilding the assessment from scratch.

## Scope

The first version is a local saved-assessment library inside the existing Flask app. It does not add a database, cloud sync, sharing, or portable import/export. Those can be added later after the local workflow is proven.

## User Experience

Step 1 - Setup & Inventory gets a Saved Assessments panel in the right action rail. The panel shows an assessment name field, notes field, a Save Current Assessment button, a dropdown of previously saved assessments, and Load/Delete actions.

Saving captures the active assessment under a user-provided name. Loading restores that assessment into the current browser session and keeps the user on Step 1 so they can review customer, price list, inventory source, warnings, and next actions before continuing.

Deleting removes only the selected saved assessment file. It does not delete uploaded inventory files, generated manual inventory CSVs, price lists, exports, or app preferences.

## Data Captured

Each saved assessment is a JSON document with:

- Schema version and generated assessment id.
- Name, notes, saved timestamp, and updated timestamp.
- Customer/project name.
- Selected OCI price list path and selected currency.
- Selected inventory path, file info, and import summary.
- Current app state from `load_app_state()`, including selected VMs and Step 4 scenario assumptions.
- Current Step 4 snapshot from `load_step4_snapshot()` when present.
- Last export file path for reference only.

Manual sizing is preserved through the selected generated manual inventory CSV and the selected VM/state data. The existing manual sizing form can rebuild its totals from that CSV after load.

## Storage

Saved assessments live under `downloads/app_state/saved_assessments/`. Filenames are generated from a safe slug plus a short id, so assessment names can change without creating unsafe paths. This follows the existing local `downloads/app_state` pattern and keeps generated user data outside source control.

## Load Behavior

When a saved assessment is loaded, the app restores session fields and writes the saved app state to the active session state file. It validates referenced local files:

- If the price list still exists, it is selected and currency is restored.
- If the price list is missing, the saved value is not selected and the user sees a warning.
- If the inventory file still exists and can be parsed, it is selected and its import summary is rebuilt.
- If the inventory file is missing or invalid, the saved value is not selected and the user sees a warning.

Inventory warning review is recomputed from the loaded inventory instead of trusted from the saved JSON, so warnings reflect the actual file on disk.

## Error Handling

Corrupt saved assessment files are skipped in the list and never crash Step 1. Loading a missing or invalid assessment shows a warning and leaves the active assessment unchanged. Missing price list or inventory dependencies result in partial restore with clear flash messages. Invalid save names are replaced with a timestamp-based default.

## Testing

Regression coverage will use the existing `tests/regression_check.py` test client flow:

- Create a manual inventory assessment.
- Save it with a name and notes.
- Change the active assessment to different manual sizing values.
- Load the saved assessment and assert customer, notes, selected inventory, selected VM count, manual sizing values, and Step 4 state are restored.
- Delete the saved assessment and assert it is removed from the saved list.

