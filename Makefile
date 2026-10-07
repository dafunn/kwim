# Task runner. Targets are self-describing via `make help`.

VENV    := services/api/.venv
PY      := $(VENV)/bin/python

.PHONY: help venv test test-api test-distiller test-client lint fmt check clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Build the dev virtualenv from requirements-dev.txt
	python3 -m venv $(VENV)
	$(PY) -m pip install -q -r services/api/requirements-dev.txt

# Collected paths and pythonpath are in pytest.ini.
test: ## Run the test suite (API + distiller)
	$(PY) -m pytest

test-api: ## Run only the API suite
	$(PY) -m pytest services/api/tests

test-distiller: ## Run only the distiller suite
	$(PY) -m pytest services/distiller/tests

test-client: ## Run the client's tests (plain scripts, no pytest)
	cd clients/python && ../../$(PY) test_kwim.py && ../../$(PY) test_llm_router.py

lint: ## Lint the tree (ruff)
	$(PY) -m ruff check .

fmt: ## Apply ruff's safe autofixes
	$(PY) -m ruff check . --fix

check: lint test test-client ## Lint, then run every suite

clean: ## Remove caches and build output
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name .pytest_cache -type d -prune -exec rm -rf {} +
	rm -rf clients/python/dist clients/python/build
