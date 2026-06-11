# VMware to OCI Migration Assessment

Local Flask app for assessing VMware workload migration paths to Oracle Cloud Infrastructure.

It compares estimated OCI infrastructure and licensing run-rate for:

- OCI Native
- Oracle Cloud VMware Solution (OCVS)
- Hybrid: OCI Native plus OCVS

This tool is for migration-path assessment and price comparison. It is not a final quote, OCI Cost Estimator replacement, or full TCO/business-case model. Final sizing and pricing must be validated with official Oracle tools before customer sign-off.

## Main Capabilities

- Import VM inventory from RVTools, VMwareInventory, or compatible Excel/CSV files.
- Select the VMs included in the assessment.
- Check OCI Native OS support.
- Estimate OCI Native compute, storage, VPU, and Windows license-included costs.
- Estimate OCVS node count, shape, datastore cost, and optional VCF license exposure.
- Model Hybrid placement between OCI Native and OCVS.
- Compare monthly, annual, and 3-year price signals.
- Export a presentation-ready Excel assessment workbook.

## Install

Use Python 3.9 or newer.

```bash
cd /path/to/vmware-to-oci-migration-assessment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell:

```powershell
cd C:\path\to\vmware-to-oci-migration-assessment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

Default:

```bash
python app.py
```

Open:

```text
http://127.0.0.1:5000/
```

Use another port if needed:

```bash
VMW2OCI_PORT=5059 python app.py
```

Then open:

```text
http://127.0.0.1:5059/
```

## Basic Workflow

1. **Setup & Inventory**
   - Enter customer name.
   - Select or download an OCI price list.
   - Select or upload inventory.

2. **VM Selection**
   - Choose VMs in scope.
   - Optionally remove duplicate VM-name rows.

3. **Migration Paths**
   - Review workload summary and migration path options.

4. **Scenario Tabs**
   - Tune OCI Native assumptions.
   - Tune OCVS assumptions.
   - Tune Hybrid placement.

5. **Price Comparison**
   - Review ranked monthly, annual, and 3-year price comparison.
   - Export the Excel report.

## Input Files

Inventory files are discovered from:

```text
rvtools/
```

Supported formats:

- `.xlsx`
- `.xlsm`
- `.csv`

Required VM-level data:

- VM name
- vCPU
- RAM
- Storage
- Operating system

A sample file is included:

```text
rvtools/example_RVTools_tabvInfo.csv
```

## Output Files

Excel reports are written to:

```text
downloads/exports/
```

The workbook includes:

- Executive Summary
- Price Comparison
- OCI Native Analysis
- OCVS Analysis
- Hybrid Analysis
- Hybrid Placement
- Selected VMs
- Non-Selected VMs
- Price List
- Technical Details

## Local Data

The app stores local working data in:

```text
downloads/
downloads/app_state/
downloads/exports/
rvtools/
```

Treat these folders as customer-sensitive after real assessments.

## Notes

- The app is currently local-only.
- Keep it bound to `127.0.0.1`.
- Do not host it for multiple users without authentication, HTTPS, access control, and deployment hardening.
- The only outbound call is the user-initiated OCI price-list download.
