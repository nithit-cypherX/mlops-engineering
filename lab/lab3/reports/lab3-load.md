# Lab 3 — Load test

> Privacy note (2026-09-25): I replaced public client IPs in this report and the linked Azure state files with `PUBLIC_IP_1` to `PUBLIC_IP_4`. The same label means the same original IP; the labels are not real addresses. Measurements, timestamps, and results are unchanged. Hashes recorded during earlier checks are kept as historical evidence for the original file versions, not updated checksums for these redacted copies.

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

## Task 3.3 Part 1 — Warm baseline results

Measured on 2026-09-21, using the three approved levels for 60 seconds each. **The baseline did not meet my p95 < 200 ms target at 10 users.** All responses passed the prediction checks, but they were not fast enough. I kept the target and the failed rounds unchanged.

### What stayed fixed

Before starting, Azure showed the same image digest listed above, model version `1`, 0.5 vCPU / 1 GiB, one Uvicorn worker, and scaling from 0 to 1 replica. The revision was `ca-itcs355-u6688124-lab3--w041wkb`. The laptop's public IP still matched the existing allow-list, so no access rules needed changing.

The app was started at 12:52:13 UTC. `/ready` returned `ready` with model version `1` at 12:53:10 UTC, before measuring. I confirmed one ready replica before each round and after the last round; it stayed the same replica with zero restarts at these checks. These setup requests are not part of the warm results or a cold-start measurement.

The plan and load-test tools were committed before measuring:

- Git commit: `48fa4b3a307441838093e6ef6420485b99253532`.
- k6 script SHA-256: `49bb20dddf1b902c986711b636ac5c8444d02cf3c5da4d43f9845c12d3cbf221`.
- k6: `2.2.0`, image `grafana/k6@sha256:9bd01d6941fca969cb61bb57d2da5ee9b385fe2aa8881df3798c196564d6ace6`.

The saved metadata says `git_dirty=true`: the existing Task 1–2 files were still uncommitted. This commit identifies the test tools, not the whole serving source. The deployed service is identified separately by its pinned image digest. No service code was changed in this test session.

### Results

Each user repeatedly sent the same single-row payload to `/predict`. Throughput is completed requests divided by the actual elapsed time, including requests finishing during the grace period. Latency uses the HTTP duration defined in Task 3.1.

| Users | Requests | Elapsed (s) | Requests/s | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 602 | 60.044 | 10.03 | 77.68 | 220.95 | 359.21 | 0 / 602 (0%) |
| 10 | 1,848 | 60.114 | 30.74 | 314.71 | 497.91 | 722.12 | 0 / 1,848 (0%) |
| 50 | 1,906 | 60.690 | 31.41 | 1,646.89 | 1,981.74 | 2,662.27 | 0 / 1,906 (0%) |

All rounds had zero timeouts, unfinished requests, and interrupted iterations. Every response passed the HTTP status, probability, and model-version checks. All three k6 runs returned exit code `99` because their p95 exceeded 200 ms; `make` returned `2`. These are recorded threshold failures, not missing results.

Raw results, with a `console.log` and `exit-code.txt` in each linked folder:

- 1 user, started 12:53:13 UTC: [summary.json](loadtest/run-20260921T125313Z-vus1-wzAVTM/summary.json).
- 10 users, started 12:54:18 UTC: [summary.json](loadtest/run-20260921T125418Z-vus10-UkNvoc/summary.json).
- 50 users, started 12:55:22 UTC: [summary.json](loadtest/run-20260921T125522Z-vus50-xASsQN/summary.json).

I checked the saved request counts against native k6 HTTP requests, iterations, and validation checks. I also recalculated throughput and matched p50/p95/p99 against the native HTTP-duration metrics. The counts and values agreed.

### Breaking-point conclusion (Task 3.3 Part 3)

The lowest tested concurrency that missed the 200 ms limit was **1 concurrent user**, with p95 **220.95 ms**. At my main target of **10 concurrent users**, p95 was **497.91 ms**. None of the three tested levels met the latency limit. All requests passed the prediction checks, so this was a latency-target miss, not a crash.

There was no passing level to compare with a failing one. I report **1 user as the lowest tested failing level**, not as the server's maximum capacity or proof that it can only serve one user. I kept the original target unchanged.

Moving from 10 to 50 users barely increased throughput, from 30.74 to 31.41 requests/s, while p95 rose from about 498 to 1,982 ms. More concurrent users did not mean much more completed work per second with this setup.

These are single 60-second rounds from my laptop over the internet, not repeated trials. The 1-user round has only 602 samples, so its p99 is based on a small tail. A Docker snapshot during the 50-user round showed the k6 container at 7.13% CPU and 37.58 MiB memory; that snapshot showed no obvious client CPU or memory pressure, but does not rule out client/network effects or prove a server bottleneck.

### Stop and remaining work

After the last round, Azure readback confirmed that the app template and ingress configuration were unchanged. I requested stop at 12:56:29 UTC and confirmed **`Stopped` at 12:56:32 UTC** (19:56:32 Bangkok time). This stops the app for this session; it is not the lab's final teardown.

This session completed the warm baseline. The final breaking-point conclusion is above, and the cold-start measurement is recorded in Part 2 below. Batch, payload-size, and instance-size comparisons remain Task 3.4. This session did not change sizing, IAM, access rules, the README, or service code, and did not commit or push the results.

## Task 3.3 Part 2 — Cold-start script prepared

Step 1 was prepared and checked locally on 2026-09-21. **No Azure cold start has been measured yet.** The warm script and its saved results are unchanged.

The separate [cold-start script](../loadtest/cold-start.js) reuses the payload and response validator from `k6.js`. It sends one prediction request, with no readiness call, warm-up, redirect following, or retry. The timeout is fixed at 120 seconds. It saves the full client-call elapsed time, HTTP duration, HTTP status, error code, and validation outcome. A timeout stays a failed observation, not a successful cold-start time. There is no p95/p99 or 200 ms threshold for this single observation.

The script uses k6's [fixed-iteration executor](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/shared-iterations/) and [request timeout](https://grafana.com/docs/k6/latest/javascript-api/k6-http/params/). Full elapsed time is measured around the client call; [HTTP duration](https://grafana.com/docs/k6/latest/javascript-api/k6-http/response/) excludes initial connection setup, so the two are kept separately.

The existing runner now supports `make cold-start-check` for offline checks and `make cold-start` for the later measurement. It uses the same pinned k6 image and saves `summary.json`, `console.log`, and `exit-code.txt` in a separate `reports/loadtest/cold-start-*` folder, including failed attempts. It records both script hashes because the cold script imports the warm script's payload and validator.

For the later approved cloud run, first confirm through Azure that the running app has zero replicas. Then use the actual UTC check time:

```bash
make cold-start TARGET='https://<endpoint>/predict' \
  ZERO_REPLICAS_CONFIRMED_AT='<actual check time, YYYY-MM-DDTHH:MM:SSZ>'
```

That timestamp is supplied by the operator; it is not proof that the script checked Azure. Keep the actual zero-replica readback with the result. Do not call `/ready` or perform a smoke check between that confirmation and the measured request. This command does not start, scale, or stop the app.

### Local checks

- `make cold-start-check`: **25 checks passed**. The unchanged warm checks also passed **30 checks**.
- Seven local HTTP cases covered a valid response, an invalid probability, HTTP 503, a redirect, a wrong model version, a refused connection, and the real 120-second timeout. The valid case passed; all six failure cases returned k6 exit `99` and saved their results. Fixture request logs confirmed one POST with the original payload per reachable case, and no readiness request or retry.
- The actual `make cold-start` runner preserved the three result files, metadata, and nonzero exit against a closed container-loopback port. Missing target, missing zero-replica metadata, and an unsafe URL were rejected before starting a run.
- Existing checks: **125 tests** and `make portability-audit` passed; the three existing deprecation warnings remain. The temporary fixture containers and dummy results were removed, not added to the Azure evidence.

Only the test tooling and this report changed. No dependencies were installed, no service/README changes were made, and no Azure operation, commit, or push was performed. Next is approval to commit the cold-start tooling, followed by the separately approved cloud measurement: wait at most 15 minutes for zero replicas, send one request, keep its outcome, and stop the app even if the check fails.

### Cloud preflight paused — 2026-09-22

The four cold-start tooling files were committed as `51ec9912b53e199f2d7092e78c2e51210412789a` before this attempt. They still matched that commit. Docker and Azure access worked, and Azure readback at 06:35:57 UTC showed the app was `Stopped`, with the same revision, image, model version, and compute settings as the warm baseline.

The access check found a changed outbound IP: both WSL and a container on Docker's default network reported `PUBLIC_IP_2`, but the existing `lab3-client` allow rule permits only `PUBLIC_IP_3/32`. I stopped at preflight without starting the app or sending a prediction request. No cold-start result was produced, and no access rule or other Azure configuration was changed. The next step needs a user decision: return to a network with the allowed outbound IP, or approve a narrowly scoped update to the allow rule for the current IP.

### Cold-start result — 2026-09-22

After the user approved the IP update, I measured **one first prediction after scale-to-zero**. It returned a valid prediction in **56.769 seconds** of full client elapsed time. This is one observed cold start, not a p95/p99 estimate or a change to the warm latency target.

