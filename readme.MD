# VMware to OCI Migration Assessment

Local Flask app for assessing VMware workload migration paths to Oracle Cloud Infrastructure.

It compares estimated OCI infrastructure and licensing run-rate for:

- OCI Native
- Oracle Cloud VMware Solution (OCVS)
- Hybrid: OCI Native plus OCVS

This tool is for migration-path assessment and price comparison. It is not a final quote, OCI Cost Estimator replacement, or full TCO/business-case model. Final sizing and pricing must be validated with official Oracle tools before customer sign-off.

## Project Origin

This application was originally based on an internal VMware-to-OCI sizing utility and has been significantly extended into a migration assessment tool covering OCI Native, OCVS, Hybrid placement, price comparison, and Excel reporting.

## Main Capabilities

- Import VM inventory from RVTools exports.
- Select the VMs included in the assessment.
- Check OCI Native OS support.
- Estimate OCI Native compute, storage, VPU, and Windows license-included costs.
- Estimate OCVS node count, shape, datastore cost, and optional VCF license exposure.
- Model Hybrid placement between OCI Native and OCVS.
- Compare monthly, annual, and 3-year price signals.
- Export a presentation-ready Excel assessment workbook.

## Quick Start

Use these steps to run the app locally on macOS or Windows.

Prerequisites:

- Python 3.9 or newer
- Git
- A terminal application:
  - macOS: Terminal
  - Windows: PowerShell

This project is intended to be available as a public GitHub repository. The app still runs locally on each user's computer at `127.0.0.1`; it is not a hosted multi-user web service.

If Git is not installed, download the repository ZIP from GitHub, unzip it, and open a terminal in the extracted folder.

### macOS

Clone the repository:

```bash
cd ~/Documents
mkdir -p Projects
cd Projects
git clone https://github.com/Daniel-Jamil/vmware-to-oci-migration-assessment.git
cd vmware-to-oci-migration-assessment
```

Create and activate a Python virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the app:

```bash
MIGRATION_ASSESSMENT_PORT=5059 python app.py
```

Open the app:

```text
http://127.0.0.1:5059/
```

To stop the app, return to Terminal and press `Ctrl + C`.

If port `5059` is already in use, choose another port:

```bash
MIGRATION_ASSESSMENT_PORT=5060 python app.py
```

Then open:

```text
http://127.0.0.1:5060/
```

### Windows

Open PowerShell, then clone the repository:

```powershell
cd $env:USERPROFILE\Documents
mkdir Projects -Force
cd Projects
git clone https://github.com/Daniel-Jamil/vmware-to-oci-migration-assessment.git
cd vmware-to-oci-migration-assessment
```

Create and activate a Python virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run this once in the same PowerShell window, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Start the app:

```powershell
$env:MIGRATION_ASSESSMENT_PORT="5059"
python app.py
```

Open the app:

```text
http://127.0.0.1:5059/
```

To stop the app, return to PowerShell and press `Ctrl + C`.

If port `5059` is already in use, choose another port:

```powershell
$env:MIGRATION_ASSESSMENT_PORT="5060"
python app.py
```

Then open:

```text
http://127.0.0.1:5060/
```

### Run From an Existing Folder

If you already downloaded or cloned the project, open a terminal in the project folder and run:

```bash
python app.py
```

Then open:

```text
http://127.0.0.1:5000/
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
