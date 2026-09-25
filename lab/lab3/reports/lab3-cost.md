# Lab 3 - Cost report

Retrieved: 2026-09-24 12:25:09 UTC
Usage window: 2026-09-21 to 2026-09-24 (UTC).
Query cutoff: 2026-09-24T12:25:09Z.
Scope: `/subscriptions/d9385e82-8bee-4612-8a76-967c241112c8/resourceGroups/itcs355-u6688124`.
Source: Azure Cost Management `2025-03-01`, `ActualCost`, `PreTaxCost`;
daily rows grouped by `ResourceId`; all 1 response page(s) read.

| Resource | Scope | Reported USD |
|---|---|---:|
| Container App | Lab 3 | No cost rows reported |
| Environment | Lab 3 | No cost rows reported |
| Managed identity | Lab 3 | No cost rows reported |
| ACR | Shared | 0.5414543316 |
| Storage | Shared | 0.00008388 |
| Log Analytics | Shared | 0 |
| Azure ML workspace | Shared | No cost rows reported |
| Key Vault | Shared | No cost rows reported |
| Application Insights | Shared | No cost rows reported |
| Action group | Shared | No cost rows reported |

Lab 3 resources reported subtotal: **No cost rows reported; not assumed zero**.
Shared resources reported subtotal: **USD 0.5415382116**.
All reported rows in this resource group: **USD 0.5415382116**.

## Limits

- Shared-resource costs are shown separately, not fully assigned to Lab 3.
  The combined amount is not a measured Lab 3-only cost.
- No cost rows reported means unknown, not zero. A reported zero only describes
  the returned rows; it does not prove that a service will always be free.
- These are reported pre-tax costs, not remaining student credit or a final bill.
  Billing rows can arrive late. The current UTC day can also be incomplete.
- We query whole UTC dates, including time before Lab 3 resources were created
  and shared-service usage after teardown. Refresh using the same end date.
  Retained shared services can continue to cost money outside this window.
- Task 5 retail estimates are not added to these amounts. There is no THB conversion.
- Unknown resource IDs stop the report for review; they are not silently excluded.

Source: [Azure cost-data scope and timing](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/understand-cost-mgt-data).
