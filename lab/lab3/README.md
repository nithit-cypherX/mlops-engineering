# Lab 3 Evidence

## Task 1 — Build the inference service

I reused the instructor’s FastAPI starter and connected it to `models:/itcs355-u6688124/1` through the Azure adapter. The model loads once at startup for each process, not on every request. Cloud-specific code stays in the adapter.

The API has four endpoints: `/predict`, `/predict/batch`, `/health`, and `/ready`. Pydantic checks the input and returns `422` for invalid requests. Responses include `model_version`, including error responses. Each request also produces a JSON log with its request ID, latency in milliseconds, and model version.

### Checks

Verified locally on 2026-09-21:

- With the real registered model, `/health` returned `200` while `/ready` returned `503` during loading, then changed to `200`.
- Batch predictions matched single predictions. A batch of 100 rows also passed.
- Missing fields, out-of-range values, extra fields, empty batches, and batches over 100 rows returned `422`.
- Offline tests confirmed that loading or startup scoring failures keep the service unready, and repeated requests do not reload the model.
- All **58 tests** and `make portability-audit` passed. Request IDs and model versions in the live-test logs matched the responses.

### Problems and fixes

The starter waited for model loading before accepting requests. I moved loading into a background task and added one test prediction before marking the service ready.

The original logs built JSON by inserting text directly. I changed this to JSON serialization so quotes and line breaks do not break the log format.

This check ran locally, not on a deployed endpoint. I stopped the server and removed the temporary model files afterwards.

## Task 2 — Deploy it

I deployed model version `1` to Azure Container Apps using a digest-pinned image from ACR. The app uses 0.5 vCPU, 1 GiB memory, and one worker. It uses the existing managed identity to pull the image and load the model, without adding roles or storing passwords.

I implemented `deploy()` and `invoke()` in the Azure adapter and connected them to `make deploy` and `make smoke`.

### Checks

Verified on Azure on 2026-09-21:

- `make deploy` completed successfully.
- `make smoke` passed with three fixed payloads. Each returned a probability between 0 and 1 and `model_version: "1"`.
- `/health` and `/ready` both returned `200`.
- Cloud logs recorded the prediction requests with request IDs, latency, and model version.
- All **125 tests** and `make portability-audit` passed.

These checks confirm that the deployed API can serve predictions. They are not a load test or an accuracy check.

### Problems and fixes

Azure initially treated the environment as Express and rejected the logging configuration. I explicitly selected `WorkloadProfiles` with a `Consumption` profile.

Deployment also stopped because Azure returned the region as `Malaysia West`, while my config used `malaysiawest`. I adjusted the comparison to handle spaces and letter case, then added a regression test.

### After testing

I stopped the app and confirmed its status was `Stopped`. I kept the resources needed for the next tasks. This was a temporary stop, not the final teardown.

## Task 3 — Load test honestly

### Instance size

I compared 0.5 vCPU / 1 GiB with 0.75 vCPU / 1.5 GiB. Each size ran for 60 seconds at 10 concurrent users, using the same image, model, payload, and one worker.

| Size | p95 | Requests/s | CPU/RAM cost per active hour |
|---|---:|---:|---:|
| 0.5 vCPU / 1 GiB | 418.24 ms | 33.55 | $0.0648 |
| 0.75 vCPU / 1.5 GiB | 256.53 ms | 54.65 | $0.0972 |

The larger size reduced p95 by **38.67%** and increased throughput by **62.88%**, while the CPU/RAM cost per hour increased by **50%**. These are retail-rate estimates before free allowance or credit, not the actual bill. Requests, logs, and network charges are separate.

All 5,306 requests passed the prediction checks, with no timeouts. However, **both sizes missed my p95 < 200 ms target**. Both k6 runs therefore exited with code `99`. I kept the target unchanged.

This was one short run per size, so the improvement is not guaranteed for every run. I restored the original size and stopped the app afterwards.

**Evidence note:** some controller output was cut off in the terminal. Both k6 result sets were complete, and I checked Azure again to confirm the restored configuration and stopped state.

