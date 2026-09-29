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
