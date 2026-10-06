# Lab 4 - Cost report

Retrieved: 2026-10-06 05:13:09 UTC
Usage window: 2026-09-29 to 2026-10-05 (UTC).
Query cutoff: 2026-10-05T23:59:59Z.
Scope: course resource group itcs355-u6688124; exact reviewed IDs are in cloudlayer/lab4_scope.py.
Source: Azure Cost Management `2025-03-01`, `ActualCost`, `PreTaxCost`;
daily rows grouped by `ResourceId`; all 1 response page(s) read.

| Resource | Scope | Reported USD |
|---|---|---:|
| Staging app | Lab 4 | No cost rows reported |
| Staging environment | Lab 4 | No cost rows reported |
| Drift compute | Lab 4 | No cost rows reported |
| Drift schedule | Lab 4 | No cost rows reported |
| Drift alert | Lab 4 | 0.0010752688172043 |
| Drift email channel | Lab 4 | 0 |
| ACR | Shared | 1.1523258852 |
| Log Analytics | Shared | 0 |
| Application Insights | Shared | No cost rows reported |
| Azure ML workspace | Shared | 0.1882655884699188 |
| Storage | Shared | 0.0002394 |
| Key Vault | Shared | 0.000063 |
| Existing Insights action group | Shared | No cost rows reported |
| Lab 4 Workbook (retained) | Lab 4 | No cost rows reported |
| Lab 4 ci identity | Lab 4 | No cost rows reported |
| Lab 4 push identity | Lab 4 | No cost rows reported |
| Lab 4 deploy identity | Lab 4 | No cost rows reported |
| Lab 4 runtime identity | Lab 4 | No cost rows reported |
| Lab 4 drift identity | Lab 4 | No cost rows reported |

Lab 4 resources reported subtotal: **USD 0.0010752688172043**.
Shared resources reported subtotal: **USD 1.3408938736699188**.
All reported rows in this resource group: **USD 1.3419691424871231**.

## Limits

- Shared-resource costs are shown separately, not fully assigned to Lab 4. Workspace-level AML charges cannot be separated by compute here.
  The combined amount is not a measured Lab 4-only cost.
- No cost rows reported means unknown, not zero. A reported zero only describes
  the returned rows; it does not prove that a service will always be free.
- These are reported pre-tax costs, not remaining student credit or a final bill.
  Billing rows can arrive late. The current UTC day can also be incomplete.
- We query whole UTC dates, including time before Lab 4 resources were created
  and any later shared-service usage. Refresh using the same end date.
  Retained shared services can continue to cost money outside this window.
- No retail estimates or remaining-credit balance are added. There is no THB conversion.
- Unknown resource IDs stop the report for review; they are not silently excluded.

Source: [Azure cost-data scope and timing](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/understand-cost-mgt-data).
