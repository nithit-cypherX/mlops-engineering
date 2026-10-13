# Lab 5

## Task 1 — Managed pipeline (in progress)

I added a [configuration example](cloud.env.example), a
[config loader](src/config.py), and [scope guards](cloudlayer/lab5_scope.py)
for `lab5-staging` in the shared `feedbackpulse-env` environment. The guards
check the reviewed resource IDs and Lab 5 ownership tags. The shared environment
is not a Lab 5 update or deletion target.

**Checks:** All 438 offline tests and Ruff passed on 2026-10-13, using Python 3.11.16
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

Task 1 is still in progress. The two Lab 5 containers and datastores are now
created and checked, along with the training, release, and runtime identities.
Their details and screenshots are recorded below. The identities now have four
container-scoped storage assignments and two registry-scoped `AcrPull` assignments.
Training also has a custom model-registration role on the existing ML workspace.
Runtime has a custom role to read workspace and model metadata. Release now
has Environment read/join access, a custom read/assign role on the Lab 5
runtime identity, and App read/write plus revision-read access only on
`lab5-staging`. It still has no ML workspace role. Runtime
model-file downloads through MLflow still need verification.
Training and release are now attached to `cpu-lab5`, which was checked with
zero current and target nodes. Runtime is not attached to an App yet.
`lab5-staging` now exists as a public-image placeholder with ingress
disabled; it is not serving a model yet.
The six steps are now connected in a locally compiled Azure ML pipeline, but no
pipeline job has been submitted. The local, Git-ignored `cloud.env` now has the
reviewed serving image, workspace tracking URI, and approved current IP. These
settings have not been applied to Azure. The model version will come from the registration result. Offline
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
MLflow/Pydantic dependencies. The later 414-test suite, including the Azure SDK
checks, reported 1,045 warnings. The latest full-suite result is recorded under
storage bindings below; no dependencies were changed.

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
that image. The local image checks are recorded below. Live use of the configured
compute and identities, permissions, failure handling, and the staging HTTP check still need
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

### Cloud prerequisites — read-only check

On 2026-10-13, I checked the existing resources without submitting a job or
changing Azure settings.

| Item | What I found |
|---|---|
| Shared services | The workspace, storage account, and Basic ACR reported `Succeeded`. ACR admin access is disabled. |
| Staging environment | `feedbackpulse-env` reported `Succeeded` with a Consumption profile. It stays shared and unchanged. |
| Compute | The workspace compute list was empty. The Lab 2 and Lab 4 clusters were already removed during their teardown. |
| Lab 5 identities and App | No Lab 5 user-assigned identities or `lab5-staging` App appeared in the resource-group inventory. |

