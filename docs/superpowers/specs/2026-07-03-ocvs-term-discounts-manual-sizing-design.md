# OCVS Term Discounts and Manual Sizing Design

## Scope

Implement two features in the local Flask assessment app:

- OCVS term discounts for 1-year and 3-year commercial terms, based on the selected OCVS bare metal shape.
- Manual sizing input that creates synthetic VM rows from summary totals, without requiring an uploaded inventory file.

The proposal generator is explicitly out of scope for this implementation.

## OCVS Term Discounts

Add OCVS commitment terms: pay-as-you-go, 1-year, and 3-year. The default is pay-as-you-go, which applies no OCVS term discount. The 1-year and 3-year options apply the screenshot discount matrix by OCVS shape:

| Shape | 1-Year Discount | 3-Year Discount |
| --- | ---: | ---: |
| BM.DenseIO2.52 | 35% | 45% |
| BM.DenseIO.E4.128 | 35% | 50% |
| BM.Standard3.64 | 30% | 40% |
| BM.Standard2.52 | 35% | 45% |
| BM.Standard.E4.128 | 35% | 45% |
| BM.GPU.A10.4 | 35% | 45% |
| BM.Standard.E5.192 | 35% | 50% |
| BM.DenseIO.E5.128 | 35% | 50% |
| BM.Optimized3.36 | 10% | 50% |

The app currently models a general IaaS discount. The new OCVS term discount remains separate from that discount and applies only to OCVS host compute pricing for both Full OCVS and Hybrid OCVS. Standard-shape Block Volume datastore pricing keeps using the existing IaaS discount only. VCF license cost is not discounted by OCVS infrastructure term discounts.

The selected term is persisted in app state and Step 4 snapshots. The UI shows the active term, selected-shape discount, and OCVS cost impact. Excel export includes the selected term and applied discount in the OCVS analysis, price list/assumptions, and technical details.

## Manual Sizing

Add a Manual Workload Summary form on Step 1 beside inventory upload. Inputs:

- VM count
- Total vCPU
- Total RAM GB
- Total storage GB
- OCI-supported VM count
- Unsupported/legacy VM count

The supported and unsupported counts must sum to the VM count. Resource totals must be positive. The app generates synthetic rows named `manual-vm-001`, `manual-vm-002`, and so on, writes them to `rvtools/manual/`, selects the generated CSV, resets prior assessment state, and preselects all generated VMs. Existing Step 2, Step 4, Hybrid placement, OCVS sizing, and Excel export flows then consume the synthetic inventory using the existing VM-level data model.

Synthetic rows distribute vCPU, RAM, and storage totals across VM count as evenly as possible, preserving entered totals. OS labels are generated only to drive existing compatibility behavior: supported rows model Oracle Linux, and unsupported rows model legacy or unsupported OS. Windows vs Linux is intentionally not requested for manual OCVS sizing because OCVS capacity depends on aggregate CPU, memory, storage, and shape assumptions rather than guest OS family.

When a generated manual inventory is selected, the Manual Workload Summary form is populated from that inventory. The same submit action updates the summary by generating a new manual CSV, selecting it, preselecting all generated rows, and clearing the Step 4 snapshot so sizing and pricing recalculate from the adjusted values.

## Testing

Regression coverage must verify:

- Manual sizing generates selected synthetic VMs and preserves total vCPU, RAM GB, and storage GB.
- Invalid manual sizing counts are rejected.
- OCVS 1-year and 3-year discounts reduce OCVS host costs for the selected shape while keeping pay-as-you-go unchanged.
- Hybrid OCVS uses the same term-discount logic.
- Excel export includes the selected OCVS term and discount assumptions.