Full results and checks: [instance-size report](reports/lab3-load.md#task-343--instance-size-measurements).

### Extra checks — latency target still not met

I did extra checks after the larger instance still missed my target of p95 < 200 ms at 10 concurrent users.

On Azure, I measured time spent inside the service alongside HTTP latency. JSON decoding took very little time, while server processing alone exceeded 200 ms in many requests. This showed that the delay was not only from my internet connection.

I then ran a local comparison using the same model, 0.5 vCPU, and 1 GiB memory. I compared the original service with a temporary version that allowed only one model calculation at a time. Each ran with 10 users for 60 seconds. The p95 dropped from **389.51 ms to 280.60 ms**, including time spent waiting. Predictions for the test payload stayed the same, with no errors.

This helped in the local test, but **the latency target was still not met**. It was only one local comparison, so I cannot claim the same improvement on Azure. I kept the target and deployed service unchanged, and stopped further tuning for now to continue the remaining lab tasks.

Full results and checks: [Azure timing diagnosis](reports/lab3-load.md#warm-latency-diagnosis--paired-timings-on-azure) and [local concurrency comparison](reports/lab3-load.md#local-concurrency-check--original-versus-one-scoring-call-at-a-time).

## Task 4 — Canary and rollback

1. I compared ROC-AUC for blind groups A and B before checking their model versions. Their scores were 0.8445 and 0.8268, so the 0.0177 gap exceeded my 0.01 alert threshold.
2. I detected the drop 33.73 seconds after the 90/10 split was confirmed, or 138.92 seconds from starting deployment preparation.
3. Azure confirmed traffic was back to version 1 at 15.10 seconds after detection, and all 50 follow-up requests reached version 1.
4. Sending more traffic to the candidate could collect its 1,200 unique rows sooner, without reducing the sample size or changing the alert threshold.
5. At the same request rate, I would expect 50/50 to detect the difference sooner, but about half the requests would reach the worse model instead of 10%. I did not test this split.

### Problems and fixes

The cleanup script timed out after stopping the app, but the candidate still showed as active. I deactivated it once more without restarting the app or repeating the experiment. I then confirmed that the candidate was inactive and all five revisions had zero replicas. I kept the original timeout result in the evidence.

Full results and checks: [canary and rollback report](reports/lab3-load.md#task-4-live-drill--degradation-detected-and-traffic-rolled-back-2026-09-24) and [verified results and cleanup](reports/canary/drill-20260924T033116Z/verification.json).

## Task 5 — Cost per thousand predictions

I used 0.5 vCPU / 1 GiB and measured **33.55 predictions per second**. The active CPU/RAM rate was **$0.0648 per hour**, plus **$0.468 per million HTTP requests**.

Assuming continuous work at this throughput (**100% utilisation**), my estimate is **$0.001005 per 1,000 predictions**:

```text
CPU/RAM: (0.0648 / 3600) × (1000 / 33.55) ≈ $0.000537
Requests: 0.468 × (1000 / 1000000) = $0.000468
Total ≈ $0.001005
```

This assumes one prediction per request and excludes startup. Here, utilisation means how much time the service has work to do, not CPU usage.

For comparison, I assumed **1,000 predictions per hour** with one replica kept warm. That gives about 29.81 seconds of work and 3,570.19 seconds of waiting, or **0.83% utilisation**. Using the idle CPU/RAM rate of $0.0216 per hour, the estimate becomes **$0.02243 per 1,000 predictions**. This assumes the waiting time qualifies for idle pricing. I did not change the app’s actual `minReplicas=0` setting.

### When would batch inference be cheaper?

1. Under the warm-replica assumptions above, N requests per hour cost about $0.0216 + ($0.0000008257 × N), within the measured serving capacity. Batch is cheaper at volumes where its hourly cost is below this amount.
2. I have not measured a batch job, so I cannot give a confirmed break-even volume. At 1,000 requests/hour, a same-size hourly job would need to finish within **20.76 minutes**, including startup and before extra costs.

All amounts are USD retail estimates before free allowances or student credit, not my actual bill. They exclude logs, network, storage, and other shared services.

Evidence and pricing: [load-test results](reports/loadtest/run-20260923T082341Z-vus10-mDq4V8/summary.json), [Malaysia West retail prices](https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20%27Azure%20Container%20Apps%27%20and%20armRegionName%20eq%20%27malaysiawest%27%20and%20priceType%20eq%20%27Consumption%27%20and%20skuName%20eq%20%27Standard%27), and [Azure billing rules](https://learn.microsoft.com/en-us/azure/container-apps/billing). Prices checked on 2026-09-24.

## Teardown and cost check

I ran `make teardown` with the required confirmation. On 2026-09-24,
I confirmed that the Lab 3 Container App, environment, managed identity,
and two role assignments were gone. I kept ACR, Storage, Azure ML,
and the shared monitoring resources.

I also checked the resource group in Azure Portal. Only the seven shared
resources remained; the Lab 3 app, environment, and identity were no longer listed.

### Problem and fix

The first run timed out while the environment was still
`ScheduledForDelete`. I waited and checked again. Once the environment
was gone, I reran the command to remove the remaining role assignments
and identity.

### Cost check

I ran `make cost-report` for 21–24 September 2026 (UTC). The snapshot
reported about USD 0.54 for shared resources. This is not a Lab 3-only
total.

Azure had not reported cost rows for the Lab 3 resources, so I did not
treat the missing data as zero cost. The snapshot is not a final bill.

Full breakdown: [cost report](reports/lab3-cost.md).
