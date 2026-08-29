# Slice 1 developer workflow.
# Every target is safe to run repeatedly.

PY := .venv/bin/python
export SREOI_DATABASE_URL ?= postgresql+psycopg://sreoi:sreoi@127.0.0.1:5432/sreoi

.PHONY: help install migrate migrate-down seed demo run test lint format typecheck contracts check clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

install:  ## Create the venv and install the project with dev extras
	uv venv .venv --python 3.11
	uv pip install --python .venv/bin/python -e ".[dev]"

migrate:  ## Apply migrations
	.venv/bin/alembic upgrade head

migrate-down:  ## Roll migrations back to base (verifies reversibility)
	.venv/bin/alembic downgrade base

seed:  ## Load districts, the live KAPSARC index, and the synthetic comparable corpus
	$(PY) -m sreoi_pipeline.cli seed

demo:  ## Ingest four demonstration opportunities and score them
	$(PY) -m sreoi_pipeline.cli demo

reset-demo:  ## Seed and demo in one step
	$(PY) -m sreoi_pipeline.cli reset-and-demo

run:  ## Start the API and UI on http://127.0.0.1:8000
	.venv/bin/uvicorn sreoi_api.main:app --reload --host 127.0.0.1 --port 8000

test:  ## Run the test suite
	$(PY) -m pytest -q

lint:  ## Lint
	.venv/bin/ruff check src tests

format:  ## Auto-fix and format
	.venv/bin/ruff check --fix src tests
	.venv/bin/ruff format src tests

typecheck:  ## mypy strict
	.venv/bin/mypy

contracts:  ## Enforce the architecture boundaries (ADR-001)
	.venv/bin/lint-imports

check: lint typecheck contracts test  ## Everything CI runs

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache
