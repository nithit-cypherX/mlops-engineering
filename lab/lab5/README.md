# Lab 5

## Task 1 — Local preparation

I added a [configuration example](cloud.env.example), a
[config loader](src/config.py), and [scope guards](cloudlayer/lab5_scope.py)
for `lab5-staging` in the shared `feedbackpulse-env` environment. The guards
check the reviewed resource IDs and Lab 5 ownership tags. The shared environment
is not a Lab 5 update or deletion target.

**Checks:** All 414 offline tests and Ruff passed on 2026-10-13, using Python 3.11.16
from the existing Lab 2 environment. Run these from `lab/lab5/`:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  ../lab2/.venv/bin/python -m pytest -q -p no:cacheprovider tests

PYTHONDONTWRITEBYTECODE=1 \
  ../lab2/.venv/bin/python -m ruff check --no-cache \
  --config ../lab4/ruff.toml src cloudlayer scripts tests
```

The [pre-run cost estimate](reports/lab5-cost.md) was committed at `18f4ad4`
before this implementation work.

Task 1 is still in progress. This step did not create resources or add roles.
The six steps are now connected in a locally compiled Azure ML pipeline, but no
pipeline job has been submitted. Staging image, workspace tracking URI, and IP access settings are
still blank. The model version will come from the registration result. Offline
tests do not prove that Azure deployment, model loading, or permissions work.

### Ingest and validate

I added local [ingest and validate commands](src/data_steps.py) using the
[Lab 4 data contract](src/data.py). The original 6,000-row dataset passed, and
the input and output SHA-256 hashes matched. These commands are now wired into
the local pipeline definition; they have not run as Azure ML steps.

Run from `lab/lab5/`, using the existing local Lab 2 dataset:

```bash
lab5_run_dir=$(mktemp -d /tmp/lab5-data-check.XXXXXX)
PYTHONDONTWRITEBYTECODE=1 ../lab2/.venv/bin/python -m src.data_steps ingest \
  --input ../lab2/data/raw/sensors.csv --output "$lab5_run_dir/ingest"
PYTHONDONTWRITEBYTECODE=1 ../lab2/.venv/bin/python -m src.data_steps validate \
  --input "$lab5_run_dir/ingest/sensors.csv" --output "$lab5_run_dir/validated"
```

Each step writes a CSV copy and a JSON report. Missing or invalid data makes
validation fail without publishing a validated copy. Output folders must be
empty; these commands do not overwrite the source or earlier results.

### Local training

I connected [training](src/train.py) to the validated dataset using the selected
Lab 2 settings: 100 trees, max depth 12, minimum leaf size 10, and seed 20260101.
One local run gave validation ROC-AUC **0.844499** and test ROC-AUC **0.846673**.
Reloading the saved model gave the same scores. This was a local check with
uncommitted code, not an Azure pipeline run.

After the ingest/validate commands above, run in the same shell:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=20260101 \
  ../lab2/.venv/bin/python -m src.train \
  --validated-dir "$lab5_run_dir/validated" \
  --dvc-metadata-path ../lab2/data/raw.dvc \
  --output-dir "$lab5_run_dir/train"
```

The output contains `model/` in MLflow format and `metrics.json`. A failed
validation report or changed CSV stops training. The [saved local result](reports/lab5-local-training.json)
records parameters, metrics, the data fingerprint, the supplied DVC directory
version, and the Git revision marked `dirty`. Model files are not included in Git.
The original training check reported 9 deprecation warnings from the existing
MLflow/Pydantic dependencies. The latest suite, including the Azure SDK checks,
reported 1,045 warnings from the existing dependencies; no dependencies
were changed.

### Local evaluation gate

I added a [gate](scripts/evaluation_gate.py) using validation ROC-AUC with a
minimum of **0.80**. This is our lab quality floor, not a threshold set by the
instructor or proof that the model beats the current production model.

The existing score of **0.844499** passed. Using **0.90** for the negative check
returned exit code 1 without changing the score or retraining. Missing or invalid
metrics also fail. The gate is now a dependency of registration in the compiled
pipeline. That failure path has not yet been checked in a live Azure ML run.

After the training command above, run in the same shell. The normal check uses
0.80 and returns exit code 0 for the recorded score:

```bash
PYTHONDONTWRITEBYTECODE=1 ../lab2/.venv/bin/python scripts/evaluation_gate.py \
  --metrics "$lab5_run_dir/train/metrics.json"
```

The stricter check below should fail with exit code 1. It tests the failure path;
it does not change the normal 0.80 floor or show that the model got worse:

```bash
PYTHONDONTWRITEBYTECODE=1 ../lab2/.venv/bin/python scripts/evaluation_gate.py \
  --metrics "$lab5_run_dir/train/metrics.json" --min-auc 0.90
```

To check the saved score without training again, use
`--metrics reports/lab5-local-training.json` in either command.

### Registration checks

I connected the gate to the [registration code](cloudlayer/azure_registry.py).
Failed checks stop before any registry call. New training exports include file
hashes and matching model metadata, so mixed or changed files are rejected.

Passing tests sent the model and lineage to a mock registry and checked the
returned version. Registry errors did not report success. This was an offline
check, not a real Azure registration. The earlier training result is unchanged
and does not contain the new hashes, so it cannot pass these registration checks.

The function requires an explicitly supplied registry client. It targets the
separate `itcs355-u6688124-lab5` model and does not promote or deploy anything.
It is now connected in the local pipeline definition, not a submitted job. The
[registration tests](tests/test_registration.py) and the existing
[tiny training test](tests/test_train.py) check this path without calling Azure.

### Step handoffs

I added file handoffs for the next pipeline steps. Training accepts a Git SHA
with `--git-commit` when `.git` is not available and records it as supplied
lineage, not proof of the image contents. With local Git, a mismatched SHA or
dirty state is rejected. Without this option, training still reads local Git
and records uncommitted changes as `-dirty`.

The gate writes `gate.json` tied to the exact training metrics. After training,
use a new, empty output folder to save the decision:

```bash
PYTHONDONTWRITEBYTECODE=1 ../lab2/.venv/bin/python scripts/evaluation_gate.py \
  --metrics "$lab5_run_dir/train/metrics.json" --output-dir "$lab5_run_dir/gate"
```

A failed gate does not write a pass report. An existing output folder with files
is rejected, not overwritten. Stop if the command fails; do not reuse an older
gate report.

The registration command requires that report and writes the verified model
name and version to `registration.json`. It uses `--training-dir`, `--gate-report`,
and `--output-dir`; it does not select `latest`. **This is a cloud-write command,
so do not run it in this local-only step.** To view its arguments without logging
in or calling Azure:

```bash
PYTHONDONTWRITEBYTECODE=1 ../lab2/.venv/bin/python -m cloudlayer.azure_registry --help
```

A tiny-data test passed through ingest, validate, train, gate, and a mock
registry. No Azure job or real registration was run. The steps now have bindings
in the locally compiled pipeline. The earlier 6,000-row training result is
unchanged.

### Staging deployment checks

I added a [staging adapter](cloudlayer/azure_staging.py) using the Lab 4 serving
setup. It reads the exact model name and numbered version from `registration.json`,
not `latest`. It only targets `lab5-staging`, with a digest-pinned image, 0.5 CPU,
1 GiB memory, and zero to one replica. Access is limited to one configured public
IPv4 address with `/32`; missing settings stop before authentication.

The shared environment and Lab 5 runtime identity must already exist and pass
the scope checks. The adapter reads them but does not change them or add roles.
An existing App without the right Lab 5 tags is rejected. No Lab 2–4 code changed.

The [offline tests](tests/test_staging.py) checked the request body, version
handoff, access rules, and revision read-back. Wrong versions, permission errors,
failed revisions, and timeouts did not report success. The adapter sends one
deployment request and waits up to 600 seconds for revision checks; it does not
automatically redeploy or delete anything after failure.

`deployment.json` records the endpoint, image, model version, and revision state.
It explicitly says `http_smoke_tested: false`. A healthy or scaled-to-zero revision
reported by Azure is not proof that a prediction request works. Live image
compatibility, model loading, permissions, and HTTP checks still need verification.

This was tested with fake credentials and mocked HTTP responses, not Azure.
The CLI is a cloud-write command and has not been run against the cloud. To view
its arguments safely from `lab/lab5/`:

```bash
PYTHONDONTWRITEBYTECODE=1 ../lab2/.venv/bin/python -m cloudlayer.azure_staging --help
```

The adapter uses the existing Python HTTP and identity libraries rather than
requiring an Azure CLI login inside the pipeline container. The request fields
follow Microsoft's [Container Apps create/update API](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/container-apps/create-or-update?view=rest-resource-manager-containerapps-2025-07-01)
and [revision API](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/container-apps-revisions/get-revision?view=rest-resource-manager-containerapps-2025-07-01).

### Local pipeline compilation

