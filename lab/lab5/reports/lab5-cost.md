# Lab 5 - Cost report

## 1. Estimate before running

Prepared on 2026-10-12, before the Lab 5 managed pipeline runs.
This is the first estimate for **Task 1 only**, not the whole lab or the term.
This section is the pre-run baseline. Record later changes to the assumptions
separately so that the original estimate stays available for comparison.

### What this estimate covers

We plan to reuse the Lab 2 model settings and Lab 4 service, with no new tuning.
The pipeline will run ingest, validate, train, evaluate, register, and staging
deployment. Only a model that passes the validation ROC-AUC gate of 0.80 can
reach registration and deployment.

The budget covers three controlled runs:

- Bad input: validation must stop the pipeline before training.
- A stricter gate in a separate test run: the measured score must fail the gate,
  with no new registration or deployment. Do not change the recorded metrics.
- Normal input and the 0.80 gate: run the full path and check the new model in staging.

These are planned checks, not completed results.

### Assumptions

- Region: Malaysia West. Use existing shared workspace, storage, and ACR if they
  are still suitable; their current state has not been checked.
- Compute: Linux `Standard_F2s_v2`, Dedicated, with a planned minimum of zero
  nodes and maximum of one node. Allow **four node-hours in total**, including
  all pipeline steps, startup, and idle time before scale-down. This is a time
  allowance, not a measured training time or four hours per run.
- Staging: Consumption, 0.5 vCPU and 1 GiB, minimum zero and maximum one replica.
  Allow two hours at the active rate and 1,000 requests within one day; assume
  zero replicas for the rest of that day.
- Registry: assume the shared ACR is Basic and allocate one day of its charge
  to this estimate. This is shared-service allocation, not a new registry or
  proof that Lab 5 caused the whole charge.
- Monitoring: allow 0.05 GB of log ingestion. No new dashboard, alert, or
  retraining schedule is included in Task 1.
- Build locally. No ACR Task build charge is included.
- Do not subtract student credits or free allowances. Their remaining amounts
  have not been checked. All amounts are pre-tax retail estimates.

Dedicated compute follows the earlier Lab 2 workaround for unavailable
low-priority quota. It is **not Spot compute** and does not meet that part of
the handout's diagram. Student credit does not turn it into discounted compute.
The proposed size and current quota still need checking before use.

### Calculation

Rates were checked against the public Azure Retail Prices API on 2026-10-12.
All rate rows below use `malaysiawest` and `Consumption` pricing.

| Component | Working | Estimated USD |
|---|---|---:|
| Training and other pipeline steps | 4 node-hours x 0.0882/hour | 0.352800 |
| Staging compute | 7,200 seconds x (0.5 x 0.000028/vCPU-second + 1 x 0.000004/GiB-second) | 0.129600 |
| Staging requests | 1,000 / 1,000,000 x 0.468 | 0.000468 |
| Shared ACR allocation | 1 day x 0.1666/day, assuming Basic | 0.166600 |
| Log ingestion | 0.05 GB x 2.691/GB, without using the free tier | 0.134550 |
| Storage, disks, and other small charges | Planning allowance, not a priced usage measurement | 0.100000 |
| **Total** | | **0.884018** |

Azure ML has no extra service fee on top of its underlying resources.
The compute row includes the machines running the pipeline steps; storage and
monitoring are not free just because they support a pipeline.
See [Azure ML pricing](https://azure.microsoft.com/en-us/pricing/details/machine-learning/).

For a rough THB budget, the matching VM rate is 2.96352 THB/hour versus
0.0882 USD/hour: `2.96352 / 0.0882 = 33.6`. Using that reference,
`0.884018 x 33.6 = 29.7030048 THB`, or **about 30 THB**.

This is a budgeting reference, not a bank exchange rate or an invoice conversion.
Microsoft describes non-USD API prices as budget references in its
[Retail Prices API documentation](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices).

**Review threshold: 50 THB for this Task 1 batch.** This is not an automatic
spending cap. Review the estimate before extra runs, longer uptime, a different
SKU, or added resources. Shared resources may keep costing money after the batch.
The estimate does not show how much remains from the 800 THB term budget.

### Rate sources and limits

- [USD rate query][usd-rates]: Linux F2s v2 is 0.0882/hour, effective 2025-10-01;
  exclude Windows, Spot, and Low Priority entries. Container Apps Standard
  active CPU, memory, and request rates are effective 2026-02-01.
- In the same query, Basic Registry Unit is 0.1666/day and Analytics Logs Data
  Ingestion is 2.691/GB above the free tier, both effective 2025-04-01.
  The Basic registry entry is marked nonprimary; confirm the actual SKU and
  billing rate before treating this allocation as an actual charge.
- [THB VM rate query][thb-rates] provides the matching 2.96352/hour reference.
- [Container Apps billing](https://learn.microsoft.com/en-us/azure/container-apps/billing)
  explains active resource and request charges, shared monthly free grants,
  and zero-replica billing. The two-hour assumption includes time while the
  replica is starting or waiting to scale down, priced at the active rate.
- The USD 0.10 allowance is deliberately rough. There is no measured storage,
  disk, transfer, or other small-service usage behind it yet. Extra networking
  features and dedicated workload profiles are outside this estimate.
- Tasks 2-9, the second provider, and LLM token use are not covered. Estimate
  their costs before using them; keep this original Task 1 estimate for comparison.

## 2. Actual cost

Not collected yet. After running, use billing for the recorded UTC usage window
and Lab 5 tags. Show shared-resource rows and any allocation separately so that
other labs' charges are not silently counted as Lab 5-only usage. Missing billing
rows are not proof of zero cost.

## 3. Estimate versus actual

Not measured yet. Compare the same Task 1 scope and time window, and explain
differences in runtime, shared allocation, free allowances, and billing timing.
Do not replace the original estimate with the actual cost.

## 4. Component breakdown

The table above is the estimated breakdown. The actual breakdown still needs
training, storage, serving, pipeline, and monitoring costs from the run and
billing evidence, without counting pipeline compute twice.

## 5. Cost per 1,000 predictions

Not measured yet. Record the serving rate and measured throughput first, then
show the working for 5%, 25%, and 80% utilisation. The 1,000-request allowance
above is not a measured cost-per-1,000 result.

## 6. Applied optimisation

No before-and-after measurement yet. Scale-to-zero is part of the planned
setup, not proof of a saving. This section needs one applied change, comparable
before-and-after costs, and a check that the service still works.

[usd-rates]: https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&currencyCode=USD&$filter=armRegionName%20eq%20%27malaysiawest%27%20and%20priceType%20eq%20%27Consumption%27%20and%20(armSkuName%20eq%20%27Standard_F2s_v2%27%20or%20serviceName%20eq%20%27Azure%20Container%20Apps%27%20or%20serviceName%20eq%20%27Container%20Registry%27%20or%20serviceName%20eq%20%27Log%20Analytics%27)
[thb-rates]: https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&currencyCode=THB&$filter=armRegionName%20eq%20%27malaysiawest%27%20and%20priceType%20eq%20%27Consumption%27%20and%20armSkuName%20eq%20%27Standard_F2s_v2%27
