PYTHON ?= python3

.PHONY: portability-audit test lint

portability-audit:
	$(PYTHON) scripts/portability_audit.py

test:
	PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONHASHSEED=20260101 $(PYTHON) -m pytest -q -p no:cacheprovider tests cloudlayer/tests

lint:
	$(PYTHON) -m ruff check --no-cache --config ../lab4/ruff.toml src cloudlayer scripts tests conftest.py
