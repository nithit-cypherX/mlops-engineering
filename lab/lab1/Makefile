# ITCS355 Lab 1
# `make reproduce` is the one command a grader runs. Keep it working.

SHELL := /bin/bash
IMAGE ?= itcs355-lab1
TAG   ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
PLATFORM ?= linux/amd64
SEED ?= 20260101
# Python reads this at process startup; keep it aligned with --seed.
export PYTHONHASHSEED := $(SEED)

.PHONY: help setup cloud-check data data-pull test portability-audit train image image-push reproduce verify clean teardown

help:
	@grep -E "^[a-zA-Z_-]+:.*?## .*$$" $(MAKEFILE_LIST) | awk -F":.*?## " "{printf \"  %-20s %s\\n\", \$$1, \$$2}"

setup: ## Install dependencies and print environment status
	python -m pip install --upgrade pip
	pip install -r requirements.txt
	@echo "environment ok"

cloud-check: ## Resolve the eight capability slots
	python scripts/cloud_check.py

data: ## Generate the default dataset (deterministic)
	python scripts/make_dataset.py --seed $(SEED)

test: ## Run data contract and split property tests
	pytest -q tests/

portability-audit: ## Fail if provider strings leak into src/
	python scripts/portability_audit.py

train: ## Train locally, outside the container
	python -m src.train --seed $(SEED) --metrics-out reports/metrics.json

image: ## Build the training image for linux/amd64
	@revision="$$(git rev-parse HEAD)" || exit 1; \
	status="$$(git status --porcelain --untracked-files=normal)" || exit 1; \
	if [ -n "$$status" ]; then revision="$$revision-dirty"; fi; \
	docker buildx build --platform $(PLATFORM) --build-arg GIT_COMMIT="$$revision" -t $(IMAGE):$(TAG) --load .

image-push: image ## Push to CONTAINER_REGISTRY via your adapter
	python -c "from src import config; from cloudlayer.factory import get_adapter; \
	print(get_adapter(config.load()).push_image(\"$(IMAGE):$(TAG)\"))"

data-pull: image ## Download the tracked data anonymously, using DVC in Docker
	@test "$$(id -u)" -ne 0 || { echo "Run reproduce as a non-root user."; exit 1; }
	docker run --rm --platform $(PLATFORM) \
	  --user "$$(id -u):$$(id -g)" \
	  --mount "type=bind,src=$$PWD/.dvc/config,dst=/inputs/dvc-config,readonly" \
	  --mount "type=bind,src=$$PWD/data/raw.dvc,dst=/inputs/raw.dvc,readonly" \
	  --mount "type=bind,src=$$PWD/data,dst=/app/data" \
	  --entrypoint sh $(IMAGE):$(TAG) \
	  /app/scripts/pull_data.sh /inputs/dvc-config /inputs/raw.dvc /app/data

reproduce: data-pull ## THE ONE COMMAND. Grader runs this.
	@mkdir -p reports
	@test "$$(id -u)" -ne 0 || { echo "Run reproduce as a non-root user."; exit 1; }
	# Match bind-mount ownership; keep MLflow's relative artifacts under reports.
	docker run --rm --platform $(PLATFORM) --network none \
	  --user "$$(id -u):$$(id -g)" \
	  --workdir /app/reports \
	  -v "$$PWD/data:/app/data:ro" \
	  -v "$$PWD/reports:/app/reports" \
	  -e MLFLOW_TRACKING_URI=sqlite:////app/reports/mlflow.db \
	  -e PYTHONHASHSEED=$(SEED) \
	  $(IMAGE):$(TAG) --seed $(SEED) \
	  --n-estimators 200 --max-depth 8 --min-samples-leaf 10 \
	  --metrics-out /app/reports/metrics.json
	$(MAKE) verify

verify: ## Check the produced metric against the README claim
	docker run --rm --platform $(PLATFORM) --network none \
	  --user "$$(id -u):$$(id -g)" \
	  --mount "type=bind,src=$$PWD/reports,dst=/app/reports,readonly" \
	  --mount "type=bind,src=$$PWD/README.md,dst=/app/README.md,readonly" \
	  --entrypoint python $(IMAGE):$(TAG) /app/scripts/verify_metric.py

teardown: ## Delete every resource tagged course=itcs355 for this lab
	python -c "from src import config; from cloudlayer.factory import get_adapter; \
	cfg=config.load(); print(get_adapter(cfg).teardown(cfg.tags(1)))"

clean: ## Remove local artifacts
	rm -rf mlruns mlartifacts mlflow.db reports/metrics.json .pytest_cache
