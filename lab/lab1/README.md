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
