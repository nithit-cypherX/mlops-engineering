# Lab 2 — Experiment Tracking and Model Registry

## Course materials and starting point

This lab follows the instructor's [Lab 2 handout](https://github.com/pasdptt/public_teaching_mlaiops/blob/30ba98b02e8f233c7d7a36d584738ea442e46bd1/course/labs/lab-02-tracking-and-registry.md).
The starting code comes from [my Lab 1](../lab1/) at commit
`931fb033bc8ff0984bf42ed8e8c7c8f1e7eed47d`. Lab 1 credits the original instructor starter.

## Task 1 — Move training onto managed compute

I added `submit_training()` and `wait_training()` to the Azure adapter and ran one
training job with `make train-remote`.

The job used a digest-pinned image from ACR. It read the dataset and DVC metadata
from Blob Storage, then saved `model/` and `metrics.json` back to the same storage.
No local source folder or dataset was uploaded with the job.

### Results

Verified on 2026-09-18:

- Azure job: `Completed`. MLflow run: `FINISHED`.
- Parameters: `n_estimators=200`, `max_depth=8`, `min_samples_leaf=5`, `seed=20260101`.
- Validation ROC-AUC: **0.8364**. Test ROC-AUC: **0.8482**.
- The parameters matched the submitted settings. Metrics in MLflow matched
  `metrics.json` in Blob Storage.
- Compute scaled back to **0 nodes** after the job.

Job ID and MLflow run ID:
`lab2-791151e70ccb4b01918f32a1ed6bd2af`

Image:

```text
itcs355u6688124.azurecr.io/itcs355-lab2@sha256:5e1b0ac4e2ab65a57de7460f42d78bc849a1ee9ca2c233c5691c0ecfc2b79f63
```

### Problems and fixes

- **Image dependencies:** The base image and locked packages left duplicate package
  metadata. I removed the conflicting base packages before copying the locked
  replacements. `pip check` then passed.
- **Datastore check:** The adapter checked for `"azure_blob"`, but the SDK returned
  `"AzureBlob"`. I changed the check to use `AzureBlobDatastore` directly and updated
  the tests to use real SDK datastore objects.
- The successful job did not hit a permission error, so no extra permissions were
  added during this run.

### Checks

All **101 Lab 2 tests** passed. Lint passed for the two files changed in the
datastore fix, and the portability audit passed.

### Compute obstacle

My Low-priority quota request was unsuccessful, so I used Dedicated compute with
Azure for Students credits. This does **not** meet Task 2's discounted-compute
requirement. Credits still count toward the **150 THB lab budget**; they do not
make the reported cost zero.