| Requests | Full client time | HTTP duration | HTTP status | Model | Timeouts | k6 exit |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 56,769 ms | 56,652.59 ms | 200 | 1 | 0 | 0 |

The response passed the existing probability and model-version checks. Native k6 metrics also showed one HTTP request, one completed iteration, one passed validation, and zero interrupted iterations. `make cold-start` returned `0`. The script and shared validator hashes matched commit `51ec9912b53e199f2d7092e78c2e51210412789a`; `git_dirty=true` still reflects the other uncommitted Lab 3 work.

**Evidence that the request started cold:**

- At 06:51:06 and 06:51:39 UTC, Azure showed the app as `Running`, the active revision as `ScaledToZero`, and an empty replica list.
- The measured request started at 06:51:39 UTC. There was no client `/ready` call, smoke check, warm-up request, or retry before it. The existing platform probes were unchanged.
- The post-request readback showed a new replica created at 06:51:40 UTC, with its container ready and zero restarts after the response.
- I requested stop at 06:52:39 UTC and confirmed **`Stopped` at 06:52:41 UTC** (13:52:41 Bangkok time).

The manual app start, idle wait, and management API checks are outside the measured latency. The first zero-replica observation was at 06:47:20 UTC; final confirmation before measuring was within the agreed 15-minute wait budget.

Raw evidence: [summary.json](loadtest/cold-start-20260922T065139Z-vus1-blYweB/summary.json), [console.log](loadtest/cold-start-20260922T065139Z-vus1-blYweB/console.log), [exit-code.txt](loadtest/cold-start-20260922T065139Z-vus1-blYweB/exit-code.txt), and [Azure state checks](loadtest/cold-start-20260922T065139Z-vus1-blYweB/azure-state.json).

**Problems and fixes:**

- The current IP was not allowed. With user approval, I updated only the existing `lab3-client` Allow rule to `PUBLIC_IP_2/32`, using Azure's [rule update command](https://learn.microsoft.com/en-us/azure/container-apps/ip-restrictions#update-a-rule). Before/after checks confirmed the image, model, compute, scaling, and managed identity were unchanged. The new IP rule remains in place.
- My temporary wait controller expected `ScaleToZero`, but the API returned `ScaledToZero`. I paused and replaced that controller, checked the actual state twice, and continued without restarting the app or sending an extra prediction. The committed k6 script was not changed.

This run used a different outbound network/IP from the warm baseline, so it is not a controlled same-network comparison. I report the observed first-request time separately rather than attributing the entire difference from warm latency to model startup.

The cold-start measurement is complete. No IAM, instance-size, service-code, or README changes were made, and no new commit or push was performed. Final teardown is still separate.

**Task 3.3 is complete:** the three warm levels, breaking-point conclusion, and separate cold-start result are recorded. Completing these measurements does not mean the latency target passed. Next is Task 3.4: batch, payload-size, and instance-size comparisons. Task 3.5 will finish the report and draft the README evidence for review.

## Task 3.4.1 Part 1 — Batch-comparison tooling

Prepared and checked on 2026-09-23. **Part 1 is complete: the offline tooling checks passed.** Docker was initially unavailable in WSL; the checks could run after Docker Desktop was opened. Part 1 did not run an Azure batch comparison; the later Part 2 results are recorded below.

I added [the comparison script](../loadtest/batch-compare.js) and [offline checks](../tests/batch-compare.test.js), using the existing pinned k6 image and runner. The service already accepts up to 100 rows at `/predict/batch`, so it did not need changing. The warm and cold-start scripts are unchanged.

### Fixed comparison

- Use the original payload repeated 100 times in both methods. One method sends 100 sequential `/predict` calls; the other sends one `/predict/batch` call with those same rows. This compares one fixed input, not a varied dataset.
- Use one virtual user for three pairs, ordered single/batch, batch/single, single/batch. Keep the existing image, model, compute, and replica settings for the later cloud run. Three pairs are my bounded comparison plan, not an instructor-specified count or a tail-latency estimate.
- After confirming the service is ready, the script sends one single and one batch warm-up in the same VU. They are recorded but excluded from the comparison timers: 2 warm-up requests plus 303 measured requests when the run completes.
- Time the whole 100-row job on the client, including response parsing and validation. Request bodies are prepared before timing. The summary keeps each pair, the median time for each method, and `median single time / median batch time`. This is not a requests/s-only comparison or a new 200 ms target.
- Check HTTP status, model version, all 100 probabilities, and agreement with the single-call results using the existing service test's absolute tolerance of `1e-9`. Keep errors and timeouts; do not publish an aggregate speedup if a pair is missing, invalid, or incomplete.
- No retries or redirects. Each request has a 10-second timeout; the comparison is capped at 180 seconds plus a 15-second grace period. An interrupted run remains incomplete rather than being treated as a faster result.

The script uses k6's [tagged metrics and thresholds](https://grafana.com/docs/k6/latest/using-k6/thresholds/#set-thresholds-for-specific-tags) to keep per-pair timings and [custom summary](https://grafana.com/docs/k6/latest/results-output/end-of-test/custom-summary/) to retain them alongside native metrics. Passing checks means a complete, valid comparison, not that batch must win.

### Commands and checks

From the Lab 3 directory, the offline check uses Docker with networking disabled:

```bash
make batch-compare-check
```

Only after the script is checked, committed with approval, and the cloud experiment is approved:

```bash
make batch-compare TARGET='https://<endpoint>/predict'
```