I connected the six steps in [pipeline/pipeline.yaml](pipeline/pipeline.yaml):
ingest, validate, train, evaluate, register, and deploy to staging. This file
describes the commands and file handoffs without Azure resource settings.
The [Azure compiler](cloudlayer/pipelines.py) adds those settings and creates
an Azure ML pipeline object and local YAML. It does not upload files, log in,
submit a job, create resources, or set a schedule.

Register must receive the gate's output, and deploy must receive that run's
`registration.json`. I set `continue_on_step_failure: false` so a failed step
stops the pipeline. Each run is configured to rerun the steps instead of reusing
cached results. These are Azure ML settings, not proof of a cloud run; see
Microsoft's [pipeline job reference](https://learn.microsoft.com/en-us/azure/machine-learning/reference-yaml-job-pipeline?view=azureml-api-2).

**Checks:** The 36 new [compiler tests](tests/test_pipelines.py) used the installed
Azure ML SDK with network and credential calls blocked. They checked all six
steps, actual input/output bindings, identity references, and YAML save/load.
Running the compiled gate command locally with 0.90 returned 1 and wrote no pass
report. This did not test Azure's scheduler or deploy anything.

**Problems and fixes:** SDK 1.35.0 has no `dsl.condition`, so I used the required
file dependencies and pipeline failure setting. Its YAML export also added
`azureml:` to local input paths. The exporter preserves the two original local
file paths, and tests check that the loaded YAML still points to those files.
It refuses to overwrite an existing output file.

To see the compiler arguments safely, run from `lab/lab5/`:

```bash
PYTHONDONTWRITEBYTECODE=1 ../lab2/.venv/bin/python -m cloudlayer.pipelines --help
```

Compilation requires local data and DVC metadata files, a full Git SHA, a
digest-pinned pipeline image, a compute name, training/release identity client
IDs, and complete staging settings. These must be reviewed before live use;
the test values are not real deployment settings. Code must be inside the image,
not uploaded from the working folder. A supplied SHA does not prove what is in
that image. The local image checks are recorded below. Compute/identity setup,
permissions, live failure handling, and the staging HTTP check still need
verification. Task 1 is not finished yet.

### Pipeline image and local checks

I prepared a [Dockerfile](Dockerfile) for the Lab 5 step code, using the pinned
Python 3.11 base and non-root setup from Lab 2 at `18f4ad4`. The
[dependency input](requirements.in) keeps the same pins, and
[requirements.txt](requirements.txt) is byte-for-byte identical to Lab 2's lock.
No dependency pins changed, and nothing was installed into the host environment.
The inherited lock still includes some DVC and test tools that these pipeline
steps do not need.

The recipe copies `src/`, `cloudlayer/`, and `scripts/`. The pipeline chooses
which command to run; the image's default command only shows Python help.
The [.dockerignore](.dockerignore) excludes local credentials and run outputs.
A build must supply a full Git SHA through `GIT_COMMIT`. The recipe checks its
format; I also compared the 13 Python source files inside this image with the
committed source. All file hashes matched.

The 10 new [recipe tests](tests/test_image_recipe.py) checked the pinned base,
lock hashes, copy scope, defaults, exclusions, and the small SHA-check snippet.
All 414 offline tests and Ruff passed on the host before the source commit.

I then built `itcs355-lab5:pipeline-9766879` for `linux/amd64` on 2026-10-13,
using source commit `9766879e586090893b6b3ce1f4ffb427e868a180`. The image's
revision label matches that commit. From `lab/lab5/` at that source revision:

```bash
docker buildx build --platform linux/amd64 \
  --build-arg GIT_COMMIT=9766879e586090893b6b3ce1f4ffb427e868a180 \
  --tag itcs355-lab5:pipeline-9766879 --load .
```

The local image ID is
`sha256:f99c8ca6dd888167cbe8de057a01402f3062a811f771a534fe58eacf5d8ccf47`.
This is not an ACR manifest digest. The build reused cached dependency layers,
so it did not test a fresh download and installation of every package.

**Container checks:** `pip check` passed. Imports and `--help` worked for all six
step command modules, even from `/tmp`. The non-root `runner` user (UID 10001)
could write to a temporary output mount prepared with matching ownership.
The same 414 tests passed inside the image in 16.05 seconds, with 1,045 dependency
warnings. The tests used the code baked into the image, not a source-code mount.
Both container checks ran with networking disabled, a read-only root filesystem,
and no credentials mounted. Azure calls in the test suite were mocked.

This confirms the local image works for these checks. It does not prove Azure ML
output-mount permissions, cloud authentication, registry access, or a live
pipeline run. No Git or image push was made, and no Azure job was run.
