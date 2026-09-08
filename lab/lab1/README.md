# Lab 1 workspace status

**Starter code only — not a completed submission.** Copied from the instructor repository at commit `2018ce996fe2da4602761977a5555cf347d10b7b`.

- [Lab handout](../../materials/upstream/course/labs/lab-01-reproducible-training.md)
- [Course overview](../../materials/upstream/course/README.md)
- [Week 0 evidence](../lab0/README.md)

Run commands from this directory. A fresh clone uses the repository root as its Git root, then `cd lab/lab1`. Before DVC setup, use `dvc init --subdir` here because this lab is nested inside the root Git repository; subsequent DVC commands run from this directory. No DVC repository has been initialized yet.

The local environment was recreated for Ubuntu. The Makefile includes Lab 1 targets only. Tests for later labs are not included. Azure adapter TODOs and grading decisions are intentionally unfinished. The expected metric below is the instructor's baseline, not a newly measured student result.

---

# ITCS355 Lab 1 — Reproducible Training

> **Course materials:** [overview](../../materials/upstream/course/README.md) and [portability reference](../../materials/upstream/course/reference/cloud-portability-reference.md). Keep this reference block when editing the Lab 1 README.

Predicting machine failure within 7 days from sensor readings. The model is not the point;
whether a stranger can reproduce it is.

> **This README is graded.** A grader with Docker and nothing else from your setup runs one
> command and compares the result against the claim below. Edit every `<...>` and delete the
> instruction blocks marked **REPLACE** before submitting.

---

## Reproduce

```bash
make reproduce
```

expected test_roc_auc: 0.848 ± 0.010

Runtime: about 40 seconds on 4 cores. No cloud account or credentials needed for this command —
that is deliberate, and it is why a grader can run it.

**REPLACE:** re-measure and update that claim line after your final change. Keep the exact
format `expected test_roc_auc: <value> ± <tolerance>`; `make verify` parses it, and so does the
grading script. Choose the tolerance from the spread you actually observe across seeds. Padding it
to hide non-determinism is visible — the grader compares your tolerance against the variance in
your own tracked runs.

---

## The problem

240 machines, 25 readings each, 6 sensor features, binary target `failed_within_7d` with a
positive rate near 12%.

Machines have persistent characteristics — a hot-running machine reads hot in every row. So the
train/validation/test split is **grouped by `machine_id`**: every reading from one machine lands
in exactly one partition. Splitting row-wise instead lets the model memorise the machine and
reports a validation score that will never survive production. `tests/test_data.py` asserts this
property holds, and Lab 4 turns it into a CI gate.

Bringing your own dataset is allowed. Replace `scripts/make_dataset.py`, update the schema in
`src/data.py`, and keep every test passing.

---

## Layout

```
src/          Layer 1 — provider-neutral. No SDKs, no bucket names, no absolute paths.
cloudlayer/   Layer 3 — the only place a provider SDK may be imported.
scripts/      Dataset generation, cloud check, portability audit, metric verification.
tests/        Data contract tests and split property tests.
```

`src/config.py` is the single point of environment knowledge. Everything else reads from it.
`make portability-audit` enforces the rule; it fails the build if a provider string appears in
`src/` or `tests/`.

---

## Setup

```bash
cp cloud.env.example cloud.env      # fill in, never commit
make setup
make cloud-check                    # eight slots, all PASS
make data                           # generate the dataset
make test                           # 10 tests, all passing
```

Post your `make cloud-check` output in the course channel before Session 1.

---

## What you must finish

Four `TODO` markers are left in the repo deliberately. Each is a graded decision, not busywork.

| Where | What |
|---|---|
| `requirements.txt` | Regenerate with `pip-compile --generate-hashes` |
| `Dockerfile` | Pin the base image by digest; add `--require-hashes` |
| `cloudlayer/<your provider>.py` | Implement `upload`, `download`, `push_image` |
| This README | The reproducibility trade-off question below |

Then:

```bash
make image-push        # image reaches your registry, digest-pinned
dvc init --subdir && dvc remote add -d storage ${BLOB_URI}/dvc
dvc add data/raw && dvc push
```

Run five or more tracked runs varying something meaningful — not five identical runs with
different seeds.

---

## Reproducibility trade-off

**REPLACE with your answer, 100 words maximum.**

Three things pin your build: hashed dependencies, a digest-pinned base image, and controlled
seeds. Under real time pressure you would keep some and drop others.

Which would you drop first, and what specifically breaks when you do? There is a defensible
answer, and we compare answers in Session 2. An answer that refuses to choose scores zero.

---

## Notes for the grader

**REPLACE:** anything that would otherwise cause you to answer a question by email. Non-obvious
choices, known limitations, anything that behaves differently on your machine. A README that
requires a conversation has failed the lab regardless of what the code does.

---

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