The command saves `summary.json`, `console.log` (including each pair's counts/errors), and `exit-code.txt` in a new `reports/loadtest/batch-compare-*` directory. It records the Git SHA, dirty flag, script/shared-script hashes, and k6 image. It does not start, resize, or stop Azure; the app must be stopped after the separately approved test session.

Checks completed:

- `make batch-compare-check`: **45 checks passed** in the pinned k6 runtime, with networking disabled. These cover request counts/order, timing, response validation, equality tolerance, summaries, and failure/incomplete cases using stub responses and clocks.
- Regression checks: `make loadtest-check` passed **30 checks**, and `make cold-start-check` passed **25 checks**, also with networking disabled.
- Two temporary loopback API fixtures ran the actual comparison script in a Docker network with no external access. Both received exactly 305 POSTs: 2 warm-ups and 303 measured requests, with the planned alternating order and no GET or retry. Matching predictions produced exit `0` and a valid summary. Different batch predictions produced exit `99`, kept all three pair timings, and left speedup values empty. These are tooling checks, not model-performance results. The temporary containers and generated summaries were removed.
- Shell syntax, Makefile command routing, and JavaScript syntax using the existing Windows Node 22 passed. The runner rejected a missing target and three unsafe target formats before Docker. The existing **125 tests** and portability audit passed, with the same three deprecation warnings.

No dependencies were installed, no service or README changes were made, and no Azure operation or push was performed. Payload-size and instance-size experiments are outside this part.

With user approval, the four batch-tooling files were committed locally on 2026-09-23 as `5c7abeabf05b33972a0928cac417aaf8f8d9d0be` (`feat(lab3): add paired batch comparison test`). The 45 batch, 30 warm, and 25 cold-start checks passed again before committing. This report, the README, and the other existing Lab 3 work were not included. Part 2 was then approved; its preflight result is below.

## Task 3.4.1 Part 2 — Preflight paused

Checked on 2026-09-23 at 04:55:19 UTC (11:55:19 Bangkok time). Docker and Azure login worked, and the batch tooling still matched commit `5c7abeabf05b33972a0928cac417aaf8f8d9d0be`. Azure showed the app as **`Stopped`**, with provisioning state `Succeeded`, the same serving image and revision, model version `1`, 0.5 vCPU / 1 GiB, and scaling from 0 to 1 replica.

The current outbound IP was **`PUBLIC_IP_1`** for both WSL and Docker. The existing `lab3-client` Allow rule permits only **`PUBLIC_IP_2/32`**. I stopped at preflight without starting the app, sending a prediction, or running the comparison. No Azure configuration or IAM changes were made, and no batch result was produced.

This preflight required user approval to replace only the existing rule with `PUBLIC_IP_1/32`. The approved update and measurement are recorded below. No README edit, commit, or push was performed in this preflight step.

### Batch-comparison result — 2026-09-23

After approval, I rechecked both outbound IPs and the full app configuration. Only the existing `lab3-client` Allow rule changed, from `PUBLIC_IP_2/32` to `PUBLIC_IP_1/32`, verified at 04:59:02 UTC. The new rule remains in place; no other configuration or IAM setting changed.

I requested app start at 05:03:15 UTC. The first three `/ready` checks timed out during startup; the next returned `ready` with model version `1`. Azure then confirmed one ready replica at 05:04:12 UTC, within the five-minute limit. Startup and readiness checks are outside the comparison timings.

I ran `make batch-compare` **once**, starting at 05:04:12 UTC, using commit `5c7abeabf05b33972a0928cac417aaf8f8d9d0be`. The same pinned serving image, model version `1`, 0.5 vCPU / 1 GiB, one worker, and 0-to-1 scaling were kept. There was no additional measurement run.

Each number below is the client time to finish all 100 rows, including response parsing and validation, with warm-up excluded:

| Pair | Order | 100 sequential singles (ms) | One 100-row batch (ms) | Predictions match |
|---:|---|---:|---:|---|
| 1 | Single, then batch | 4,047 | 42 | Yes |
| 2 | Batch, then single | 3,995 | 46 | Yes |
| 3 | Single, then batch | 3,924 | 63 | Yes |

The median times were **3,995 ms for singles** and **46 ms for batch**. Their ratio was **86.85x** (`3995 / 46`). This compares completion of the same 100-row job over this network; it is not a claim that the model's computation alone became 86.85 times faster. It also does not replace the earlier 10-user p95 target.

There were **305 completed requests**: 2 warm-ups and 303 measured requests. All three pairs returned matching predictions within the fixed absolute tolerance of `1e-9`. There were **zero errors, timeouts, unfinished requests, or interrupted iterations**. Both k6 and `make` returned `0`.

I matched the saved summary against native k6 metrics and the three per-pair console records, recalculated the medians and ratio, and checked the script hashes against the committed tools. The counts, times, and validation outcomes agreed. `git_dirty=true` records the other uncommitted Lab 3 work; the test tools themselves still matched the commit.

Azure readback before and after the comparison showed the same ready replica with zero restarts, and unchanged app settings. I requested stop at 05:04:27 UTC and confirmed **`Stopped` at 05:04:29 UTC** (12:04:29 Bangkok time).

Raw evidence: [summary.json](loadtest/batch-compare-20260923T050412Z-vus1-WV9qzQ/summary.json), [console.log](loadtest/batch-compare-20260923T050412Z-vus1-WV9qzQ/console.log), [exit-code.txt](loadtest/batch-compare-20260923T050412Z-vus1-WV9qzQ/exit-code.txt), and [Azure state checks](loadtest/batch-compare-20260923T050412Z-vus1-WV9qzQ/azure-state.json).

## Task 3.4.1 Part 3 — What this means

Batch used one HTTP request and one scoring call for all 100 rows. Single calls repeated these steps 100 times. This helps explain why batch finished the whole job sooner. I did not measure network time and model time separately, so the 86.85x result is not a model-only speedup.

All 100 rows were ready before timing started. This result covers three pairs with the same repeated payload, one client, and a warm service. It does not show how long users would wait while a batch is being filled, or how batch performs with many concurrent users.

**Task 3.4.1 (batch size) is complete:** tooling, measurement, and interpretation are recorded. Payload-size and instance-size comparisons still remain, followed by the final report and README evidence in Task 3.5. No service/README changes, instance-size changes, new commit, or push were made in this step. Final teardown remains separate work.

## Task 3.4.2 Part 1 — Prepare payloads and check the timing path

Checked locally on 2026-09-23. [payload-cases.js](../loadtest/payload-cases.js) reuses the original sensor row and adds only spaces before the closing JSON brace. It prepares four bodies; it does not send requests. No extra fields or rows are added.

| Case | Request bytes | Local API result | Rows passed to the model stub |
|---|---:|---|---:|
| Original | 120 | HTTP 200 | 1 |
| 10 KiB | 10,240 | HTTP 200 | 1 |
| 100 KiB | 102,400 | HTTP 200 | 1 |
| 1 MiB | 1,048,576 | HTTP 200 | 1 |

These are synthetic size cases, not realistic larger sensor records. The sizes are my test plan, not instructor-specified values. [JSON permits this whitespace](https://www.rfc-editor.org/rfc/rfc8259#section-2). This mainly exercises request transfer and JSON decoding (deserialization), not extra prediction work or a larger response.

### Checks

- [Offline k6 checks](../tests/payload-cases.test.js): **23 passed** in the existing pinned k6 image with networking disabled. All bodies have the exact byte sizes and decode to the same six keys and values. The original body is unchanged.
- A separate in-memory check sent those exact k6-generated bodies through the local API. Each was parsed once and passed the same one-row values to the model stub once. All returned the stub probability `0.68` and version `1`. This checks the contract, not the registered model's predictions or performance.
- Unknown fields, out-of-range values, missing fields, and malformed JSON still returned **422**, without reaching the model stub. Socket connections were blocked; none were attempted. Payload bodies stayed in memory, with no large fixture files saved.
- Existing checks also passed: **30 warm, 25 cold, 45 batch**, plus **29 API/observability tests** with the same three deprecation warnings.

The cached serving image could not run `TestClient` because its test-only `httpx2` dependency was absent. I used the existing Lab 3 test environment instead, without installing anything. The image and local environment have matching FastAPI `0.141.1`, Starlette `1.6.0`, Pydantic `2.13.5`, and matching service/schema file hashes.

### Timing method — initial proposal

The current path is: read the body, decode JSON, validate the six fields, score one row, then build the response. FastAPI calls `Request.json()` after reading the bytes; Starlette uses `json.loads(body)`. The existing `latency_ms` log covers combined request handling, not a separate JSON or model timer.

Keep body construction outside request timing. Record HTTP latency separately from server-side JSON decoding and scoring. A JSON timer should start after the body has been read, so upload time is not called JSON parsing time. Compare it with scoring and total server processing before claiming that JSON dominates. Do not subtract a local parser benchmark from Azure HTTP time or treat k6's waiting time as model time.

**Payload preparation and local contract checks are complete.** No performance result or serialization crossover has been measured. Direct timing inside the Azure service would need a reviewed instrumentation/image change; a local-only profile would not prove the Azure breakdown. The user subsequently approved local timing code and tests, recorded below; deployment remains separate. No service/schema, README, existing load-test runner, Azure resource, dependency, commit, or push was changed during payload preparation. The preparation files are not yet committed load-test evidence.

## Task 3.4.2 — Local timing implementation, before Part 2

With approval, I added timing only to `/predict` in [service/app.py](../service/app.py). It uses FastAPI's [custom request/route mechanism](https://fastapi.tiangolo.com/how-to/custom-request-and-route/), the existing JSON parser, and Python's `perf_counter_ns`. A successful response now has a [Server-Timing header](https://www.w3.org/TR/server-timing/#the-server-timing-header-field), with durations in milliseconds:

| Header metric | What it measures |
|---|---|
| `json_decode` | The framework's JSON decoding path with the body already in memory |
| `scoring` | Preparing the row, building the model input, and getting the prediction |
| `processing` | From full body receipt to the route returning the rendered response |

JSON decoding and scoring are parts of processing, not extra times to add to it. These are elapsed times, not CPU-only times. Processing includes validation and any wait for the worker thread; it excludes body receipt, outer middleware logging, and sending the response over the network. The small timing-wrapper overhead is not removed from the readings.

The response JSON, schema, model calculation, and existing logs stay unchanged. Timings belong to each request, not shared model state. Invalid input and model failures keep their original error responses and have **no success timing header**. Batch, readiness, and liveness do not use the new route.

Checks on 2026-09-23:

- **19 new offline tests** in [test_payload_timing.py](../tests/test_payload_timing.py) passed. They cover all four sizes, unchanged predictions through the model stub, validation errors, model failure/recovery, an unavailable model, untouched routes, cached JSON, and two overlapping requests.
- A controlled clock test added 100 ms for body receipt, 2 ms for JSON, 3 ms for scoring, and 5 ms for response construction. Headers reported **2 / 3 / 10 ms**, not 110 ms. This verifies units and timer boundaries; these are simulated times, not performance results.
- `make test`: **144 passed**, with the same three deprecation warnings. `make portability-audit`: passed.
- The generated OpenAPI schema hash is unchanged: `4e9d87a891db72b2123ef45b139688213ca20dae1662f92f508c829527330551`. README, request/response schema, dependency locks, and previous load-test tools are unchanged.

**Local timing code and tests are complete.** No image was built or pushed, no Azure app was started or deployed, and no dependency, commit, or push was added. The running-image reference in earlier results is still the old image; those results were not replaced. At this point, the payload-size k6 measurement script was still missing; its implementation is recorded next. Approved commit/image/deployment work is still needed before the cloud experiment. Task 3.4.2 is not yet a completed performance comparison.

## Task 3.4.2 — Payload comparison tooling, before Part 2

[payload-compare.js](../loadtest/payload-compare.js) now uses the four prepared bodies with one user. It sends one warm-up per size, then 20 measured requests per size: **84 requests** in a complete run. Each round rotates the first size, so every size appears in each position five times. Bodies are prepared before requests. The request timeout is 10 seconds, with no retry or redirect and a hard scenario stop at 180 seconds. Failed warm-up stops measurement; an interrupted run stays incomplete.

The script checks HTTP status, model version, and prediction agreement with the first warm-up using absolute tolerance `1e-9`. It keeps HTTP time separate from the three server timers. Missing, duplicate, invalid, or inconsistent timing values fail the check; missing time is never replaced with zero. It records each response in the console, including errors, timeouts, warm-up status, size, and raw timing header.

For each size, the summary keeps median HTTP, JSON, scoring, and processing times. It also takes the **median of each request's `json_decode / processing` share**, not a ratio of two medians. A median share above 50% means JSON takes most of this measured server processing time. This is our stated rule, not an instructor-provided threshold or a claim about total HTTP time. The summary names the lowest tested size meeting it, or says it was not observed in the tested range. Incomplete or invalid comparisons have no median comparison or majority conclusion. Twenty samples are a bounded comparison, not a reliable p99 estimate.

The existing runner and Makefile now provide:

```bash
make payload-compare-check
# Only after the required commit, image/deployment work, and experiment approval:
make payload-compare TARGET='https://<endpoint>/predict'
```

The future endpoint run saves `summary.json`, `console.log`, and `exit-code.txt` in a new `reports/loadtest/payload-compare-*` directory. Metadata includes Git SHA/dirty state, hashes of the comparison script, shared script and payload cases, and the pinned k6 image. No readiness, start, deployment, or stop operation is built into this runner.

Checks on 2026-09-23:

- `make payload-compare-check`: **98 checks passed**, including the 23 payload-preparation checks, with networking disabled. Checks cover parsing, counts/order, warm-up separation, prediction agreement, timeouts, invalid/missing evidence, median-share logic, and incomplete results.
- Two temporary HTTP fixtures ran the actual k6 script against the current service code with a model stub inside isolated Docker networking. Each received exactly **84 POSTs**, with the planned body sizes/order and no GET or retry. The valid case exited `0`; removing one measured response's timing header exited `99`, retained the failed sample, and suppressed comparison results. Raw records, native counts, and summary medians were independently checked. These are tooling checks, not performance evidence. The temporary containers and summaries were removed.
- Regressions passed: **30 warm, 25 cold, 45 batch** checks; **144 Python tests** with the same three warnings; and the portability audit. Shell syntax, Makefile routing, and missing/unsafe-target guards passed.

**At the end of this tooling step, the scripts were ready but not yet cloud-tested or committed.** This step changed only the new comparison script/test, Makefile, runner, and this report. Service code, schema, README, dependencies, and earlier load-test scripts stayed unchanged. No Azure resource was accessed, no image was built/pushed, and no commit/push was made in that step. The later commit and local image checks are recorded below; the old deployed image still does not contain these timers.

## Task 3.4.2 — Local image checkpoint

The serving code, build inputs, payload scripts, and related tests are now in local commit `0606ecbe5cd977361798cfd8adf44c819f83b322`. That commit did not include README changes, this report, raw measurement outputs, or private configuration. The pre-commit checks passed: 144 Python tests, 98 payload checks, 100 existing warm/cold/batch checks, the portability audit, and `pip check`.

On 2026-09-23, I built `itcs355-lab3:0606ecb` for `linux/amd64`. Its local image ID is `sha256:e92a8d6ef68cf94d487f7b3803bba251c2f5843d9dc4d1974b479006a29cfb6c`; this is not an ACR repository digest.

**Problem and fix:** the first build from the working folder copied Python cache files into the image. I rejected that image and rebuilt from the committed files only. This kept the source and dependency locks unchanged. Run this from the repository root to use the same build input:

```bash
set -o pipefail
git archive --format=tar 0606ecbe5cd977361798cfd8adf44c819f83b322:lab/lab3 | \
  docker buildx build --platform linux/amd64 -f service/Dockerfile.serve \
  -t itcs355-lab3:0606ecb --load -
```

The Dockerfile and `.dockerignore` were not changed, so use the archive command above for this image rather than rebuilding from a folder containing local cache files. Dependencies were installed inside the image using the existing hashed lock; nothing was installed into the host environment.

Local checks passed with external networking disabled, no mounted source or credentials, and a read-only container:

- All 12 files under the image's `service/`, `src/`, and `cloudlayer/` matched the committed source. No extra cache files were present there. The container ran as UID 10001 (`runner`), and `pip check` passed.
- A one-worker HTTP smoke check used a model stub. Requests of 120 bytes, 10 KiB, 100 KiB, and 1 MiB returned HTTP 200, the same expected prediction, and all three valid `Server-Timing` values.
- Three invalid requests stayed HTTP 422 without scoring or success timing headers. Health, readiness, and batch checks passed, and the stub loaded once.

The smoke check explicitly used one worker, matching the Azure baseline. The image's existing default command still specifies two workers; it was not changed. The test container and rejected first image were removed. README and source files stayed unchanged.

**At this local checkpoint, the image was checked but not pushed or deployed.** These stub checks do not verify real registry/model loading or cloud performance. No Git push, image push, Azure start, deployment, or payload performance experiment was performed in that step. The later image push is recorded below.

## Task 3.4.2 — ACR image checkpoint

On 2026-09-23, I used the existing adapter to push the checked image to `itcs355u6688124.azurecr.io/itcs355-lab3:0606ecb`. The tag did not already exist. ACR recorded it at `2026-09-23T06:29:49Z`.

The digest-pinned reference is:

```text
itcs355u6688124.azurecr.io/itcs355-lab3@sha256:688d36dd527294d20107908fd610df7261feb69db2ead30d585208f96aba3061
```

- The digest reported by ACR matched the Docker push result.
- Pulling by this digest succeeded. The image ID matched the locally checked image, `sha256:e92a8d6ef68cf94d487f7b3803bba251c2f5843d9dc4d1974b479006a29cfb6c`, with `linux/amd64` and user `runner`.
- Before and after the push, the app was `Stopped`, on revision `ca-itcs355-u6688124-lab3--w041wkb`, still using the old image digest `sha256:99ce0052c62042bf4a60962675f8e20871a4844b3933dc11cf59ab08753d0aba`.

Only the image was published in this step. No Git push, deployment, app start, permission change, or real payload measurement was performed. Source, README, and private deployment configuration stayed unchanged in that step. The later deployment check is recorded below.

## Task 3.4.2 — Azure deployment and smoke check

On 2026-09-23, I deployed the new digest above as revision `ca-itcs355-u6688124-lab3--0000001`. WSL and Docker both used `PUBLIC_IP_1`, matching the existing `/32` allow rule. No IP permission change was needed.

- The app was started at `06:52:01Z`. Readiness passed with model version `1` at `06:52:55Z`.
- Two prediction requests, **120 bytes and 1 MiB**, returned HTTP 200 and the same probability, `0.00737357519563027`. Both had model version `1`, request IDs, and valid JSON/scoring/processing timing headers. This used the real registered model, not a stub.
- Stop was confirmed at `06:52:58Z`. A fresh Azure read at `06:55:06Z` confirmed `Stopped` and provisioning `Succeeded`.
- Comparing the saved configuration before and after showed only the image changed. The model settings, 0.5 vCPU / 1 GiB, one worker, scaling, probes, identity, ingress, and traffic settings stayed the same.

**Problem and fix:** my first check read the old image while Azure was still applying the update. The immediate stop request was rejected because that operation was in progress. Azure's [update API is asynchronous](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/container-apps/update?view=rest-resource-manager-containerapps-2025-07-01). I re-read the state, confirmed that the update had finished and the app was still stopped, then continued with start, smoke, and stop. I did not repeat the deployment or change the app code.

[Deployment evidence](loadtest/deploy-smoke-20260923T064737Z/azure-state.json) keeps the original configuration for rollback, the initial failure, request headers/results, timestamps, and an independently re-read final configuration. The private `SERVING_IMAGE` setting now points to the new digest; other private settings, source, and README stayed unchanged. No Git commit or push was made in this step.

**At the end of this step, deployment and smoke checks passed and the app was stopped.** Two requests proved basic operation, not latency distributions or where JSON processing dominates. The later full payload comparison is recorded below.

## Task 3.4.2 — Measured payload comparison

On 2026-09-23 at 07:08 UTC, I ran the committed payload script against the real model. It sent **4 warm-ups + 80 measured requests**, with 20 measured samples per size and one user. Sizes rotated each round. Warm-ups and readiness GETs are not included in the table.

| Payload | Median HTTP (ms) | JSON decode (ms) | Scoring (ms) | Server processing (ms) | Median JSON share |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original, 120 B | 41.31 | 0.021 | 8.74 | 10.11 | 0.21% |
| 10 KiB | 41.67 | 0.031 | 8.22 | 9.18 | 0.34% |
| 100 KiB | 45.55 | 0.133 | 8.14 | 9.26 | 1.44% |
| 1 MiB | 100.81 | 1.157 | 8.25 | 10.40 | 11.28% |

All time columns are medians. JSON share is the median of each request's JSON decode time divided by its server processing time, not the ratio of the table's medians. JSON and scoring are parts of processing; do not add all three columns together.

**What this shows:** making the body larger increased JSON decode time, but it still did not take most of the measured server processing time. At 1 MiB, median HTTP time was about 2.44 times the original request, while median JSON share was only 11.28%. No tested size crossed our stated **above 50%** rule. I cannot call the extra HTTP time serialization time: HTTP also covers transfer and other waiting outside our server timer.

This is a synthetic byte-size test: the same one-row JSON was padded with spaces. It measures request JSON decoding, not a larger feature set, client-side JSON encoding, or response serialization. **The approved four-size experiment is complete, but the point where serialization dominates was not found.** The instructor's request to locate that point remains unresolved; this result is not evidence that it never happens, or that the threshold must be above 1 MiB. I did not extend the size range or run another experiment without approval.

**Checks:** k6 exited `0`; all 84 requests returned HTTP 200 and passed the model-version, prediction, and timing checks. Predictions matched within `1e-9`. There were no failed, timed-out, or unfinished prediction requests. I separately read the 84 raw records, checked warm-up separation, rotating order, 20 samples per size, and 84 unique request IDs. Recalculating the medians and per-request JSON shares matched both the summary and native k6 metrics; all 37 k6 thresholds passed.

**Same baseline:** model version `1`, 0.5 vCPU / 1 GiB, one worker, and the digest-pinned image recorded above. The revision stayed `ca-itcs355-u6688124-lab3--0000001`. Azure showed the same single replica before and after the run, with zero restarts. The full app configuration was unchanged.

Readiness passed at `07:08:20Z` after three readiness GET timeouts during startup; those are not prediction timeouts. The measurement command ran from `07:08:21Z` to `07:08:27Z`. Stop was requested at `07:08:29Z`, and Azure confirmed **Stopped / Succeeded** at `07:08:36Z`.

Evidence: [summary](loadtest/payload-compare-20260923T070821Z-vus1-4SVIt8/summary.json), [raw requests](loadtest/payload-compare-20260923T070821Z-vus1-4SVIt8/console.log), [exit code](loadtest/payload-compare-20260923T070821Z-vus1-4SVIt8/exit-code.txt), and [Azure state and independent checks](loadtest/payload-compare-20260923T070821Z-vus1-4SVIt8/azure-state.json).

The summary correctly records `git_dirty=true`: README and this report already had local edits. The service, build inputs, and load-test tooling matched commit `0606ecbe5cd977361798cfd8adf44c819f83b322`. This step added the measurement evidence and updated this report only. Source, README, and private configuration stayed unchanged; no Git commit or push was made.

## Task 3.4.2 — Extended payload tooling (offline)

On 2026-09-23, I prepared the next bounded comparison using **1, 4, 8, and 16 MiB**. The 1 MiB request is byte-for-byte the same as the previous experiment's largest body. All four still decode to the same six input values; only JSON whitespace grows. The earlier small-size script remains in commit `0606ecbe5cd977361798cfd8adf44c819f83b322`, and its measurement files were not changed.

The plan still uses one user, four warm-ups, and 20 rotating measured requests per size: at most 84 requests and 609 MiB of request bodies. The 10-second request timeout, 180-second hard measurement limit, prediction checks, and above-50% median JSON-share rule stay unchanged. These sizes and the rule are our experiment choices, not instructor-specified numbers.

The script now stops after the first invalid measured response, as it already did during warm-up. It records the failure before stopping and never retries. Incomplete or failed runs still produce no comparison medians or majority conclusion. This covers HTTP errors such as 413/503, timeouts, mismatched predictions/model versions, invalid JSON, and missing or invalid timing evidence.

**Offline checks passed:** `make payload-compare-check` passed all 133 checks with Docker networking disabled. The new tests first failed against the old code in 23 places, then passed after changing the sizes and stop behavior. They check exact body sizes/content, the shared 1 MiB body, normal counts/order, and stopping at the last warm-up, first measurement, middle, and final request. Existing warm/cold/batch checks also passed: 30 + 25 + 45. These checks use simulated responses and sent no network data; they are not Azure performance results.

Only the two payload scripts, their two tests, and this report changed in this step. No service code, schema, dependency, runner, Makefile, README, private configuration, or earlier measurement file changed. No Azure operation, image build/deployment, Git commit, or push was performed in that offline step. **At that checkpoint, the larger-size cloud experiment had not run and the updated scripts were not yet committed.** The later committed attempt is recorded below.

## Task 3.4.2 — Extended payload attempt: stopped during warm-up

The two payload scripts and their two tests were committed as `a41ec75e1b8c9bc2f41a908ced5437bec5309a68`. On 2026-09-23 at 07:37 UTC, I ran that version once against the real Azure endpoint. The source and tooling matched the commit; `git_dirty=true` reflects the existing README/report edits, not uncommitted test code.

| Warm-up size | Result |
| --- | --- |
| 1 MiB | HTTP 200; prediction, model version, and timing checks passed |
| 4 MiB | HTTP 200; prediction, model version, and timing checks passed |
| 8 MiB | HTTP 200; prediction, model version, and timing checks passed |
| 16 MiB | Client timeout at the configured 10-second limit; no HTTP response or server timing received |

**Obstacle:** the 16 MiB warm-up timed out (`error_code=1050`, HTTP status field `0`). The script kept that record and stopped without retrying. Only four warm-ups were attempted: three passed and one timed out. **No measured requests were sent.** k6 exited `99` because the checks/count thresholds were not met; Make returned `2`.

The summary correctly marks this as `invalid-comparison`, with all comparison medians and majority decisions set to `null`. The 8 MiB warm-up had a JSON share of 54.64%, but that is one excluded warm-up, not the planned median of 20 measured requests. It is a lead for a later decision, not proof that the crossover has been located. This attempt does not establish a 16 MiB Azure body-size limit or the cause of the timeout. I did not raise the timeout, reduce the sizes, change the criterion, or run again.

**Checks and cleanup:** raw records, native k6 counts, and the summary agree: four attempts/completed client calls, one error/timeout, zero measured requests, and no fabricated timing for the failed request. The app configuration exactly matched the previous payload run and stayed unchanged. Before/after Azure reads showed the same single replica with zero restarts, model version `1`, 0.5 vCPU / 1 GiB, one worker, and the existing digest-pinned image.

The app was started at `07:36:29Z`; readiness passed at `07:37:24Z` after three startup readiness GET timeouts. Those GETs are separate from the prediction requests. The payload command ran from `07:37:26Z` to `07:37:42Z`. Stop was requested at `07:37:44Z`, and Azure confirmed **Stopped / Succeeded** at `07:37:52Z`.

Evidence: [summary](loadtest/payload-compare-20260923T073726Z-vus1-cvKDzk/summary.json), [raw requests](loadtest/payload-compare-20260923T073726Z-vus1-cvKDzk/console.log), [k6 exit code](loadtest/payload-compare-20260923T073726Z-vus1-cvKDzk/exit-code.txt), and [Azure state and independent audit](loadtest/payload-compare-20260923T073726Z-vus1-cvKDzk/azure-state.json).

This step added the failed-attempt evidence and updated this report only. README, source, private configuration, and the 22 earlier evidence files stayed unchanged. No deployment, Git commit, or push was made during this experiment. **The attempt is documented and the app is stopped; the payload comparison remains unresolved.**

## Task 3.4.2 — Prepare the 1–8 MiB confirmation (offline)

On 2026-09-23, I changed the next request set to **1, 2, 4, and 8 MiB**, with the user's approval. The failed 16 MiB attempt above remains part of the evidence. Its 8 MiB warm-up suggested where to look next, but this change does not turn that warm-up into a measured result or resolve the timeout's cause.

Only `loadtest/payload-cases.js` and the two payload tests changed, plus this report. The comparison script itself stayed unchanged: one user, four warm-ups, 20 rotating measurements per size, 10-second request timeout, 180-second hard limit, stop on the first invalid response, and a median JSON share strictly above 50%. If completed, this set has 84 requests and 315 MiB of request bodies. The request schema and model inputs still stay the same; only JSON whitespace changes.

`make payload-compare-check` passed **all 133 checks** with Docker networking disabled and no network data sent. Before changing the cases, the revised expectations caught four mismatches in the old set; after the case change, all checks passed. The checks cover sizes/content, counts/order, stopping on failure, and withholding conclusions for invalid or incomplete runs. The test's simulated majority at 8 MiB is not a cloud result.

All 26 existing evidence files, including the 16 MiB timeout records, were preserved unchanged. README, private configuration, service code, measurement logic, and runner stayed unchanged. No Azure operation, image build/deployment, Git commit, or push was performed in that offline step. **At that checkpoint, the set was checked offline but not yet committed or cloud-tested.** The later committed experiment is recorded below.

## Task 3.4.2 — Confirmed payload comparison: 1–8 MiB

The case change and its tests were committed as `2693c980b634fd24d4bf0148a7c250959eeaee6f`. On 2026-09-23 at 08:03 UTC, I ran that version once against the real Azure model: four warm-ups, then 20 rotating measurements for each size, with one user. All **84 requests passed**, with no errors, timeouts, or unfinished requests. Warm-ups are excluded below.

| Payload | Median HTTP (ms) | JSON decode (ms) | Scoring (ms) | Server processing (ms) | Median JSON share |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 MiB | 73.90 | 1.42 | 8.08 | 10.33 | 13.46% |
| 2 MiB | 136.06 | 2.87 | 8.33 | 12.12 | 23.61% |
| 4 MiB | 243.36 | 5.56 | 7.88 | 14.36 | 37.78% |
| 8 MiB | 489.09 | 11.31 | 8.61 | 20.79 | 54.34% |

**Answer:** 8 MiB was the lowest tested size where JSON decoding took more than half of the measured server processing time. Its median share was **54.34%**, above our pre-set 50% rule; 17 of its 20 measured requests also individually exceeded 50%. At 4 MiB, the median was 37.78%. This finds a dominant JSON-decoding case, not the exact crossover between the tested sizes or a guarantee for other workloads.

Each time column is a median. JSON share is the median of the 20 individual `json_decode / processing` ratios, not a ratio of the table's medians. JSON and scoring are already included in processing. These are elapsed times, not CPU-use measurements.

The request still contains one row with the same six values, padded with JSON whitespace. This tests **request JSON decoding**, not client encoding or response serialization. The server processing timer starts after the full body arrives, so the 54.34% is not a share of the 489.09 ms HTTP time. I cannot attribute the extra HTTP delay entirely to JSON decoding.

**Checks:** k6 and Make both exited `0`; all 37 k6 thresholds passed. Prediction/model-version/timing checks passed, and all probabilities matched within `1e-9`. I independently checked all 84 raw records, unique request IDs, warm-up separation, rotating order, and 20 measurements per size. Recomputed medians and per-request shares matched the summary and native k6 metrics.

**Same setup and cleanup:** model `1`, image digest `688d36dd527294d20107908fd610df7261feb69db2ead30d585208f96aba3061`, 0.5 vCPU / 1 GiB, one worker, and the same revision and single replica with zero restarts. Full app configuration stayed unchanged. Start was requested at `08:02:23Z`, readiness passed at `08:03:19Z`, and the command ran from `08:03:21Z` to `08:03:42Z`. Azure confirmed **Stopped / Succeeded** at `08:03:51Z`; a separate read at `08:06:33Z` confirmed it again. Startup readiness GETs are not prediction measurements.

Evidence: [summary](loadtest/payload-compare-20260923T080321Z-vus1-KwGliW/summary.json), [raw requests](loadtest/payload-compare-20260923T080321Z-vus1-KwGliW/console.log), [k6 exit code](loadtest/payload-compare-20260923T080321Z-vus1-KwGliW/exit-code.txt), and [Azure state and independent audit](loadtest/payload-compare-20260923T080321Z-vus1-KwGliW/azure-state.json).

The previous 16 MiB timeout and all 26 older evidence files remain unchanged; its cause is still unknown. This experiment did not retry or change the 10-second timeout, 180-second limit, app resources, permissions, or image. Source/tooling matched the committed version; `git_dirty=true` reflects the existing README/report edits. Only new evidence and this report were added or updated in this step. README and private configuration stayed unchanged; no deployment, Git commit, or push was made.

**Payload-size evidence is now available for Task 3.** This does not complete all of Task 3 or Lab 3; the one-step-up instance-size latency/cost comparison is still pending.

## Task 3.4.3 — Instance-size measurements

On 2026-09-23, I ran one warm 60-second round at 10 users for each size, baseline first. Both used the same image digest `688d36dd527294d20107908fd610df7261feb69db2ead30d585208f96aba3061`, model `1`, one worker, the normal single-row payload, and a maximum of one replica. The unchanged k6 script matched commit `2693c980b634fd24d4bf0148a7c250959eeaee6f`. I remeasured the baseline because the September 21 results used the older image.

| Size | Requests | Elapsed (s) | Requests/s | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.5 vCPU / 1 GiB | 2,018 | 60.149 | 33.55 | 301.68 | 418.24 | 508.23 | 0 / 2,018 |
| 0.75 vCPU / 1.5 GiB | 3,288 | 60.170 | 54.65 | 189.61 | 256.53 | 301.00 | 0 / 3,288 |

The larger size had **38.67% lower p95** and **62.88% higher throughput** in this pair. It was faster, but **neither size met the original p95 < 200 ms target**. Both k6 runs exited `99` because of that latency threshold only. There were no prediction errors, timeouts, unfinished requests, or interrupted iterations. I did not raise the target or try another size.

**Cost change:** the Malaysia West USD retail rates read on 2026-09-23 were $0.000028 per vCPU-second, $0.000004 per GiB-second, and $0.468 per million requests. At active rates, CPU/RAM cost rises from **$0.0648/hour to $0.0972/hour: +50%**. These are calculated prices before free grants or credit, not an observed bill or the amount deducted from student credit. Startup, revision changes, logs, network, and shared services are not included in this hourly compute comparison. The source meter records and calculations are in the evidence below. See [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices) and [billing rules](https://learn.microsoft.com/en-us/azure/container-apps/billing).

**Setup and cleanup:** the baseline ran at 08:23 UTC on revision `0000001`; the larger round ran at 08:26 UTC on `0000002`, after its readiness checks passed. Comparing the immutable revision templates showed only CPU and memory changed. Azure also automatically raised ephemeral storage from 2 GiB to 4 GiB, as described in its [CPU-based storage limits](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts#ephemeral-storage); no volume or storage account was added.

Restoration was requested at `08:27:23Z` and stop at `08:27:36Z`. A separate Azure read at `08:28:48Z` confirmed **Stopped / Succeeded**, the exact original app configuration, and zero replicas for every revision. Restored revision `0000003` has the same template as `0000001`; the larger revision is inactive. I stopped before a new post-restore readiness check, so `latestReadyRevisionName` still names `0000002`. No post-restore prediction was sent; the next session must check readiness normally before measuring.

**Verification and limits:** saved request counts, checks, timing percentiles, and throughput agree with native k6 metrics. This existing runner saves aggregate percentiles, not individual latency samples. One sequential pair does not eliminate network/time effects or establish repeatability. The terminal truncated the controller's final large JSON, so full live replica snapshots were not retained. Both k6 result sets are intact; the separately re-read configuration/revision evidence is saved, without pretending it recreates those missing snapshots.

Evidence: [baseline summary](loadtest/run-20260923T082341Z-vus10-mDq4V8/summary.json), [baseline console](loadtest/run-20260923T082341Z-vus10-mDq4V8/console.log), [larger summary](loadtest/run-20260923T082619Z-vus10-38S6Ty/summary.json), [larger console](loadtest/run-20260923T082619Z-vus10-38S6Ty/console.log), and [Azure readback, audit, and prices](loadtest/run-20260923T082341Z-vus10-mDq4V8/azure-state.json). Each run directory also contains its actual `exit-code.txt`.

Only new measurement evidence and this report were added or updated. All 30 earlier evidence files, README, source/tooling, and private configuration stayed unchanged. No image build, permission change, Git commit, or push was made. The approved two-size experiment is complete; this is not a claim that all of Task 3 or Lab 3 is finished.

## Warm latency diagnosis — timing capture prepared

Prepared locally on 2026-09-23. The existing 10-user results show long waits for the response, but do not separate scoring from other server work. They cannot prove the bottleneck. No new Azure measurement was made in this step.

The [warm k6 script](../loadtest/k6.js) now saves HTTP timings and the three existing `Server-Timing` values together for each completed request, with its request ID, user/iteration, status, and validation result. Records stay in `console.log`; component percentiles and timing-completeness counts are added to `summary.json`. Failed responses, timeouts, and missing/malformed headers remain in the records. Missing timing is not replaced with zero.

I moved the existing timing parser into `k6.js` so the warm and payload scripts share it. The parser and payload comparison behavior are unchanged. Service code, model, image, and runner are unchanged. The normal 120-byte payload, 10-user/60-second defaults, 10-second timeout, and p95 < 200 ms / zero prediction-error targets stay the same. Incomplete timing evidence now fails a separate diagnostic threshold; it does not redefine the latency target.

**Checks:** all 263 offline k6 checks passed (warm 60, payload 133, cold 25, batch 45). Isolated HTTP fixtures passed for valid responses, missing/malformed timing, invalid predictions, HTTP 503, slow responses, timeouts, and 10 concurrent users. Paired record counts matched completed requests, including in the runner's normal console format. All 144 Python tests and the portability audit passed. Temporary fixture containers and results were removed; these are tooling checks, not model-performance evidence.

**Limits and next step:** per-request logging adds client work, so the later diagnostic run is not an instrumentation-free comparison. Processing includes decoding and scoring, but excludes body receipt and outer logging/network work. HTTP minus processing is not network-only time; separate p95 values must not be subtracted. The script changes are not committed yet. A real 10-user/60-second diagnostic round still needs approval and normal readiness checks. No Azure operation, build, dependency installation, README edit, Git commit, or push was performed.

## Warm latency diagnosis — startup check stopped before measurement

On 2026-09-23, my network IP changed from the allowed `PUBLIC_IP_1/32` to `PUBLIC_IP_4`. With approval, I temporarily allowed only the new `/32`. A full configuration comparison confirmed that nothing else changed. The timing script was already committed as `c86613a72d9816b586d32512112396cd131e0c4e`.

The app started and `/ready` passed, but my controller's next check could not confirm one active revision with one replica. It stopped **before invoking k6**. There is no new load-test result or p95 from this attempt.

**Problem and limit:** the controller checked that replica summary only once. It also did not save the failing snapshot before the assertion, so I cannot say which count failed or prove that Azure status propagation caused it. This does not establish a service/model fault. The proposed correction is to wait within a fixed limit for both revision and live-replica readiness, saving the snapshots before checking them. The one-ready-replica requirement stays unchanged; no automatic new run was made.

**Cleanup:** stop was confirmed at `12:48:35Z`, and the original allow-list and full app configuration were restored at `12:48:51Z`. The whole controlled attempt took about 98 seconds. An independent read at `12:50:11Z` confirmed `Stopped / Succeeded`, zero replicas in every revision, no retained live-replica records, and an exact match to the original configuration. All 37 older evidence files, README, private configuration, source, and test scripts stayed unchanged.

Evidence: [controller events and independent cleanup check](loadtest/warm-diagnostic-precheck-20260923T124715Z/azure-state.json). Only this failure evidence and report entry were added. No image build, service change, commit, or push was made.

## Warm latency diagnosis — readiness gate corrected locally

On 2026-09-23, I corrected the check that stopped the previous attempt. After `/ready` passes, the controller now polls the revision summary and live replica details every three seconds. Both checks share the existing readiness deadline (at most 240 seconds, also limited by the overall run budget). Each CLI read uses only the remaining time; the controller does not extend the budget or retry k6.

The gate still requires one active revision, one ready/running replica, zero container restarts, and zero replicas in inactive revisions. The active revision must match the app's latest and latest-ready revision. A stopped replica with old `ready=true` container fields cannot pass. Configuration drift or failed provisioning stops the check. Every returned snapshot is logged before deciding; read errors are logged and stop the attempt. After measurement, the controller also logs before checking that the same replica is still ready.

The small host-side helper is [warm_readiness.py](../scripts/warm_readiness.py), with [offline regression tests](../tests/test_warm_readiness.py). The one-off controller prepared in this session imports it; no new Make target or deployment command was added. Service code, model, image, k6 settings, latency target, README, and previous evidence are unchanged.

**Checks:** 27 new regression cases passed; all 171 Python tests and the portability audit passed. A separate offline fixture exercised the actual controller's readiness and post-measurement blocks with fake HTTP, CLI responses, and time. It checked delayed readiness, remaining-time limits on reads, snapshot sanitization, changed/stopped replicas, and configuration drift. No real cloud request was made.

**Limit and handoff:** this verifies the local correction, not Azure startup or latency. Before another approved diagnostic, refresh the configuration/IP and protected-file hashes; do not reuse the old preflight embedded in the controller. Keep the same single 10-user/60-second round and cleanup rules. No Azure start, load test, image build, dependency installation, Git commit, or push was performed in this step.

## Warm latency diagnosis — paired timings on Azure

On 2026-09-23, I ran the approved diagnostic once: **10 users for 60 seconds**, the same 120-byte payload, model version 1, image digest, one worker, and 0.5 vCPU / 1 GiB. I temporarily allowed only the approved client IP. After startup, both readiness checks passed before k6 started at `13:08:13Z`. One ready/running replica with zero restarts was confirmed before and after measurement; no other revision had replicas.

**Result:** 1,794 completed predictions in 60.325 seconds, **29.74 requests/second**, and zero errors, timeouts, or unfinished requests. HTTP p50 was **326.30 ms**, p95 **487.45 ms**, and p99 **587.26 ms**. The original **p95 < 200 ms target still failed**. k6 exited `99` for that threshold only; there was no retry.

All 1,794 requests had valid paired timing records and unique request IDs. Counts and recomputed percentiles matched the native k6 metrics.

| Measured interval | Median (ms) | p95 (ms) |
|---|---:|---:|
| HTTP request duration | 326.30 | 487.45 |
| Server processing, including the two rows below | 207.81 | 327.32 |
| Data preparation and model scoring | 122.93 | 287.02 |
| JSON decoding | 0.0118 | 0.0258 |

**What this tells me:** JSON decoding was small for most requests. Server processing itself exceeded 200 ms in **983 of 1,794** requests, so these slow responses cannot be explained by network delay alone. Data preparation/scoring is a useful place to investigate next, but its elapsed time does not prove CPU throttling or thread contention.

I also subtracted intervals **within each request**, not separate percentiles. The remaining time inside processing had a median of **44.04 ms**; HTTP time outside processing had a median of **115.46 ms**. The first does not isolate queueing, and the second is not network-only time. A single round with per-request logging does not establish repeatability or prove a regression against the earlier uninstrumented run.

**Runtime warning:** k6 ignored the top-level `gracefulStop` option and used its 30-second default, not the intended 15 seconds. The actual 60-second round completed in 60.325 seconds with no unfinished requests. I kept the warning in the evidence and did not change settings or run another round.

**Post-audit fix (2026-09-24):** I removed the unsupported option and kept k6's default 30-second grace period. All 264 offline checks passed, and `k6 inspect` read the 1/10/50-user configurations without warnings. The raw results are unchanged; I did not run another Azure load test.

**Cleanup:** stop was confirmed at `13:09:27Z`; the exact original configuration and allow-list were restored at `13:09:43Z`. The controller finished in about 163 seconds. An independent read at `13:10:28Z` confirmed `Stopped / Succeeded`, zero replicas for every revision, and no retained replica records. All 38 older evidence files and the preflight-protected source, private configuration, and README stayed unchanged.

Evidence: [native summary](loadtest/run-20260923T130813Z-vus10-om7CQV/summary.json), [paired request records and runtime warning](loadtest/run-20260923T130813Z-vus10-om7CQV/console.log), [controller snapshots and cleanup](loadtest/run-20260923T130813Z-vus10-om7CQV/azure-state.json), and [timing audit](loadtest/run-20260923T130813Z-vus10-om7CQV/timing-analysis.json). Only this round's evidence and report entry were added. No service/model change, image build, dependency installation, Git commit, or push was made. This completes the diagnostic round, not the latency requirement or Lab 3.

## Local concurrency check — original versus one scoring call at a time

On 2026-09-23, I ran one local pair to check whether overlapping model calculations were adding delay under the small CPU limit. Both rounds used the same serving image, registered model version 1, **0.5 vCPU / 1 GiB**, one worker, the existing 40-token thread pool, and the same 120-byte payload. Each round had five warm-up requests, then **10 users for 60 seconds**. The original mode ran first.

The temporary wrapper kept the service code unchanged. In the second mode, a lock let only one request calculate with the model at a time. Waiting for that lock was still included in HTTP latency and in `scoring_ms`; these are elapsed times, not pure model CPU time. Both modes used the same instrumentation. The model was downloaded once before testing and its five artifact hashes matched the saved Lab 2 model. Measured requests used local loopback only, without the home internet or Azure request path.

| Local mode | Completed requests | HTTP p50 (ms) | HTTP p95 (ms) | HTTP p99 (ms) | Requests/second | Most overlapping score calls |
|---|---:|---:|---:|---:|---:|---:|
| Original | 2,149 | 291.15 | 389.51 | 407.23 | 35.75 | 10 |
| One scoring call at a time | 3,092 | 196.57 | 280.60 | 310.02 | 51.43 | 1 |

**Result:** the second mode had about **28% lower p95** and **44% higher throughput** in this pair, but **both still failed p95 < 200 ms**. Both k6 exits were `99` for that latency threshold only. All **5,241** measured predictions matched the reference probability for this payload within `1e-9`; there were no errors, timeouts, or unfinished requests. Request counts, unique IDs, server call counts, and recomputed percentiles matched the native results. Each mode loaded the model once.

Both local service containers used about 30 CPU-seconds during their approximately 60-second measurement windows, and CPU throttling occurred in 602 of 605 and 602 of 606 enforcement periods. This supports CPU quota pressure in this local setup. These counters include the whole service and diagnostic probe overhead; `throttled_usec` is not a wall-time percentage.

**What I can conclude:** reducing overlapping scoring calls helped in this local pair. Internet delay is not required for this service to exceed 200 ms. This does not measure how much the home internet affected Azure, prove the exact scheduling cause, or show that the same change will pass on Azure. It is one fixed-order pair, not a repeatability test, and prediction agreement covers this load-test payload only. The deployed service has not been changed.

**Preparation issue:** before either measured round, the warm-up payload assertion failed because the Python literal used `68.0` and `55.0` instead of the JavaScript workload's `68` and `55`. I corrected only that temporary literal to 120 bytes. No k6 requests ran during that failed preparation. The inherited `gracefulStop` warning also remained; both actual rounds finished without unfinished requests.

**Cleanup and scope:** all experiment containers were removed. I deleted only the downloaded temporary model copy and the empty preparation directory; the registered model is unchanged and can be downloaded again. All 82 pre-existing protected files were unchanged before appending this entry. No Azure app was started, and no production source, README, dependency, model, image, Git commit, or remote repository was changed. The latency requirement remains open.

Evidence: [original native summary](loadtest/local-concurrency-20260923T132735Z/original-measured/summary.json), [serialized native summary](loadtest/local-concurrency-20260923T132735Z/serialized-measured/summary.json), [comparison and raw-file hashes](loadtest/local-concurrency-20260923T132735Z/comparison.json), and [model identity, controller observations, and cleanup](loadtest/local-concurrency-20260923T132735Z/experiment.json). Each result folder also contains the per-request `console.log` and `runtime.log`; the three temporary harness scripts are retained beside them as method evidence.

## Task 4 preparation — canary tooling checked offline (2026-09-24)

This is preparation, not a completed canary or rollback experiment. I added the host-side [canary command](../scripts/canary.py), scoped operations in the existing [Azure adapter](../cloudlayer/azure.py), and offline tests. The existing API, `deploy()` implementation, dependency files and README were not changed in this step. No model was registered or downloaded, no image was built, and no Azure resource was started or updated.

The command needs an explicit `--execute` flag and separately registered versions linked to the two reviewed Lab 2 runs. It checks the stopped app, baseline revision, pinned image and 0.5 CPU / 1 GiB limits before preparing a candidate with zero traffic. It then uses explicit revision names for 90/10 routing at the main app URL. The candidate copies the baseline template; only its model version and revision suffix change. Readiness is limited to five minutes. The measurement stops at 600 batch requests or the five-minute check, with bounded HTTP timeouts; these are experiment limits, not a guaranteed cloud-billing cap.

Quality detection uses blinded A/B ROC-AUC on the same 1,200 unique validation rows per group. Repeated rows do not increase that count. The 0.01 gap is our approved drill threshold, not a course requirement or a statistical-significance claim. Labels stay on the client. Incomplete results and an unexpected worse baseline cannot be reported as a successful candidate-degradation drill. The command records request IDs, scores, timestamps, actual request/prediction counts and the decision before revealing the group mapping.

Rollback evidence separates the traffic configuration readback from 50 observed baseline requests, with a one-minute observation limit. That finite window is not proof about every future request. Cleanup attempts baseline-only routing, candidate deactivation and app stop even if an earlier stage fails. It must confirm stopped status and zero replicas, or report cleanup as unconfirmed. It leaves Multiple mode with the baseline explicitly pinned; switching to Single mode while the candidate is latest could choose the wrong revision. A hard process kill or lost cloud access still requires manual recovery using the revision names in the saved preflight receipt.

**Checks run:** `unshare -Urn make canary-check` passed all 37 focused tests. `unshare -Urn make test portability-audit` passed all 208 tests and the portability audit, with networking disabled. Tests cover invalid preflight states, empty REST responses, explicit traffic weights, blind scoring, duplicate/incomplete data, request/time limits, partial preparation failure, rollback observations and cleanup failure. `make canary-help` also worked offline. The local dataset check found 1,200 unique validation rows and 121 positives. Hash checks preserved the existing service/source/dependency/README files, and AST comparison confirmed all 13 original Azure adapter methods were unchanged. Existing dependency deprecation warnings remain. Ruff is not installed in this environment, so no Ruff check was claimed and no dependency was added.

**Preparation issue:** the two new test files initially shared a basename, which pytest rejected during collection. I renamed the provider-specific file to `test_azure_canary.py`; the focused and full suites then passed. This did not run a cloud experiment.

**Still unverified:** real candidate loading, live 90/10 routing, the actual AUC gap, detection time and live rollback. Candidate registration and the live drill need their own approved step. Task 3's latency target also remains open.

REST contracts were checked against Microsoft's API version 2025-07-01: [app update](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/container-apps/update?view=rest-resource-manager-containerapps-2025-07-01), [app start](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/container-apps/start?view=rest-resource-manager-containerapps-2025-07-01), and [revision deactivation](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/container-apps-revisions/deactivate-revision?view=rest-resource-manager-containerapps-2025-07-01). These document the API contract; they are not evidence that this drill has run on Azure.

## Task 4 preparation — candidate registered, not deployed (2026-09-24)

I registered the reviewed Lab 2 candidate once using the existing Lab 2 adapter. The Registry now has `models:/itcs355-u6688124/2`, with status `READY` and stage `None`. It points to run `1730032f-4352-4781-99dc-687b35f1778c`. The source run was `FINISHED`, all five MLflow model files were listed, and its Git/DVC/image lineage matched the original training job. All eight lineage tags were checked after registration. No duplicate candidate version was created.

Version 1 remains `READY` / `Staging`, with the same run and lineage tags. The app remained `Stopped` in Single mode, with unchanged traffic and four revisions at zero replicas. The complete app/revision snapshot hash matched before and after. All 163 checked existing Lab 2/3 files were unchanged before adding this evidence. No training, model download, image build, app start, deployment, stage transition, README edit, commit or push was performed.

The candidate's recorded Lab 2 validation ROC-AUC is `0.8268292496112868`, versus `0.8444994217173845` for the baseline. These are existing training-run metrics, not new canary results. Registration is complete; loading the candidate in the serving image and the live 90/10 detection/rollback drill remain unverified and require the next approved step. Task 3's latency target is still open.

Evidence: [timestamped registration and before/after checks](canary/candidate-registration-20260924T030441Z.json). Registration returned version 2 at `2026-09-24T03:05:05Z`; Registry readback was verified at `03:05:10Z`, and the stopped app was checked again at `03:05:58Z` (UTC).

## Task 4 live preflight — stopped at the IP check (2026-09-24)

The local validation-data check passed: 1,200 unique rows and 121 positives. The current client IP did not match the locally configured allowlist. A read-only Azure check at `2026-09-24T03:12:56Z` confirmed that the current IP was not allowed there either. The single-host Azure rule also differed from the local configuration. I did not change either value or broaden access.

The live canary experiment did not start. No candidate revision was created and no prediction request, app start or traffic update was sent. The app was still `Stopped`, in Single mode, with its original traffic configuration and all four revisions at zero replicas. This is an access/preflight blocker, not a failed quality or rollback measurement. Updating the narrowly scoped client-IP rule and matching local configuration needs separate approval before trying the preflight again.

Evidence: [sanitized preflight and Azure readback](canary/preflight-blocked-20260924T031256Z.json). Public IP values are omitted.

## Task 4 preflight — IP rule aligned (2026-09-24)

With approval, I replaced only the IP in the existing Azure Allow rule with the current client's `/32` address and updated `SERVING_ALLOWED_IP` in the local, Git-ignored `cloud.env`. The rule still allows one host. Its name/action, other app properties, identity, traffic and revision states were unchanged. There was no inherited environment override, and all other lines in `cloud.env` were preserved.

The read-only preflight passed at `2026-09-24T03:23:32Z`: the current IP matched both configurations; the validation data had 1,200 unique rows and 121 positives; versions 1 and 2 were `READY` and linked to the expected runs; and the candidate plan reused the pinned image with 0.5 CPU, 1 GiB and at most one replica per revision. All 165 checked existing Git-visible Lab 2/3 files were unchanged before adding this evidence.

The earlier IP blocker is resolved for the current connection. The app remains `Stopped` in Single mode, with unchanged traffic and all four revisions at zero replicas. No serving request, candidate deployment or live canary run was made in this step. Registry/configuration checks do not prove model readiness or inference; those still need the live drill.

Evidence: [IP update confirmation and read-only preflight](canary/ip-reconciliation-20260924T032151Z.json). Actual IP values are omitted.

## Task 4 live drill — degradation detected and traffic rolled back (2026-09-24)

I ran one 90/10 canary drill using the existing serving image and registered versions 1 and 2. Both revisions used 0.5 CPU, 1 GiB and a maximum of one replica. The requests went through the main app URL; Azure chose the revision. The detector used blinded groups A/B and only revealed their version mapping after saving its decision.

After 205 batch requests, both groups had predictions for the same 1,200 unique validation rows (121 positives). A received 183 requests and B received 22: the observed split was 89.27% / 10.73%, not exactly the configured weights. Repeated rows did not count as new samples. A's ROC-AUC was `0.8444994217173845`, and B's was `0.8268292496112868`. The gap, `0.01767017210609767`, exceeded our pre-set `0.01` drill threshold. Only then was B identified as candidate version 2. A separate pairwise calculation from the saved probabilities reproduced both AUCs within `1e-12`.

Detection took **33.73 seconds after the 90/10 configuration was confirmed**, or **138.92 seconds from the start of deployment preparation**. The second clock includes preparation/readiness; it is not inference time. The candidate served 2,200 predictions in the measured replay, excluding readiness/warm-up scoring.

Rollback was requested immediately after the decision. Azure confirmed baseline-only traffic **15.10 seconds after detection**. All 50 subsequent observation requests returned version 1, and that observation window finished **21.98 seconds after detection**. This is configuration readback plus real request evidence, not just a successful command.

| Event | UTC timestamp |
| --- | --- |
| Deployment preparation began | 03:32:14.197968 |
| Both revisions ready | 03:33:44.739789 |
| 90/10 split confirmed | 03:33:59.379124 |
| Blind degradation decision | 03:34:33.107778 |
| Baseline-only traffic confirmed | 03:34:48.206639 |
| 50 baseline requests observed | 03:34:55.089260 |

**Cleanup obstacle and resolution:** the command exited with `cleanup_unconfirmed` and `stop_or_check:TimeoutExpired`. The app was stopped with zero replicas, but the candidate still showed `active`. I preserved that original result, then deactivated only this candidate once more while the app remained stopped. At `03:39:49Z`, it was confirmed inactive. An independent check at `03:41:26Z` confirmed the app was `Stopped`, all five revisions had zero replicas, and traffic was explicitly pinned to the baseline at 100%. Multiple mode was retained. This was cleanup of the same drill, not a second experiment or an app restart. The reason the first deactivation did not remain reflected in the final state has not been established.

All 166 checked existing Lab 2/3 files were unchanged before documenting the result. No source/API/README changes, training, model registration, image build, dependency change, commit or push occurred. The non-traffic ingress configuration, identity and other protected app properties also matched the preflight hashes.

Evidence: [native preflight](canary/drill-20260924T033116Z/preflight.json), [timestamped requests and decisions](canary/drill-20260924T033116Z/events.jsonl), [original summary including the cleanup timeout](canary/drill-20260924T033116Z/summary.json), and [independent score check, cloud observations and cleanup reconciliation](canary/drill-20260924T033116Z/verification.json).

The quality-detection and rollback experiment is verified, with the cleanup issue resolved separately. This remains a fixed labeled replay, not a significance or future-data claim. Task 3's latency target is still open. The approved five-line explanation and cleanup obstacle are now recorded in the [Task 4 README evidence](../README.md#task-4--canary-and-rollback).
