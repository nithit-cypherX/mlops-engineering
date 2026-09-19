# Lab 2 commands. Managed training requires an explicit image digest.
SHELL := /bin/bash
PYTHON ?= python
IMAGE ?= itcs355-lab2
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
PLATFORM ?= linux/amd64
IMAGE_URI ?=
N_ESTIMATORS ?= 200
MAX_DEPTH ?= 8
MIN_SAMPLES_LEAF ?= 5
SEED ?= 20260101
BUDGET_THB ?=
TUNE_TIMEOUT_S ?=
TRIALS ?= 12
TRIAL_ESTIMATE_S ?= 600
RESUME_FROM ?=
# Test only: 1 fails after four checkpointed trials; keep 0 for normal/resumed jobs.
TEST_INTERRUPTION ?= 0
STUDY_ID ?=
export PYTHONHASHSEED := $(SEED)
export IMAGE_URI N_ESTIMATORS MAX_DEPTH MIN_SAMPLES_LEAF SEED
export BUDGET_THB TUNE_TIMEOUT_S TRIALS TRIAL_ESTIMATE_S
export RESUME_FROM TEST_INTERRUPTION
export STUDY_ID

.PHONY: help test portability-audit cloud-check image image-push train-remote tune compare reload-check cost-report teardown

help: ## List the available preparation commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F ':.*?## ' '{printf "  %-20s %s\n", $$1, $$2}'

test: ## Run inherited tests after retrieving the tracked dataset
	@test -f data/raw/sensors.csv || { echo "Missing data/raw/sensors.csv: retrieve the tracked dataset before make test."; exit 1; }
	$(PYTHON) -m pytest -q tests/ cloudlayer/

portability-audit: ## Check that provider details stay outside the training code
	$(PYTHON) scripts/portability_audit.py

cloud-check: ## Check configured capability slots and local CLI availability
	$(PYTHON) scripts/cloud_check.py

image: ## Build the inherited training image for linux/amd64
	@revision="$$(git rev-parse HEAD)" || exit 1; \
	status="$$(git status --porcelain --untracked-files=normal)" || exit 1; \
	if [ -n "$$status" ]; then revision="$$revision-dirty"; fi; \
	docker buildx build --platform $(PLATFORM) --build-arg GIT_COMMIT="$$revision" -t $(IMAGE):$(TAG) --load .

image-push: image ## Push the image via the configured Azure adapter
	$(PYTHON) -c 'from src import config; from cloudlayer.factory import get_adapter; print(get_adapter(config.load()).push_image("$(IMAGE):$(TAG)"))'

train-remote: ## Submit one digest-pinned training image and wait for the result
	@test -n "$$IMAGE_URI" || { echo 'Set IMAGE_URI to a digest-pinned reference: registry/repo@sha256:...'; exit 1; }
	$(PYTHON) scripts/train_remote.py --image-uri "$$IMAGE_URI" \
		--n-estimators "$$N_ESTIMATORS" --max-depth "$$MAX_DEPTH" \
		--min-samples-leaf "$$MIN_SAMPLES_LEAF" --seed "$$SEED"

tune: ## Submit a study; requires IMAGE_URI/BUDGET_THB/TUNE_TIMEOUT_S; optional RESUME_FROM or TEST_INTERRUPTION=1 (new study only)
	@test -n "$$IMAGE_URI" || { echo 'Set IMAGE_URI to a digest-pinned image containing src.tune'; exit 1; }
	@test -n "$$BUDGET_THB" || { echo 'Set BUDGET_THB to an explicit study allocation'; exit 1; }
	@test -n "$$TUNE_TIMEOUT_S" || { echo 'Set TUNE_TIMEOUT_S to the Azure job run limit in seconds'; exit 1; }
	@case "$$TEST_INTERRUPTION" in 0|1) ;; *) echo 'TEST_INTERRUPTION must be 0 or 1'; exit 1 ;; esac
	$(PYTHON) scripts/tune_remote.py --image-uri "$$IMAGE_URI" \
		--budget-thb "$$BUDGET_THB" --timeout-s "$$TUNE_TIMEOUT_S" \
		--trials "$$TRIALS" --trial-estimate-s "$$TRIAL_ESTIMATE_S" --seed "$$SEED" \
		--resume-from "$$RESUME_FROM" $(if $(filter 1,$(TEST_INTERRUPTION)),--test-interruption,)

compare: ## Read one finished study from MLflow and write reports/lab2-comparison.md; no training
	@test -n "$$STUDY_ID" || { echo 'Set STUDY_ID to the study ID from the checkpoint.'; exit 1; }
	$(PYTHON) scripts/compare_runs.py --study-id "$$STUDY_ID"

# Keep these exports local to reload-check so other targets still read cloud.env.
reload-check: export MODEL_REGISTRY_NAME ?=
reload-check: export VERSION ?=
reload-check: ## Reload five held-out rows; requires MODEL_REGISTRY_NAME and numeric VERSION
	@test -n "$$MODEL_REGISTRY_NAME" || { echo 'Set MODEL_REGISTRY_NAME to the registered model name.'; exit 1; }
	@test -n "$$VERSION" || { echo 'Set VERSION to an explicit model version number.'; exit 1; }
	$(PYTHON) scripts/reload_check.py --name "$$MODEL_REGISTRY_NAME" --version "$$VERSION"

cost-report: export COST_END ?=
cost-report: ## Read Azure spend since 2026-09-17; optional COST_END=YYYY-MM-DD (UTC)
	$(PYTHON) scripts/cost_report.py --end "$$COST_END"

teardown: ## Delete only the reviewed cpu-lab2 compute; keep jobs, models and shared resources
	$(PYTHON) -m cloudlayer.teardown_lab2
