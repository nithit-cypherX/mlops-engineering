# Lab 3 — Load test

## Task 3.1 — Plan before measuring

Recorded on 2026-09-21 (UTC+07:00), before running the Task 3 load tests. Task 2 smoke checks and request logs already exist, but they are not load-test results. This report does not claim that the target has been met.

### Target

For a warm `POST /predict` request, my target is **p95 < 200 ms at 10 concurrent virtual users**, with no failed predictions observed. This means at least 95% of the measured requests should finish within 200 ms using the timing definition below.

I kept the 200 ms example from the instructor's [k6 starter](https://github.com/pasdptt/public_teaching_mlaiops/blob/317f56ebf8563211d338c372c0db8d3a09e2004d/loadtest/k6.js) as an initial lab target. It is not a required value or a production SLA. I will report a miss honestly rather than raise the target after measuring.

Latency uses k6's `res.timings.duration`: sending the request, waiting, and receiving the response. It includes network time during that exchange, but excludes initial connection setup, DNS, and TLS. It is not just the latency written by the server. See the [k6 metric definitions](https://grafana.com/docs/k6/latest/using-k6/metrics/reference/).

### Baseline to keep fixed

| Setting | Planned value |
|---|---|
| Tool and client | k6 through Docker on this laptop's WSL Ubuntu; reuse the instructor's starter |
| Endpoint | Existing Lab 3 Azure Container App in `malaysiawest`, over HTTPS |
| Model | `models:/itcs355-u6688124/1` |
| Compute | 0.5 vCPU, 1 GiB memory, one worker |
| Scaling | Minimum 0, maximum 1 replica; confirm one ready replica for warm runs |
| Input | The fixed single-row `BASE_PAYLOAD` in [scripts/serving.py](../scripts/serving.py) |
| Warm rounds | 1, 10, then 50 virtual users; 60 seconds per level, run separately |

Use the same serving image as Task 2:

```text
itcs355u6688124.azurecr.io/itcs355-lab3@sha256:99ce0052c62042bf4a60962675f8e20871a4844b3933dc11cf59ab08753d0aba
```

The k6 version/image will be pinned when preparing the script. It has not been downloaded in Task 3.1.

### How I will measure

- **Warm runs:** confirm readiness before starting the measured round. Each virtual user sends one request at a time, repeatedly, without an added sleep. Keep startup and warm-up requests outside the warm results.
- **Results per level:** record p50/p95/p99 in milliseconds, request count, actual elapsed time, requests per second, and failed requests as both a count and a percentage. Keep slow and failed attempts in the record; do not report only successful requests. Record timeouts or unfinished requests separately.
- **Valid response:** HTTP `200`, a finite probability in `[0, 1]`, and model version `1`. A timeout, non-200 response, or invalid response counts as a failed prediction. Errors are not ignored just because they are below the starter's 1% threshold.
- **Cold start:** first confirm that the running app has scaled to zero replicas. Then time the first prediction request without warming it through `/ready` or a smoke check. Record full client elapsed time, HTTP duration, and the outcome separately. A timeout is a timeout, not a successful cold start. Time spent manually starting a stopped app is not this measurement.
- **Breaking point:** find the lowest tested concurrency where p95 reaches/exceeds 200 ms or errors appear. Refine between the passing and failing levels when needed. If 50 still passes, agree on a bounded higher-load round before continuing; report only the tested bound until the breaking point is found. Check whether the load generator, rather than the service, is limiting throughput.

Keep the run timestamp, script revision, k6 version, request count, and endpoint configuration with the results. Save failed rounds too. If too few requests finish to support a useful p99, state the limitation and agree on a longer run without changing the target.

### Remaining Task 3 work

These steps follow the instructor's [Task 3 handout](https://github.com/pasdptt/public_teaching_mlaiops/blob/317f56ebf8563211d338c372c0db8d3a09e2004d/course/labs/lab-03-serving-and-rollback.md).

- **3.2 — Prepare the script:** reuse the k6 starter, collect all required metrics, and add the load-test command. Keep service code unchanged. Get approval to commit the plan and script before the measured runs.
- **3.3 — Run baseline tests:** measure the three levels, cold start, and breaking point. Record which configuration meets the target, or report that none tested does.
- **3.4 — Change one variable at a time:** compare a 100-row batch with 100 single calls; vary valid JSON payload size to investigate serialization overhead; then test one larger instance size. Report latency and cost changes. Keep schema validation intact and agree on the larger size and estimated cost before using it.
- **3.5 — Complete this report:** add actual results and a short explanation, then draft the README evidence for review. The script must be committed for submission.

No load test, Docker image pull, Azure start, instance-size change, README edit, or commit/push was performed in Task 3.1. Keep Task 1–2 code unchanged. Stop the app after later test sessions; final teardown remains a separate required lab step.

## Task 3.2 — Script prepared

Prepared and checked on 2026-09-21. The target above is unchanged. There are still no Azure load-test results.

I adapted the instructor's [k6 script](../loadtest/k6.js) and added [the Docker runner](../scripts/loadtest.sh). The runner uses k6 **2.2.0**, pinned by digest. No Python dependencies or service code were changed.

### Commands

From the Lab 3 directory, the offline checks do not call an endpoint:

```bash
# Needed once on a machine without this image:
docker pull grafana/k6@sha256:9bd01d6941fca969cb61bb57d2da5ee9b385fe2aa8881df3798c196564d6ace6
make loadtest-check
```

After approval to start Task 3.3, confirm the endpoint is ready, then run each level separately. For example:

```bash
make loadtest TARGET='https://<endpoint>/predict' VUS=10 DURATION=60s
```

Use `VUS=1`, `10`, and `50` for the three baseline rounds. This command does not deploy, start, or warm the app. It is not the cold-start test. Warm requests have a 10-second timeout and a 15-second end-of-round grace period so an in-flight request can finish or time out.

Each round creates a new directory under `reports/loadtest/` containing:

- `summary.json`: p50/p95/p99, throughput, counts, errors, timeouts, unfinished requests, run metadata, and the native k6 summary. Latencies include failed responses. `meets_target` evaluates that round against the fixed limits; the main target still requires the 10-user round.
- `console.log`: k6 output, including failures and interrupted-iteration information.
- `exit-code.txt`: the actual k6 exit code. A failed threshold is not hidden by the runner.

The metadata includes the start time, Git SHA, dirty-worktree flag, script hash, and k6 image digest. Record the actual endpoint configuration alongside the measured rounds in Task 3.3. No credentials or cloud config are mounted into the k6 container, and usage reporting is disabled.

### Tooling checks, not performance results

- `make loadtest-check`: **30 checks passed** in a network-disabled container, covering response validation, fixed thresholds, metrics, and summary calculations.
- Isolated local HTTP fixtures confirmed that valid responses pass, while invalid responses, responses slower than 200 ms, and timeouts fail and still save JSON results.
- The actual Makefile runner preserved all three output files and the failure exit code against a closed container-loopback port. Missing/unsafe targets and concurrency above 50 were rejected before running Docker.
- Existing checks: **125 tests** and `make portability-audit` passed. Temporary fixtures and their generated output were removed; their numbers are not reported as model performance.

Task 3.2 is ready. The local commit of this plan, k6 script, runner, offline checks, and Makefile was approved on 2026-09-21, before any Task 3 cloud measurements. This records the load-test tools, not the whole Lab 3 submission. The remaining Task 1–2 files are outside this commit. No push or Azure operation is included. Next, agree on the bounded Task 3.3 cloud run; batch/payload/instance comparisons remain Task 3.4.
