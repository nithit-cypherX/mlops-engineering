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

### Managed MLflow trade-off

I used Azure ML's managed MLflow, so I did not need to run my own tracking server.
The trade-off is that accessing my runs and registered model depends on Azure
login and workspace access. Moving to another cloud would also mean moving the
saved tracking data and model artifacts.

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

## Task 2 — Run a budgeted study

I ran 12 configurations on Azure ML using three hyperparameters:

- `n_estimators`: 100, 300
- `max_depth`: 4, 8, 12
- `min_samples_leaf`: 1, 10

All trials used the same data split and `seed=20260101`. The study had a shared
budget of **20 THB**, within the **150 THB lab budget**.

### Results

All **12 trials finished**. Each trial saved its hyperparameters, separate
validation and test metrics, duration, instance type, estimated cost, and model
artifacts in MLflow. The Git SHA, data fingerprint, DVC hash, and image digest were
also recorded.

Compute returned to **0 nodes** after both jobs.

### Interruption and resume

I made the first job stop after four successful trials and checked that their
run IDs were saved in Blob Storage.

The resumed job skipped those four trials and ran only the remaining eight. The
final checkpoint contained 12 unique configurations. The original checkpoint
stayed unchanged, and the estimated cost continued from the previous total.

This tested a planned stop **between trials**, not a real Spot eviction or a stop
during model training.

- Interrupted job: `lab2-02d45592b6d34040a3d9516a8e6859c2`
- Resumed job: `lab2-d4f87c7c872c42c8bbca1b179e554b3f`
- Code commit: `610e7cfefbfe080d711a7f621e1d6992fa803ad3`

Both jobs used this image:

```text
itcs355u6688124.azurecr.io/itcs355-lab2@sha256:df878435dd990605f1c63dc4538b9cb86dacd31384d0a730c6b2d1c0399b282d
```

### Problems and limits

- **MLflow tags:** An earlier attempt failed before the first trial because
  row-count tags were integers. I changed them to strings and added a regression
  test. The new image then ran successfully on Azure.
- **Discounted compute:** My Low-priority quota request was unsuccessful, so I
  used Dedicated `Standard_F2s_v2` in `malaysiawest`. This does **not** meet the
  discounted-compute requirement.
- **Cost:** The estimated cost for all 12 trials was **0.0736 THB**. This excludes
  VM startup, idle time, and other services. Earlier bill checks returned `429`.
  A later check succeeded; see [Cost](#cost) for the reported amount and limits.

## Task 3 — Compare and justify

### Model selection justification

I chose Random Forest with 100 trees, max_depth=12 and min_samples_leaf=10.
In the original fixed-seed comparison, validation ROC-AUC was 0.84450 versus
0.84506 for 300 trees. I accept this observed score drop for a smaller model
and faster batch predictions, rather than tiny estimated cost savings.

The saved model was 1.53 MB versus 4.59 MB. In the same local container,
50 alternating measurements after warm-up gave median prediction times of
34.55 ms versus 86.72 ms per 1,200 validation rows. These are not Azure
endpoint timings.

Across five model seeds on a fixed split, mean validation ROC-AUC was
0.83964, with sample variance 0.00001414 and standard deviation 0.00376.
This does not establish equal quality to 300 trees.

The recorded Azure trial compute estimate was 0.00539 THB. At one retrain
per month with the same data, settings and rate, this component is about
0.00539 THB/month, excluding VM startup, idle time, storage and other services.

This choice could be wrong if 300 trees performs more consistently across
seeds. I have only checked multiple seeds for the 100-tree model.

### Selected model and evidence

Selected MLflow run: `8087ae5c-d06f-411e-b6b0-7f7891ffe7ed`
with `seed=20260101`.

- [Comparison of 12 trials](reports/lab2-comparison.md)
- [Five-seed results](reports/lab2-seed-check.json)
- [Model size and prediction latency](reports/lab2-model-benchmark.json)

## Task 4 — Register the model with lineage

I implemented `register_model()` in the Azure adapter and registered the model
chosen in Task 3. I used the saved model from the original run without training it
again.

- Model: `models:/itcs355-u6688124/1`
- Status: `READY`
- Stage: changed from `None` to `Staging`, verified on 2026-09-19.

### Lineage and checks

The model version contains all eight required lineage tags. The validation and
test scores come from the original run, not the five-seed averages.

<details>
<summary>Show the eight lineage tags</summary>

```text
git_commit=610e7cfefbfe080d711a7f621e1d6992fa803ad3
data_version=1c886b512c8a5c9bf723da1cd119fc80.dir
mlflow_run_id=8087ae5c-d06f-411e-b6b0-7f7891ffe7ed
training_job_id=lab2-d4f87c7c872c42c8bbca1b179e554b3f
image_digest=sha256:df878435dd990605f1c63dc4538b9cb86dacd31384d0a730c6b2d1c0399b282d
seed=20260101
metric_val=0.8444994217173845
metric_test=0.8466731943553135
```

Both metrics are ROC-AUC.

</details>

All **400 Lab 2 tests** passed, including 34 registration tests. Lint and the
portability audit also passed.

After moving the model to Staging, I read it back and checked that its lineage,
run ID and artifact source stayed unchanged. It was still version `1`; no new
version or endpoint was created.

### Who should be allowed to promote it?

In a real team, I would require approval from a designated model reviewer before
an authorised MLOps engineer promotes the model. Being able to train a model
should not automatically give someone permission to promote it.

Before approval, they should check the model comparison and selection
justification, validation results, complete lineage, passing tests, and known
performance and cost limits. Staging is a step before production, not permission
to deploy directly.

### Problem and fix

MLflow’s model-list command failed because it added a filter that Azure did not
support. I checked the registry using the Azure SDK and direct model lookup
instead. No dependency changes were needed.

## Task 5 — Prove you can reload it

I adapted the instructor’s `reload_check.py` and used it to load
`models:/itcs355-u6688124/1` from the Azure ML registry. It downloaded the saved
model without training again or using an old local model.

I used the same dataset and split seed (`20260101`). The five rows came from the
test set. Their machine IDs did not overlap with the training or validation sets.

### Command and result

From `lab/lab2/`, using the existing environment and Azure login:

```bash
.venv/bin/python scripts/reload_check.py \
  --name itcs355-u6688124 --version 1
```

Verified on 2026-09-19:

```text
loading models:/itcs355-u6688124/1
  reading 125: p(failure)=0.0109
  reading 126: p(failure)=0.0148
  reading 127: p(failure)=0.0169
  reading 128: p(failure)=0.0449
  reading 129: p(failure)=0.0013

PASS  model reloaded from the registry and scored five held-out rows
```

### Checks

The command finished successfully, and all five predictions were valid
probabilities. All **433 Lab 2 tests** passed, including 33 reload-check tests.
Lint and the portability audit also passed.

This shows that the registered model can be downloaded, loaded and used for
prediction. Scoring five rows is a reload check, not a new accuracy evaluation.

## Cost

On 19 September 2026, `make cost-report` succeeded after earlier checks returned
HTTP 429. Azure reported USD 0.46337713, about THB 15.24, for 17–19 September.
This includes all reported shared ACR and Storage costs in that window. The
reported amount is below THB 150, but billing updates can arrive later.

See the [cost report](reports/lab2-cost.md) for the breakdown and THB reference.

## Teardown

I ran `make teardown` and confirmed that `cpu-lab2` was deleted. The four jobs,
registered model and artifact listing stayed unchanged. I kept the jobs to
preserve the existing run evidence and artifact references, so this differs from
the handout's instruction to delete both jobs and compute. Training on this
compute again would require recreating and configuring it.
