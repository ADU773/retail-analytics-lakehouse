# Convenience targets for Linux/macOS (Windows: use the retail-lakehouse CLI directly).
PYTHON ?= python
CLI := $(PYTHON) -m retail_lakehouse

.PHONY: help install dev run-all generate ingest transform test-dq report benchmark dashboard test lint format clean

help:  ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-12s %s\n", $$1, $$2}'

install:  ## install the package
	$(PYTHON) -m pip install -e .

dev:  ## install with dev + dashboard extras
	$(PYTHON) -m pip install -e ".[dev]"

run-all:  ## generate -> ingest -> transform -> test -> report (default scale)
	$(CLI) run-all

generate:  ## synthetic raw data only
	$(CLI) generate

ingest:  ## raw -> bronze Parquet (idempotent)
	$(CLI) ingest

transform:  ## silver + gold models (SELECT=... to choose models)
	$(CLI) transform $(if $(SELECT),--select $(SELECT),)

test-dq:  ## data-quality tests on all layers
	$(CLI) test

report:  ## docs/index.html, docs/images/*.png, docs/insights.md
	$(CLI) report

benchmark:  ## throughput / RSS / storage benchmark
	$(CLI) benchmark

dashboard:  ## interactive Streamlit dashboard
	$(PYTHON) -m streamlit run app/dashboard.py

test:  ## pytest suite
	$(PYTHON) -m pytest -q

lint:  ## ruff lint + format check
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:  ## apply ruff formatting and safe fixes
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

clean:  ## delete generated data and reports (keeps docs/)
	$(CLI) clean
