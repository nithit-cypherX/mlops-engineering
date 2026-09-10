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

## Checklist before you submit

- [ ] `make reproduce` works from a fresh clone, on a machine that is not yours
- [ ] `make verify` passes against your claim line
- [ ] `make test` — all tests pass
- [ ] `make portability-audit` — clean
- [ ] Image builds for `linux/amd64` and is pushed, digest-pinned
- [ ] `dvc push` completed; a grader can `dvc pull`
- [ ] Five or more tracked runs with params, metrics, data fingerprint, and commit SHA
- [ ] Every **REPLACE** block above is gone (the course-materials block at the top stays)
- [ ] `git log -p | grep -i -E "secret|password|AKIA|BEGIN PRIVATE"` returns nothing

That last check is not optional. A credential in Git history is an automatic deduction in this
course, and rotating it is your responsibility, not the grader's.
