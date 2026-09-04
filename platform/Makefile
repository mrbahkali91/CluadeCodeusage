# Slice 1 developer workflow.
# Every target is safe to run repeatedly.

PY := .venv/bin/python
export SREOI_DATABASE_URL ?= postgresql+psycopg://sreoi:sreoi@127.0.0.1:5432/sreoi

.PHONY: help install up down logs db-only db-up deploy migrate migrate-down seed demo run test lint format typecheck contracts check clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

install:  ## Create the venv and install the project with dev extras
	uv venv .venv --python 3.11
	uv pip install --python .venv/bin/python -e ".[dev]"

up:  ## Docker: build and run the whole stack (http://127.0.0.1:8000)
	docker compose up -d --build
	@echo "Waiting for the app to become healthy..."
	@for i in $$(seq 1 90); do \
	  curl -sf -o /dev/null http://127.0.0.1:8000/health && \
	    { echo "Ready: http://127.0.0.1:8000/auth/signin"; exit 0; }; \
	  sleep 2; \
	done; echo "Not healthy yet -- check: make logs"; exit 1

down:  ## Docker: stop the stack (add CLEAN=1 to delete the database volume)
	docker compose down $(if $(CLEAN),--volumes,)

logs:  ## Docker: follow the stack logs
	docker compose logs -f

db-only:  ## Docker: run just PostGIS, then develop with the local venv
	docker compose up -d db
	@echo "SREOI_DATABASE_URL=postgresql+psycopg://sreoi:sreoi@127.0.0.1:5432/sreoi"

db-up:  ## Start the local PostgreSQL cluster (native install)
	pg_ctlcluster 16 main start || true
	@pg_isready -h 127.0.0.1 -p 5432

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

deploy:  ## One command: Postgres + migrations + seed + serve (http://127.0.0.1:8000)
	./deploy-local.sh

deploy-reset:  ## Same, but rebuild the schema from scratch first
	RESET=1 ./deploy-local.sh

run:  ## Start the API and UI only (assumes DB is already prepared)
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

check: lint typecheck contracts  ## Everything CI runs (a missing test DB fails, never skips)
	SREOI_REQUIRE_DB=1 $(PY) -m pytest -q

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache
