# Lab 3: serving API and host-side deployment commands.
# service/, tests/test_service.py, scripts/portability_audit.py:
# https://github.com/pasdptt/public_teaching_mlaiops @ 317f56ebf8563211d338c372c0db8d3a09e2004d
# src/, cloudlayer/, .dockerignore reused/adapted from this repository's lab/lab2:
# https://github.com/nithit-cypherX/mlops-engineering @ 95a1f8f139a96d06c5c3656bb14f7375bb36e27a
# Task 3 load-test commands reuse the instructor's k6 starter.

SHELL := /bin/bash
.DEFAULT_GOAL := help
PYTHON ?= .venv/bin/python
IMAGE ?= itcs355-lab3
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
PLATFORM ?= linux/amd64

.PHONY: help setup serve serve-image deploy smoke loadtest loadtest-check cold-start cold-start-check batch-compare batch-compare-check payload-compare payload-compare-check test portability-audit

export TARGET VUS DURATION ZERO_REPLICAS_CONFIRMED_AT

help:
	@echo "Lab 3: serving and deployment"
	@echo "  setup              Install hashed dependencies into the Lab 3 environment"
	@echo "  serve              Run the local service using cloud.env (no training)"
	@echo "  serve-image        Build the serving image for linux/amd64"
	@echo "  deploy             Deploy to the prepared environment (creates/updates a live app)"
	@echo "  smoke              Invoke the configured endpoint with three fixed payloads"
	@echo "  loadtest           One warm k6 round; set TARGET, VUS (default 10), DURATION (default 60s)"
	@echo "  loadtest-check     Check the k6 script offline in Docker, without endpoint requests"
	@echo "  cold-start         One prediction after confirming zero replicas; set TARGET and ZERO_REPLICAS_CONFIRMED_AT"
	@echo "  cold-start-check   Check the cold-start script offline in Docker"
	@echo "  batch-compare      Three warm pairs: 100 singles vs one 100-row batch; set TARGET"
	@echo "  batch-compare-check  Check batch comparison offline in Docker"
	@echo "  payload-compare    Four sizes, 20 measured requests each; set TARGET (requires timing headers)"
	@echo "  payload-compare-check  Check payload comparison offline in Docker"
	@echo "  canary-help        Show the guarded Task 4 command (no cloud calls)"
	@echo "  canary-check       Run Task 4 unit tests with fake cloud/HTTP responses"
	@echo "  teardown           Preview reviewed cleanup; CONFIRM=ca-itcs355-u6688124-lab3 enables deletion"
	@echo "  teardown-check     Test the cleanup guards offline (no Azure calls)"
	@echo "  cost-report        Read billing and write reports/lab3-cost.md; COST_END defaults to 2026-09-24"
	@echo "  cost-report-check  Test billing/report handling offline"
	@echo "  test               Run service and adapter tests offline"
	@echo "  portability-audit  Check for provider details outside cloudlayer/"

setup:
	$(PYTHON) -m pip install --require-hashes -r requirements.txt

serve:
	$(PYTHON) -c "from src import config; import uvicorn; uvicorn.run('service.app:app', host='127.0.0.1', port=8080)"

serve-image:
	docker buildx build --platform $(PLATFORM) -f service/Dockerfile.serve -t $(IMAGE):$(TAG) --load .

deploy:
	$(PYTHON) -m scripts.serving deploy

smoke:
	$(PYTHON) -m scripts.serving smoke

loadtest:
	bash scripts/loadtest.sh run

loadtest-check:
	bash scripts/loadtest.sh check

cold-start:
	bash scripts/loadtest.sh cold

cold-start-check:
	bash scripts/loadtest.sh cold-check

batch-compare:
	bash scripts/loadtest.sh batch

batch-compare-check:
	bash scripts/loadtest.sh batch-check

payload-compare:
	bash scripts/loadtest.sh payload

payload-compare-check:
	bash scripts/loadtest.sh payload-check

# Unit tests use offline registry/model stubs, not training or live cloud access.
.PHONY: canary-help canary-check
canary-help:
	$(PYTHON) -m scripts.canary --help

canary-check:
	$(PYTHON) -m pytest -q tests/test_canary.py cloudlayer/tests/test_azure_canary.py

test:
	$(PYTHON) -m pytest -q tests/ cloudlayer/tests/

portability-audit:
	$(PYTHON) scripts/portability_audit.py

# No wildcard/tag-based deletion; no confirmation means a read-only preview.
.PHONY: teardown teardown-check
export CONFIRM
teardown:
	$(PYTHON) -m cloudlayer.teardown_lab3 --confirm "$$CONFIRM"

teardown-check:
	$(PYTHON) -m pytest -q cloudlayer/tests/test_teardown_lab3.py

.PHONY: cost-report cost-report-check
export COST_END
cost-report:
	$(PYTHON) -m scripts.cost_report --end "$$COST_END"

cost-report-check:
	$(PYTHON) -m pytest -q cloudlayer/tests/test_cost_report.py