The Azure ML Malaysia West size list includes `Standard_F2s_v2` (2 vCPUs, 4 GiB),
but not the handout's `Standard_B2s`. The AML quota response showed 0 of 16
Dedicated regional vCPUs used, 0 of 16 FSv2 Dedicated vCPUs used, and a regional
Low-priority limit of 0. These are the returned AML limits, not a promise that
Azure can allocate a VM. The existing estimate's F2s_v2 Dedicated plan remains
a candidate, with zero minimum nodes and one maximum node; nothing was created.
Microsoft notes that [AML supports a specific set of VM sizes](https://learn.microsoft.com/en-us/azure/machine-learning/concept-compute-target?view=azureml-api-2#supported-vm-series-and-sizes),
which can differ from ordinary Azure VMs.

Before a live run, we still need the pipeline image in ACR with its real manifest
digest, a configured compute target, and reviewed training, release, and serving
identities with scoped access. The serving image digest and allowed IP also need
review. The workspace returned its MLflow tracking URI, but no live Lab 5
`cloud.env` was created. The four datastores present at that check reported `AccountKey` authentication;
I did not retrieve any keys or change them. That setup is not evidence of
identity-scoped data access. Task 2's permission-removal test is still separate.

**Tool issue:** The installed CLI `ml` extension could not load. I used core CLI
GET/list commands with a temporary empty extension-discovery folder for these
checks. I did not remove, reinstall, or repair the extension. No code, roles,
resources, or image contents changed in this check.

### Compute and access plan — grants applied so far

I used the reviewed [pipeline/compute.yaml](pipeline/compute.yaml) to create
`cpu-lab5` in `mlw-itcs355-u6688124`, using Dedicated F2s_v2,
zero to one node, and scale-down after 120 idle seconds. It keeps public-node
networking but disables public SSH and local SSH credentials. This is not a
private-network setup. The tags stay `course=itcs355`, `student=itcs355-u6688124`,
and `lab=5`. The four-node-hour allowance in the original estimate is unchanged;
scale-down settings are not a spending cap.

The three [compute-draft tests](tests/test_compute_config.py) loaded the YAML
with the existing Azure ML SDK and checked its serialized request, scale/SSH
settings, and identity order with cloud calls blocked. The full host suite passed:
417 tests and Ruff, with 1,054 dependency warnings. This checks the local file,
not Azure provisioning or RBAC. The image was not rebuilt; its 414-test result
above is unchanged. No resource or role was created during that offline check.

The identity names are `id-itcs355-u6688124-lab5-training`,
`id-itcs355-u6688124-lab5-release`, and `id-itcs355-u6688124-lab5-runtime`.
They have now been created in the reviewed resource group and region; the
creation checks are recorded below. Azure returned their client and principal
IDs. The storage, image-pull, training model-registration, runtime model-metadata,
release Environment read/join, and runtime-identity read/assign grants are
recorded below. Training/release compute attachments and the App-scoped release
role are now applied. Runtime model-file access through MLflow is not yet verified.

| Identity | Intended work | Proposed access boundary |
|---|---|---|
| Training | Ingest through registration; default compute identity for image/data setup | `AcrPull` on the existing ACR; read input blobs and write run/model artifacts in explicitly reviewed containers; model registration/read-back in the existing ML workspace. No App deployment or role-management grant. |
| Release | Only the `deploy_staging` step | App read/write and revision read on `lab5-staging`; environment read/join on `feedbackpulse-env`; custom read/assign access on the Lab 5 runtime identity. Its registration input and deployment output also need working datastore access. No App delete or environment update/delete grant. |
| Runtime | Load the numbered model and serve requests | `AcrPull` on the ACR plus read-only model metadata/artifact access. No model registration, deployment, or role-management grant. Normal stdout/stderr collection does not require giving it Log Analytics Contributor. |

This is the overall access plan, not proof of live access. `AcrPull` on this
legacy-permissions ACR can pull other repositories too; it is not restricted to
one image. AML model permissions at workspace scope can cover other models;
the adapter's model-name check is not an RBAC boundary. The artifact grants are
limited to `lab5-artifacts`. The training model grant and runtime metadata-read
grant now use the reviewed workspace scope. No workspace artifact-service read
permission was added. The actual MLflow download path still needs review before
any further runtime grant.

For release, the granted custom actions are `Microsoft.App/containerApps/read`,
`Microsoft.App/containerApps/write`, and `Microsoft.App/containerApps/revisions/read`
at the App. `Microsoft.App/managedEnvironments/read` and
`Microsoft.App/managedEnvironments/join/action` are now granted at the shared
environment only; the checks are recorded below. Release now has custom
read/assign access on the Lab 5 runtime identity. The earlier Managed Identity
Operator proposal was replaced with this two-action role; the built-in role
also includes other actions and wildcards that this step does not need.
Do not give any job identity Owner, Contributor on the resource group,
role-assignment writes, or permission to retrieve storage account keys.

**Access boundaries and remaining checks:**

- The compute attaches training and release to one cluster, matching the current
  compiler. Training is listed first as the intended default identity. Azure
  also uses a compute identity for image/data setup. Separate client IDs on the
  same machine are not isolation from untrusted jobs: code may request another
  attached identity. This setup assumes trusted lab code. If we need enforced
  job isolation, use separate compute targets and update the compiler first.
- The local storage bindings below now avoid the shared default for declared
  inputs, step outputs, and model uploads. The containers and datastores are now
  created and checked, and container-scoped Blob grants are now configured.
  Live pipeline access is still unverified. Do not grant
  `datastores/listsecrets/action` just to make a stored-key path pass.
  A folder prefix alone does not enforce container-level Blob RBAC.
- The placeholder App and its App-scoped release role now exist. Model deployment
  remains untested. Keep the release grant limited to that App; do not widen it
  to the whole resource group.

Image publishing and pipeline submission remain separate approved operations;
the release identity is not a new GitHub CI identity. This setup does not finish
Task 2 or its permission-removal test. No Lab 2–4 or FeedbackPulse roles change.

References: [compute YAML fields](https://learn.microsoft.com/en-us/azure/machine-learning/reference-yaml-compute-aml?view=azureml-api-2),
[compute identity behavior](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-identity-based-service-authentication?view=azureml-api-2#compute-cluster),
[data authentication](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-administrate-data-authentication?view=azureml-api-2),
and [Container Apps actions](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/compute#microsoftapp).

### Lab 5 storage bindings

I added two credentialless datastore configurations for the existing storage account
`itcs355u6688124`. They contain no account key or SAS. Each points to a separate
private Blob container, not just a folder in the shared container.

| Intended files | Datastore configuration | Container |
|---|---|---|
| Raw CSV and DVC metadata uploaded by the job submitter | [lab5_inputs](pipeline/datastore-inputs.yaml) | `lab5-inputs` |
| All six steps' outputs and the model uploaded during registration | [lab5_artifacts](pipeline/datastore-artifacts.yaml) | `lab5-artifacts` |

The [compiler](cloudlayer/pipelines.py) sets the two input upload destinations
and a job-only default for outputs. It does not change the workspace default.
The [registration code](cloudlayer/azure_registry.py) checks the datastore's
account, container, HTTPS endpoint, ownership tags, and credential type before
uploading. Wrong metadata, stored credentials, or a failed lookup stop the call.
The existing gate, model hashes, version allocation, and read-back checks stay.

**Checks:** All 438 offline tests and Ruff passed, with 1,237 dependency warnings.
The tests covered YAML reloads, the installed SDK's input-upload routing and
credential selection, rejected storage settings, and a tiny training export
through mock registration. An SDK control-flow test also checked that model
version allocation did not access the workspace default datastore. Uploads and
service responses were mocked; these results do not prove Azure access.

The live datastore definitions and private-container settings were checked below.
For a pipeline run, the submitter needs input-upload access; training needs input read and
artifact read/write; release needs artifact read/write; runtime needs artifact
read. The grants are recorded below; the live denial test remains separate.
Sharing the artifact container also gives readers access to its other run files,
not only models.
These bindings do not relocate Azure ML's internal logs or other workspace storage.

The offline implementation did not create resources or upload files. The earlier
image does not include these storage-code changes and must be rebuilt before a
live run.

References: [credentialless Blob datastore YAML](https://learn.microsoft.com/en-us/azure/machine-learning/reference-yaml-datastore-blob?view=azureml-api-2)
and [job-level output datastore](https://learn.microsoft.com/en-us/azure/machine-learning/reference-yaml-job-pipeline?view=azureml-api-2#attributes-of-the-settings-key).

### Storage created and checked — 2026-10-13

I created `lab5-inputs` and `lab5-artifacts` in the existing storage account,
then registered `lab5_inputs` and `lab5_artifacts` in `mlw-itcs355-u6688124`.
Containers hold the files; datastores let the pipeline refer to those locations
by name. Both containers are still empty because no data was uploaded.

**Read-back checks at 09:40 ICT:** Both containers were private, and both
datastores pointed to the expected account and container over HTTPS. Their
credential type was `None`, so they stored no account key or SAS. The Lab 5
tags matched the local configuration. The workspace default stayed
`workspaceblobstore`, and the existing `itcs355` container's access was unchanged.
Private here means no anonymous Blob access, not a private-network setup.

I used the signed-in user's existing permissions; no roles were added. The
13 storage tests passed after updating the YAML descriptions, with 482 existing
dependency warnings. This setup does not prove that the future job identities
can read or write these files. Identity permissions and a live run remain separate
steps. No identity, compute, or App was created, and no pipeline, build,
commit, or push was run in this step.

Screenshots:

- [Both private containers](reports/screenshots/lab5-private-containers-2026-10-13.jpg)
- [Both Azure ML datastores](reports/screenshots/lab5-datastores-2026-10-13.jpg)

### Lab 5 identities — created, not connected

On 2026-10-13, I created the three identities listed above in
`itcs355-u6688124`, Malaysia West. All three names were absent before creation.
They have the same `course`, `student`, and `lab=5` tags as the storage setup.
I read them back and checked their resource IDs, location, tags, tenant, and
distinct client/principal IDs.

**At creation, read-only checks at 09:53 ICT:** The subscription role-assignment
query returned zero direct Azure RBAC assignments for each new principal. The workspace had no
compute targets. The resource group's existing Container App did not reference
any of the new identities, and `lab5-staging` did not exist. This is a setup check,
not proof that training, model loading, or deployment can access their resources.

No roles or federated credentials were added in that creation step, and the
identities were not attached to anything. No compute, App, pipeline run, build, commit, or push was
created or run. This step did not create Task 2's CI identity or perform its
permission-removal test. The earlier offline test results above are unchanged;
only documentation and a YAML comment changed locally in this step.

Screenshot: [Three Lab 5 managed identities](reports/screenshots/lab5-identities-2026-10-13.png).

### Container-scoped storage roles — 2026-10-13

I added four storage role assignments for the three Lab 5 identities. Each grant
is scoped to one container in `itcs355u6688124`, not the whole storage account
or resource group.

| Identity | `lab5-inputs` | `lab5-artifacts` |
|---|---|---|
| Training | Storage Blob Data Reader | Custom Blob read/write |
| Release | No grant | Custom Blob read/write |
| Runtime | No grant | Storage Blob Data Reader |

The custom role is `ITCS355 Lab5 Blob Read Write u6688124`. Its only management
action is `Microsoft.Storage/storageAccounts/blobServices/containers/read`.
Its only data actions are `Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read`
and `Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write`.
It has no delete, container-change, key-access, or role-management actions.
The definition is available for assignment within the resource group, but its
two assignments are only on `lab5-artifacts`.

I used this custom role because Storage Blob Data Contributor also allows
deletion. Read/write still allows overwriting files, so this is not immutable
storage. The grants cover all blobs in the container, not just model files.

**Read-back checks at 10:08 ICT:** A fresh subscription query found exactly two
direct assignments for training, one for release, and one for runtime. All four
principal IDs, role IDs, and container scopes matched the approved table. The
custom role's actions matched the list above, and both containers stayed private.
No ACR, ML workspace, or deployment roles were added in that storage step. The built-in Reader role
also lists a user-delegation-key action, but these container-scoped assignments
do not grant that [account-level operation](https://learn.microsoft.com/en-us/rest/api/storageservices/get-user-delegation-key#permissions).

This verifies the saved RBAC configuration, not a live Blob request made by an
identity. The identities are still unattached. No compute, App, pipeline run,
build, commit, or push was created or run in this step. The earlier offline test
results are unchanged; no implementation code changed here.

Screenshots:

- [Training storage roles](reports/screenshots/lab5-training-storage-roles-2026-10-13.jpg)
- [Release storage role](reports/screenshots/lab5-release-storage-role-2026-10-13.jpg)
- [Runtime storage role](reports/screenshots/lab5-runtime-storage-role-2026-10-13.jpg)

References: [built-in Blob roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/storage)
and [custom roles and assignment scope](https://learn.microsoft.com/en-us/azure/role-based-access-control/custom-roles).

### ACR image-pull roles — 2026-10-13

I added `AcrPull` to the training and runtime identities, scoped only to the
existing ACR `itcs355u6688124`. Release did not get an ACR role. This role allows
image pulls, not pushes, deletion, or changes to the registry settings.

Training is first in the compute draft, so it is the proposed default identity
Azure ML uses to prepare job containers. The release step runs on that same
compute; it does not need a separate pull grant. The staging adapter already
selects runtime for the Container App's image pull. The identities are still
unattached, so this is permission setup, not a successful live image pull.

**Scope limit:** The ACR still uses `LegacyRegistryPermissions`. These grants
allow pulling from all repositories in this registry, including non-Lab-5 images.
They are not limited to one repository, tag, or digest. I did not switch the shared
registry to ABAC or enable admin credentials or anonymous pulls.

**Initial read-back:** The two assignment records matched the expected
principal IDs, `AcrPull` role ID, and exact ACR scope. The first subscription
list still showed the older storage-only state, so I did not retry the writes.

**Final checks at 12:41 ICT:** The subscription list then showed exactly three
direct assignments for training, one for release, and two for runtime. This
matched the four existing storage grants plus the two new pull grants. A separate
ACR-scoped list also confirmed both grants and no ACR grant for release.
The registry mode, disabled admin account, and disabled anonymous pull stayed
unchanged. No extra permissions were needed to resolve the delayed list result.

No ML/deployment roles, compute, or App were added. No image was built, pulled,
or pushed, and no pipeline, commit, or Git push was run. The earlier offline
test results are unchanged; no implementation code changed in this step.

Screenshots:

- [Training AcrPull and existing storage roles](reports/screenshots/lab5-training-acrpull-2026-10-13.jpg)
- [Runtime AcrPull and existing storage role](reports/screenshots/lab5-runtime-acrpull-2026-10-13.jpg)

References: [Azure ML default compute identity](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-identity-based-service-authentication?view=azureml-api-2#compute-cluster),
[Container Apps image-pull identity](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity-image-pull),
and [ACR role modes](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-rbac-abac-repository-permissions).

### Training model-registration role — 2026-10-13

I added `ITCS355 Lab5 Model Registrar u6688124` only to the Lab 5 training
identity. Both the role's assignable scope and its assignment are limited to
`mlw-itcs355-u6688124`. Runtime and release did not get an ML workspace role
during that training step.

The custom role has five actions under `Microsoft.MachineLearningServices/`:

| Action | Why the registration step needs it |
|---|---|
| `workspaces/read` | Read workspace metadata for the ML client. |
| `workspaces/datastores/read` | Check the artifact datastore before uploading. |
| `workspaces/models/read` | Read the model entry and the next version number. |
| `workspaces/models/versions/read` | Read existing versions and check the registered result. |
| `workspaces/models/versions/write` | Create or update a model version. |

There are no wildcards, delete actions, datastore-secret reads, compute or
deployment actions, or role-management actions. The role adds no Blob data
actions; model-file uploads still use the existing container-scoped storage
grant. The inspected SDK skips datastore-secret requests for our credentialless
datastore.

**Scope limit:** These permissions cover model versions and datastore metadata
throughout this workspace, not only the Lab 5 model. The model-name check in our
code is not an RBAC boundary. Version write access is not an append-only grant.

**Checks at 12:56 ICT:** I read back the five actions, the training principal,
and the exact workspace scope. Separate subscription and workspace lists
confirmed the new assignment. Training now has four direct assignments;
release still has one and runtime still has two. All six earlier storage/ACR
assignments were unchanged. The workspace still had zero compute targets and
`lab5-staging` did not exist.

This checks the saved permissions, not a real registration made by training.
No model was registered, no identity was attached, and no pipeline, build,
deployment, commit, or push was run. No implementation code changed, so the
earlier offline test results above are unchanged and were not rerun here.

Screenshots:

- [Custom role assigned to training](reports/screenshots/lab5-training-model-registrar-2026-10-13.jpg)
- [Four read permissions and one model-version write permission](reports/screenshots/lab5-model-registrar-actions-2026-10-13.png)

Reference: [Azure ML roles and model operations](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-assign-roles?view=azureml-api-2).

### Runtime model-metadata role — 2026-10-13

I added `ITCS355 Lab5 Model Reader u6688124` only to
`id-itcs355-u6688124-lab5-runtime`. The role can be assigned only in
`mlw-itcs355-u6688124`, and its assignment uses that exact workspace scope.

It has three actions under `Microsoft.MachineLearningServices/`:

| Action | What runtime can read |
|---|---|
| `workspaces/read` | Workspace metadata. |
| `workspaces/models/read` | Model entries in the workspace. |
| `workspaces/models/versions/read` | Registered model versions. |

The role has no wildcards, write/delete actions, datastore or artifact-service
permissions, or Blob data actions. It does not allow training, deployment, or
role changes. The existing Blob Reader on `lab5-artifacts` and `AcrPull` on the
ACR stayed unchanged. This metadata role covers other models in the same
workspace too, not just the Lab 5 model.

**Checks at 13:16 ICT:** Separate subscription and workspace lists confirmed
the runtime assignment, its scope, and all three read actions. Runtime now has
three direct assignments; training still has four and release still has one.
All seven earlier assignments were unchanged. The first subscription list
lagged behind the direct read-back; it later showed the new assignment without
repeating the write. The workspace still had zero compute targets, and
`lab5-staging` did not exist.

This checks the saved role setup, not a model request made by runtime. The
reused loader reads a numbered model through MLflow, which may request files
through the Azure ML artifact service. We still need to check the real model's
download path and test runtime access. The Blob grant alone does not prove
that whole path works; no workspace-wide artifact read permission was added.

No identity was attached, and no compute, App, model download, pipeline, build,
deployment, commit, or push was created or run in this step. No implementation
code changed; the earlier offline tests were not rerun.

Screenshots:

- [Model Reader role assigned to runtime](reports/screenshots/lab5-runtime-model-reader-2026-10-13.png)
- [Three read permissions](reports/screenshots/lab5-runtime-model-reader-actions-2026-10-13.png)

Reference: [Azure ML roles and model operations](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-assign-roles?view=azureml-api-2).

### Release Environment read/join role — 2026-10-13

I added `ITCS355 Lab5 Environment Join u6688124` only to
`id-itcs355-u6688124-lab5-release`. Both the role's assignable scope and its
assignment are limited to the existing `feedbackpulse-env` resource.

The role has only two actions:

- `Microsoft.App/managedEnvironments/read` to read the Environment.
- `Microsoft.App/managedEnvironments/join/action` to let an App use it when
  the required App permissions are also in place.

It has no Environment update/delete, App write, identity-assignment, secret-read,
or role-management permissions, and no data actions or wildcards. Join access
is not limited to the name `lab5-staging`. The later App write grant must be
scoped to that App separately; this role alone is not enough to deploy it.

**Final checks at 13:29 ICT:** Direct read-back plus Environment-scoped and
subscription-wide lists confirmed the release principal, exact scope, and two
actions. Release now has two direct assignments; training still has four and
runtime still has three. The eight earlier assignments were unchanged. The first
subscription list lagged behind the direct read-back, then caught up without
repeating the write. The Environment was still `Succeeded`, with the same
configuration and Consumption profile. The workspace still had zero compute
targets, and `lab5-staging` did not exist.

This verifies the saved role setup, not a deployment made by release. No identity
was attached, no Environment or App configuration was changed, and no compute,
App, pipeline, build, deployment, commit, or push was created or run. No
implementation code changed; the earlier offline tests were not rerun.

Screenshots from the Portal show the release assignment on `feedbackpulse-env`
and the role's two permissions. Capturing them did not change the Azure setup.

- [Environment Join role assigned to release](reports/screenshots/lab5-release-environment-join-2026-10-13.jpg)
- [Two read/join permissions](reports/screenshots/lab5-environment-join-actions-2026-10-13.jpg)

Reference: [Container Apps permissions](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/compute#microsoftapp).

### Release runtime-identity read/assign role — 2026-10-13

I added `ITCS355 Lab5 Runtime Identity Assigner u6688124` to
`id-itcs355-u6688124-lab5-release`. Both the role's assignable scope and its
assignment are limited to `id-itcs355-u6688124-lab5-runtime`.

The role has only two actions:

- `Microsoft.ManagedIdentity/userAssignedIdentities/read` to read the runtime
  identity and its client ID before deployment.
- `Microsoft.ManagedIdentity/userAssignedIdentities/assign/action` to allow
  attaching that identity to an App when App write permission is also present.

It cannot create, update, or delete identities, change federated credentials,
manage roles, or write Apps. It has no wildcards or data actions. The built-in
Managed Identity Operator role was not granted.

This scope limits which identity release can assign, not the destination App.
The later App write grant must be limited to `lab5-staging`. If release gains
write access to other compatible resources later, this assign permission could
let it attach the same runtime identity there too.

**API checks at 13:54 ICT:** Direct read-back, runtime-scoped listings, and
subscription-wide listings confirmed the release principal, exact scope, and
two actions. Release had three direct assignments, training had four, and
runtime had three. All nine earlier assignments were unchanged. The first
subscription list lagged, then caught up without repeating the write. Identity
metadata and the shared Environment configuration were unchanged. There were
still no workspace compute targets or `lab5-staging` App.

This verifies the saved permissions, not an actual attachment or deployment by
release. No identity was attached, no compute or App was created, and no
pipeline, build, deployment, commit, or push was run. No implementation code
changed, so the earlier offline tests were not rerun.

Portal evidence:

![Release role assignment scoped to the Lab 5 runtime identity](reports/screenshots/lab5-release-runtime-assigner-2026-10-13.jpg)

![Custom role with only identity read and assign permissions](reports/screenshots/lab5-runtime-assigner-actions-2026-10-13.jpg)

References: [Managed identity actions](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/identity#microsoftmanagedidentity)
and [Managed Identity Operator](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/identity#managed-identity-operator).

### Staging placeholder — 2026-10-13

I created `lab5-staging` in the existing `feedbackpulse-env` in Malaysia West.
This gives the next release-role assignment an exact App scope. It uses
Microsoft's public Hello World image, not our model-serving image:

```text
mcr.microsoft.com/k8se/quickstart@sha256:3a4d93c34c6753f24765ab17a36f1754aee9b02082b7d9553d17697b9e8252c4
```

Ingress is disabled. The Consumption settings are 0.5 CPU, 1 GiB memory, and
0–1 replicas. No identity, secret, or private registry configuration is attached.

**Checks:** One create request succeeded. At 14:48 ICT, Azure reported
`Succeeded` and listed `lab5-staging--sw3yomt` as the latest ready revision.
Read-back confirmed the image, scale limits, disabled ingress, Lab 5 tags,
and exact Environment. The shared Environment configuration was unchanged.
The Portal later showed the revision running with one replica; this is not
proof that it had already scaled to zero or that running it costs nothing.

No roles were added, no compute or pipeline job was created, and no model was
registered or deployed. At this bootstrap step, release still needed App-scoped access. Model loading
and HTTP checks remain for the real pipeline deployment. No implementation code
changed, so the earlier offline tests were not rerun. Nothing was committed or pushed.

![Staging placeholder revision with ingress disabled](reports/screenshots/lab5-staging-bootstrap-2026-10-13.jpg)

Image source: [Microsoft Container Apps quickstart](https://learn.microsoft.com/en-us/azure/container-apps/get-started?tabs=bash).

### Release App-scoped deployment role — 2026-10-13

I added `ITCS355 Lab5 Staging Deployer u6688124` to
`id-itcs355-u6688124-lab5-release`. Both the role's assignable scope and its
assignment are limited to `lab5-staging`, not the resource group or other Apps.

The role contains only the three actions used by the staging code:

- `Microsoft.App/containerApps/read` to check the App and deployment status.
- `Microsoft.App/containerApps/write` to update the App.
- `Microsoft.App/containerApps/revisions/read` to check the resulting revision.

There are no wildcards, data actions, App delete, secret-list, or role-management
permissions. App write is still a powerful permission: it can change that App's
image and configuration, not just the image tag. The release identity must stay
trusted, especially when the runtime identity is attached later.

**Checks at 15:03 ICT:** Direct read-back and the App-scoped assignment list
confirmed the release principal, exact scope, and three actions. The Portal
also showed the assignment at **This resource**. The earlier subscription-wide
listing still omitted the new assignment, while all ten earlier assignments
matched the baseline. I did not repeat the writes to address that listing delay.
The saved App and shared Environment configurations were unchanged.

This checks the saved permissions, not deployment by the release identity.
No identity was attached, no model was deployed, and no compute or pipeline job
was created. The placeholder still had one replica at the final check; I have
not verified scale-to-zero. No implementation code changed, tests were not rerun,
and nothing was committed or pushed.

![Release deployment role scoped to lab5-staging](reports/screenshots/lab5-release-staging-deployer-2026-10-13.jpg)

Reference: [Container Apps permissions](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/compute#microsoftapp).

### Pipeline compute created — 2026-10-13

I created `cpu-lab5` from the reviewed draft in Malaysia West. Before creation,
Azure ML listed `Standard_F2s_v2` as supported: 2 vCPUs and 4 GiB memory. Both
the regional Dedicated quota and FSv2 Dedicated quota showed 0 of 16 vCPUs used,
so the one-node plan fit the quota. This does not guarantee capacity for a later job.

**Checks at 15:12 ICT:** Azure reported `Succeeded`, allocation state `Steady`,
and current/target node counts of **0/0**. Read-back matched Dedicated F2s_v2,
minimum 0, maximum 1, and 120 idle seconds before scale-down. Public SSH and
local authentication were disabled; public-node networking stayed as planned.
Only the training and release identities were attached. Runtime was not attached.
The Studio screenshots also show zero current nodes and the saved settings.

**Small check issue:** The quota response used the same family name for Dedicated
and Low-priority rows. My first guard stopped before sending a create request.
Filtering by Dedicated quota type fixed the check; I then sent one create request.

No pipeline, training job, or model deployment was started. No role assignments,
App settings, or shared Environment settings were changed. The compute file and
implementation code were unchanged, so the earlier offline tests were not rerun.
Image pulls, job identity selection, and datastore access still need a real
pipeline check. Sharing a cluster still assumes trusted code, as explained above.
Nothing was committed or pushed.

![cpu-lab5 ready with zero current nodes](reports/screenshots/lab5-cpu-lab5-ready-2026-10-13.jpg)

![cpu-lab5 scale limits and training/release identities](reports/screenshots/lab5-cpu-lab5-settings-identities-2026-10-13.jpg)

Reference: [Azure ML compute clusters and scaling](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-create-attach-compute-cluster?view=azureml-api-2).

### Local staging settings ready — 2026-10-13

I prepared `cloud.env` with the workspace's tracking URI, the Lab 5 runtime
identity, and the approved current public IPv4 restricted to `/32`. The file
has owner-only permissions and is excluded from Git and Docker builds. The IP
must be checked again if the internet connection or address changes.

The serving configuration reuses this existing Lab 4 image:

```text
itcs355u6688124.azurecr.io/itcs355-lab4@sha256:654ed915ca7c12f59b619979f3795c705b82eb2a9ee23620b520e185095c6bb8
```

ACR confirmed that digest is readable and uses `linux/amd64`. The local serving
code and dependency lock have no differences from its source commit `83b1d62`.
The existing ACR ARM-token authentication policy is enabled; no registry settings
were changed. This does not prove the runtime identity can download the model.

**Checks:** The real local settings passed the existing validator with networking
blocked. The 23 configuration and image-recipe tests passed. No implementation
code changed, so I did not repeat the full suite.

The pipeline image is separate from this serving image. Its earlier local build
does not contain the latest storage and registration changes. The updated source
is now saved in commit `c58490cc91d9ec8de9b52651e9ef2fc63e510f61`. Next, we need a new
pipeline image build and ACR push to obtain its manifest digest. Only then can we
compile the final job with that image.

**Before committing:** The 189 focused storage, compute, pipeline, registration,
and training tests passed, with 1,237 existing dependency warnings. Ruff and
`git diff --check` passed too. I then updated the stale compute comments without
changing any configuration values; its three tests passed again.
No image build/push, pipeline run, deployment, or Git push was performed.
