# Lab 4 Evidence

## Task 1 — Tests that can actually fail

I reused the Lab 3 serving code and the existing dataset. I added tests without training a new model.

- **Unit tests:** check that feature values and column order stay correct. A missing feature must fail before reaching the model. These tests use a mock and run offline.
- **Data tests:** check the dataset contracts below. I also kept the split tests, including the check that no machine appears across train, validation and test sets.
- **Model behaviour tests:** load `models:/itcs355-u6688124/1` and check valid probabilities, a low-risk prediction for the instructor’s healthy-machine example, and prediction latency.
- **Integration test:** start the built container, wait for `/ready`, then call `/predict`. It must return HTTP `200`, a probability between 0 and 1, and model version `"1"` in both the JSON body and response header.

### What the data contracts would catch

These are examples of problems the tests would catch, not incidents observed in production.

| Data contract | Problem it would catch |
|---|---|
| Expected columns and types | An upstream export drops `temp_c`, adds an unexpected column, or changes a column’s type. |
| No nulls in required columns — allowed null rate: 0% | A failed sensor or incomplete join leaves missing feature values. |
| Features stay within plausible ranges | A producer sends an impossible value, such as `load_pct = 250`. |
| Labels are 0/1, with positive rate strictly between 1% and 99% | A label export uses `2` for failures or accidentally writes every label as `0`. |
| Unique `reading_id` | An ingestion retry adds the same readings twice. |

I tested eight deliberately corrupted copies to confirm that the contract checks reject bad data. The original CSV was not changed. These are local checks, not the failing-PR evidence required in Task 3.

### Checks

Local checks used the existing Lab 3 Python environment. Registry-backed tests also required serving configuration and cloud login.

- **20 offline tests passed**, including the leakage test and eight bad-data cases.
- **3 model behaviour tests passed.** The healthy-machine example scored `0.0`. Mean warm prediction time was `33.349 ms`, below the `200 ms` limit.
- **1 integration test passed** using `itcs355-lab4:task14-local-20260929`.
- `make portability-audit` passed.

The latency limit reuses Lab 3’s target as a loose model-only check. It measures mean warm inference time, not API p95.

### Problem and fix

The local container does not share my host’s cloud login. I passed a short-lived token through stdin without putting it in the image or Git. This setup is for the local test; it does not establish CI authentication.

The test removed its container afterwards. The registered model and original training run stayed unchanged.

## Task 2 — CI pipeline

I added a GitHub Actions workflow for pull requests and pushes to main. It scans the full Git history, runs lint and tests, builds the image, then tests that image before publishing it.

Only a successful run on main can push the image to ACR and deploy to staging. The image uses the commit SHA as its tag and is deployed by digest. Azure login uses OIDC instead of a saved cloud password.

### Checks

[CI run for commit `bb38fe7`](https://github.com/nithit-cypherX/mlops-engineering/actions/runs/36961138955) passed:

- Secret scan, lint and portability audit passed.
- 166 unit/data tests, 3 model behaviour tests and 1 integration test passed.
- Staging used the digest pushed by this run. All three smoke payloads passed with model version `"1"`.
- Cleanup removed the runner’s temporary IP rule. I checked Azure again and confirmed that only my configured IP remained allowed.

### Problems and fixes

The readiness check depended on `latestReadyRevisionName`, which Azure did not return. I changed it to check the revision directly, including its image digest and model version, and gave CI permission to read revisions.

CI also hit a GET timeout. The code now retries readiness GETs within the same 600-second limit without deploying again. The cause of the earlier slow GET is still unknown.

Cleanup had another problem: Azure accepted the PATCH but returned an empty response, which caused a JSON error. I handled the empty response and kept the GET check that confirms the runner’s IP rule was removed.

## Task 3 — Prove a bad commit is blocked

I opened [PR #1](https://github.com/nithit-cypherX/mlops-engineering/pull/1) with one deliberate change: the data loader returned `load_pct=250`, above the allowed maximum of `100`. I kept the original CSV and tests unchanged.

### Checks

[CI run for commit `7896619`](https://github.com/nithit-cypherX/mlops-engineering/actions/runs/36964236442) failed as expected:

- `tests/test_data.py::test_features_within_plausible_ranges` caught the bad value.
- Error: `AssertionError: load_pct above plausible ceiling: 250.0`.
- Data tests: **1 failed, 17 passed**. Unit tests: **148 passed**.
- The PR's model, image and integration job was skipped. No image was pushed and no deployment ran.

I closed the PR without merging. The bad change did not enter `main`.
