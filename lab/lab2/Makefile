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
export PYTHONHASHSEED := $(SEED)
export IMAGE_URI N_ESTIMATORS MAX_DEPTH MIN_SAMPLES_LEAF SEED
export BUDGET_THB TUNE_TIMEOUT_S TRIALS TRIAL_ESTIMATE_S
export RESUME_FROM TEST_INTERRUPTION

.PHONY: help test portability-audit cloud-check image image-push train-remote tune

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
