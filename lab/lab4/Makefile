# Lab 4 Task 1.1: serving baseline only, not a completed test suite or deployment.
# Source: lab/lab3 at 72e081d1e723d3b855b7df57937519234badaaa3.
# service/, src/, base/factory, dependency inputs/locks, .dockerignore and audit
# are reused unchanged. azure.py keeps only Blob, push-image and model-loading methods.
# No cloud.env, local environment, model or previous evidence is copied.
# data/raw.dvc and tests/test_data.py come from lab/lab2 at the same commit.
# Missing data now fails the tests instead of skipping them.
# Task 1.2 adds offline feature checks, known-content hashes and bad-data cases.
# Task 1.3 adds a separate registry-backed model test target; no retraining.
# Task 1.4 adds a local container test; build first, then test that exact image.
# Task 2.1: reuse the instructor's history scanner at 2e29faeb1fdaa6360947808e32b0e3c1444113f2.

SHELL := /bin/bash
.DEFAULT_GOAL := help
PYTHON ?= .venv/bin/python
IMAGE ?= itcs355-lab4
TAG ?= $(shell git rev-parse HEAD)
PLATFORM ?= linux/amd64

.PHONY: help setup data-pull scan-secrets lint test test-unit test-data test-model test-integration serve-image image-push portability-audit

help:
	@echo "Lab 4: offline, registry-backed and local container tests are separate."
	@echo "  setup              Install the hashed Lab 4 runtime, test and lint dependencies"
	@echo "  data-pull          Download the pinned public CSV and check SHA-256; no cloud login"
	@echo "  scan-secrets       Scan all local Git history; refuses shallow clones"
	@echo "  lint               Check Lab 4 Python code with the instructor's Ruff rules"
	@echo "  test               Run the current offline unit and data tests"
	@echo "  test-unit          Run offline feature-preparation unit tests"
	@echo "  test-data          Run data-contract and split tests; missing data fails"
	@echo "  test-model         Test the configured registry model; requires config and cloud login"
	@echo "  test-integration   Test IMAGE:TAG locally; build first, set serving config and log in on host"
	@echo "  serve-image        Build the serving image for linux/amd64"
	@echo "  image-push         Push the existing IMAGE:TAG to CONTAINER_REGISTRY; no rebuild"
	@echo "  portability-audit  Check provider-neutral code boundaries"

setup:
	$(PYTHON) -m pip install --require-hashes -r requirements.txt

data-pull:
	$(PYTHON) scripts/pull_data.py

scan-secrets:
	$(PYTHON) scripts/scan_secrets.py

lint:
	$(PYTHON) -m ruff check src/ service/ cloudlayer/ scripts/ tests/

test:
	$(PYTHON) -m pytest -q tests/test_features.py tests/test_pull_data.py tests/test_data.py

test-unit:
	$(PYTHON) -m pytest -q tests/test_features.py tests/test_pull_data.py

test-data:
	$(PYTHON) -m pytest -q tests/test_data.py

test-model:
	$(PYTHON) -m pytest -q -rP tests/test_model_behaviour.py

test-integration:
	INTEGRATION_IMAGE='$(IMAGE):$(TAG)' $(PYTHON) -m pytest -q -rP tests/test_integration.py

serve-image:
	docker buildx build --platform $(PLATFORM) -f service/Dockerfile.serve -t $(IMAGE):$(TAG) --load .

image-push:
	$(PYTHON) -c 'import sys; from src.config import load; from cloudlayer.factory import get_adapter; print(get_adapter(load(strict=False)).push_image(sys.argv[1]))' '$(IMAGE):$(TAG)'

portability-audit:
	$(PYTHON) scripts/portability_audit.py
