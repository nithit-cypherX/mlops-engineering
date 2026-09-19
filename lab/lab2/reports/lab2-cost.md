# Lab 2 - Cost report

Retrieved: 2026-09-19 13:11:50 UTC
Usage window: 2026-09-17 to 2026-09-19 (UTC; current day can be incomplete).
Scope: `/subscriptions/d9385e82-8bee-4612-8a76-967c241112c8/resourceGroups/itcs355-u6688124`.
Source: Azure Cost Management `2025-03-01`, `ActualCost`, `PreTaxCost`,
daily rows grouped by `ResourceId`; all 1 response page(s) read.

| Resource | Allocation | Reported USD | THB reference |
|---|---|---:|---:|
| Azure ML workspace | Direct | 0.08838734 | 2.9068 |
| Azure ML compute | Direct | No cost rows reported | Not assumed zero |
| Key Vault | Direct | No cost rows reported | Not assumed zero |
| Log Analytics | Direct | No cost rows reported | Not assumed zero |
| Application Insights | Direct | No cost rows reported | Not assumed zero |
| ACR | Shared | 0.37485300 | 12.3280 |
| Storage | Shared | 0.00013680 | 0.0045 |

Direct reported subtotal: **USD 0.08838734**.
Shared-resource allocation: **USD 0.37498980**.
Combined reported amount: **USD 0.46337713 / about THB 15.24**.
Lab budget: **THB 150**. This snapshot is not a final budget pass.

## What these numbers mean

- The window starts on the workspace creation day, 2026-09-17. We include
  the full day's costs, including hours before its 06:10 UTC creation.
- ACR and Storage also serve Lab 1. We allocate all their reported costs
  in this window to Lab 2 conservatively, not as measured Lab 2-only usage.
- Direct resources use the inventory checked on 2026-09-19. New billed
  resource IDs stop the report for review rather than being silently excluded.
- Costs include failed jobs and idle time where Azure charges for them.
  These rows do not separate those amounts. Trial estimates are not added again.
- USD is the reported pre-tax cost, not the remaining student credit.
  Missing rows do not prove a service is free. Billing data can arrive late;
  refresh after teardown using the same end date. Retained services can
  continue costing money after that cutoff.

## THB reference

1 USD = 32.88749433 THB for budget comparison only, not invoice FX.
Derived from Microsoft's matched Linux Standard_F2s_v2 on-demand prices in
`malaysiawest`: USD 0.0882/hour and THB 2.900677/hour, checked 2026-09-19.
Meter: `6f6004d7-c68d-5437-931c-5046eecae885`; effective date 2025-10-01.
Source: [Azure Retail Prices API](https://prices.azure.com/api/retail/prices).
Microsoft describes non-USD prices as budget references in its
[pricing documentation](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices).
