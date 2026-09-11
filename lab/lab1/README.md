# Lab 1 Evidence

[Lab handout](../../materials/upstream/course/labs/lab-01-reproducible-training.md) · [Course overview](../../materials/upstream/course/README.md) · [Portability reference](../../materials/upstream/course/reference/cloud-portability-reference.md)

Starter code: [instructor repository](https://github.com/pasdptt/public_teaching_mlaiops/tree/2018ce996fe2da4602761977a5555cf347d10b7b).

## Task 1 — Structure the project

I used the instructor's starter code in `lab/lab1/`. The training code is in `src/`, tests are in `tests/`, and cloud adapters are in `cloudlayer/`.

`src/config.py` reads settings from `cloud.env` and builds data paths from the lab directory. I checked `src/` and found no hardcoded bucket names, provider hostnames, or absolute paths from my machine. The code does not import from `notebooks/`.

**Checks:** `make portability-audit` passed. Git ignores `cloud.env`, and I did not find the file at its current path in local Git history.

**Problem and fix:** The first audit failed with `python: command not found`. I had not activated the lab environment. I ran `source .venv/bin/activate` and tried the audit again. It passed.

## Task 2 — Pin the environment properly

I used `uv pip compile --generate-hashes` to create `requirements.txt`. I kept the original 10 direct dependencies. The lock file contains 95 packages, including their dependencies.

I pinned both base images in the Dockerfile to the same digest and added `--require-hashes` to the install command.

The starter already used seeds for Python, NumPy, data splitting and Random Forest. I kept this and added `python_hash_seed` to MLflow alongside `seed`.

**Checks:** Installation with hashes passed in a separate environment, and `pip check` found no conflicts. The Docker image built for `linux/amd64`. All eight seed tests and two seed-related split tests passed. I checked MLflow logging with a mock model and temporary storage. I have not trained a real model yet.

**Problem and fix:** The starter set `PYTHONHASHSEED` after Python had already started, so it did not affect that process. I moved the setting to Makefile and Docker so it is set before Python starts. Training now stops if it does not match `--seed`.

### Reproducibility trade-off

If I had to skip one thing under time pressure, I would skip base-image digest pinning first. I would keep hashed dependencies and seeds so the Python packages and random choices stay the same. The risk is that the image tag could point to a newer image with different Python or system libraries. This could change the result even with the same packages and seeds. I would add the digest back before submitting. In this lab, I kept all three.

## Task 3 — Containerise the training job

I kept the starter's multi-stage Dockerfile and non-root user. I updated the Makefile so the container uses my Ubuntu UID/GID and runs from `/app/reports`. This lets it save the MLflow database and artifacts outside the container.

I trained one Random Forest model with the starter settings. The validation ROC-AUC was 0.8364 and the test ROC-AUC was 0.8482. I loaded the saved model in a new container and checked that its predictions gave the same metrics without training again.

I implemented `push_image` in the Azure adapter using `az acr login`, `docker tag` and `docker push`. Then I pushed the image to ACR with:

```bash
make image-push TAG=task33-20260909
```

### Checks

- All 10 data tests and four image-push tests passed.
- The image uses `linux/amd64` and runs as a non-root user.
- The image digest on ACR matched the one from the push. I also ran the image by digest with `--help`, and it worked.
- I checked all 10 image layers and the image metadata for credentials. I found no personal credentials. The flagged matches came from library examples or compiled code.

### Problem and fix

The container could read the data, but it could not save the output because of folder permissions.

I fixed this by running the container with my Ubuntu user and group IDs (`UID/GID`). I also set its working folder to `/app/reports`, so the MLflow database and saved model stay available after the container stops.

### Image reference

```text
itcs355u6688124.azurecr.io/itcs355@sha256:4a0b592dfe6cea2618d886e603444fd429271fd8d965ad44ef545e8415b4fc6c
```

ACR access requires authentication. This image contains the training code and dependencies, not the trained model from the test run.

## Task 4 — Version the data

I added `dvc[azure]` and updated the hashed lock file without changing the existing package versions.

I used `dvc init --subdir` to keep DVC inside Lab 1. I tracked `data/raw` and pushed it to Azure Blob Storage. Git ignores the CSV, and the DVC config contains no credentials.

### Checks

- `dvc push` passed, and `dvc status -c` confirmed that the cache and remote were in sync.
- I tested `dvc pull` in a separate folder with no data or existing cache, using my Azure account. The downloaded CSV matched the original byte-for-byte.
- I kept the starter's split code and leakage test. The pipeline splits by `machine_id`, so readings from one machine stay together.
- All 10 data tests passed. Extra checks with three seeds confirmed that each seed gave the same split when repeated, with no missing or duplicated rows.
- I also simulated machine leakage between each pair of splits. The existing test caught all three cases. These extra checks were separate from the saved test suite.

### Data version

```text
1c886b512c8a5c9bf723da1cd119fc80.dir
```

No changes to the split code were needed.

## Task 5 — Track five runs

I trained five Random Forest models using the same dataset and seed (`20260101`). I used the starter settings as the baseline and changed one hyperparameter at a time.

I added DVC hash logging and passed the Git SHA into the Docker image at build time. I also used `model.get_params()` to log all model settings, including defaults.

### Results

| Run | Trees | Max depth | Min samples leaf | Validation ROC-AUC | Test ROC-AUC |
|---|---:|---:|---:|---:|---:|
| Baseline | 200 | 8 | 5 | 0.8364 | 0.8482 |
| Fewer trees | 100 | 8 | 5 | 0.8397 | 0.8466 |
| More trees | 300 | 8 | 5 | 0.8377 | 0.8491 |
| Shallower trees | 200 | 4 | 5 | 0.8405 | 0.8543 |
| Larger leaves | 200 | 8 | 10 | 0.8417 | 0.8491 |

I used validation ROC-AUC to compare the runs. Increasing `min_samples_leaf` to 10 gave the highest score among these five runs. The improvement over the baseline was small, so I would not claim that this setting is always better. I did not use test scores to choose the settings.

### Checks

- All five runs finished successfully in MLflow.
- Each run recorded all model settings, the seed, validation/test metrics, DVC hash, Git SHA and a model artifact.
- I loaded all five saved models in a new container without training again. Their ROC-AUC and Average Precision matched the logged metrics within `1e-12`.
- MLflow showed warnings about the container username and automatic Git detection. I checked that our `git_commit` tag still contained the correct SHA in every run.

### Run references

Experiment: `task53-five-runs`

Git commit:

```text
d20ed98d88d1faaa5a0669d23647f281b3daaee0
```

DVC data version:

```text
1c886b512c8a5c9bf723da1cd119fc80.dir
```

Local results: `reports/task53-AU71uE/`

## Task 6 — Reproduce the result

### Problem and data

The goal is to predict whether a machine will fail within seven days using sensor readings.

The dataset is synthetic, created with the instructor’s `scripts/make_dataset.py`. It contains 6,000 readings from 240 machines. I stored this version in DVC on Azure Blob Storage. The command below downloads that data instead of generating it again.

### Run it yourself

You need Git, Make, Bash and Docker with Linux containers. On Windows, use Ubuntu in WSL with Docker Desktop integration enabled. You do not need Python, DVC or an Azure account on your machine.

Clone the repo and open the lab folder:

```bash
git clone https://github.com/nithit-cypherX/mlops-engineering.git
cd mlops-engineering/lab/lab1
```

Then run:

```bash
make reproduce
```

This builds the image, downloads the data, trains the model and checks the score. It uses the settings selected in Task 5: 200 trees, max depth 8, min samples leaf 10 and seed `20260101`.

```text
expected test_roc_auc: 0.8491 +/- 0.001
```

The command should finish with `PASS reproduced within tolerance`. Metrics are saved in `reports/metrics.json`. The MLflow database and model artifacts also stay in `reports/`.

To check the score again without retraining:

```bash
make verify
```

### Checks

I tested the flow in a separate folder with no dataset or previous training results. The downloaded data matched the original byte-for-byte. Training gave a test ROC-AUC of `0.8491045378`, and verification passed.

The test took about 20 seconds with Docker build cache available. The first run will take longer because Docker needs to download the base image and install dependencies.

### Problem and fix

The Azure data was private, so someone else could not download it without logging in. I enabled public read and listing on the data container because DVC needs both. Anonymous users can download the data, but cannot upload, change or delete files.

DVC also failed because the Ubuntu UID used by the container had no username inside the image. I supplied a username for the download step while keeping the container non-root.

## Checklist before you submit

Checked items below include two limitations I accepted, not a waiver from the instructor.

- [x] `make reproduce` passed from a fresh clone of a local snapshot. **Accepted limitation:** this was on my own machine with Docker build cache, not someone else's machine.
- [x] `make verify` passed against the README claim: `0.8491 +/- 0.001`.
- [x] `make test` — all 34 tests passed. The four Azure push tests also passed separately.
- [x] `make portability-audit` — clean.
- [x] The Task 3 image builds for `linux/amd64`, was pushed to ACR, and has a digest reference above. It is the Task 3 version; `make reproduce` builds the latest code locally.
- [x] `dvc push` completed, and anonymous data download passed through `make reproduce`. The download script sets up anonymous access for DVC.
- [x] Five tracked runs have params, metrics, data fingerprint, commit SHA and model artifact references.
- [x] No unfinished **REPLACE** blocks remain above this checklist; the course-material links are kept.
- [x] Ran `git log -p | grep -i -E "secret|password|AKIA|BEGIN PRIVATE"`. **Accepted limitation:** it returns keyword matches, including `SECRET_STORE_PATH` and the checklist command itself, so it does not return nothing. Additional checks found no private-key headers, AWS access-key IDs, GitHub tokens or Azure account-key patterns, and no committed `cloud.env`. These checks do not guarantee that every possible secret has been detected.

That last check is not optional. A credential in Git history is an automatic deduction in this
course, and rotating it is your responsibility, not the grader's.
