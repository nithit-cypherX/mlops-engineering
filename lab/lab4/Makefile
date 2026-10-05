# Lab 4: tests, image publishing and staging commands; cloud setup is required.
# Source: lab/lab3 at 72e081d1e723d3b855b7df57937519234badaaa3.
# service/, src/, base/factory, dependency inputs/locks, .dockerignore and audit
# are reused. Task 2.4 also adapts Lab 3 deploy/invoke and three-payload smoke.
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

.PHONY: help setup data-pull scan-secrets lint test test-unit test-data test-model test-integration serve-image image-push deploy smoke restore-access test-deploy portability-audit
.PHONY: teardown teardown-check cost-report cost-report-check

help:
	@echo "Lab 4: offline, registry-backed and local container tests are separate."
	@echo "  setup              Install the hashed Lab 4 runtime, test and lint dependencies"
	@echo "  data-pull          Download the pinned public CSV and check SHA-256; no cloud login"
	@echo "  scan-secrets       Scan all local Git history; refuses shallow clones"
	@echo "  lint               Check Lab 4 Python code with the instructor's Ruff rules"
	@echo "  test               Run the current offline unit, drift, deployment and data tests"
	@echo "  test-unit          Run offline feature/logging, drift, downloader, deployment and smoke tests"
	@echo "  test-data          Run data-contract and split tests; missing data fails"
	@echo "  test-model         Test the configured registry model; requires config and cloud login"
	@echo "  test-integration   Test IMAGE:TAG locally; build first, set serving config and log in on host"
	@echo "  serve-image        Build the serving image for linux/amd64"
	@echo "  image-push         Push the existing IMAGE:TAG to CONTAINER_REGISTRY; no rebuild"
	@echo "  deploy             Deploy the pinned image; needs prepared Lab 4 resources, config and login"
	@echo "  smoke              Invoke the configured endpoint with three fixed payloads"
	@echo "  restore-access     Remove temporary runner access; keep the configured client IP"
	@echo "  test-deploy        Check deployment guards and smoke handling offline; no cloud calls"
	@echo "  portability-audit  Check provider-neutral code boundaries"
	@echo "  teardown           Preview by default; CONFIRM=itcs355-lab4-staging enables exact-scope deletion"
	@echo "                     Requires Lab 4 CI disabled, no unfinished runs and idle reviewed resources."
	@echo "  teardown-check     Test teardown guards offline; never deletes cloud resources"
	@echo "  cost-report        Read USD billing since 2026-09-29; COST_END defaults to today UTC"
	@echo "  cost-report-check  Test cost-query and report handling offline"
	@echo "  Preview/billing require Azure scope in cloud.env or exported environment variables."

setup:
	$(PYTHON) -m pip install --require-hashes -r requirements.txt

data-pull:
	$(PYTHON) scripts/pull_data.py

scan-secrets:
	$(PYTHON) scripts/scan_secrets.py

lint:
	$(PYTHON) -m ruff check src/ service/ cloudlayer/ monitoring/ scripts/ tests/

test:
	$(PYTHON) -m pytest -q tests/test_features.py tests/test_feature_logging.py tests/test_dashboard.py tests/test_drift.py tests/test_drift_job.py tests/test_drift_snapshot.py tests/test_pull_data.py tests/test_serving_commands.py tests/test_workflow.py cloudlayer/tests/test_deployment.py cloudlayer/tests/test_monitoring.py cloudlayer/tests/test_teardown_lab4.py cloudlayer/tests/test_cost_report.py tests/test_data.py

test-unit:
	$(PYTHON) -m pytest -q tests/test_features.py tests/test_feature_logging.py tests/test_dashboard.py tests/test_drift.py tests/test_drift_job.py tests/test_drift_snapshot.py tests/test_pull_data.py tests/test_serving_commands.py tests/test_workflow.py cloudlayer/tests/test_deployment.py cloudlayer/tests/test_monitoring.py cloudlayer/tests/test_teardown_lab4.py cloudlayer/tests/test_cost_report.py

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

deploy:
	$(PYTHON) -m scripts.serving deploy

smoke:
	$(PYTHON) -m scripts.serving smoke

restore-access:
	$(PYTHON) -m scripts.serving restore-access

test-deploy:
	$(PYTHON) -m pytest -q tests/test_serving_commands.py tests/test_workflow.py cloudlayer/tests/test_deployment.py

portability-audit:
	$(PYTHON) scripts/portability_audit.py

teardown: export CONFIRM ?=
teardown:
	$(PYTHON) -m cloudlayer.teardown_lab4 --confirm "$$CONFIRM"

teardown-check:
	$(PYTHON) -m pytest -q cloudlayer/tests/test_teardown_lab4.py

cost-report: export COST_END ?=
cost-report:
	$(PYTHON) -m scripts.cost_report --end "$$COST_END"

cost-report-check:
	$(PYTHON) -m pytest -q cloudlayer/tests/test_cost_report.py
