#!/usr/bin/env bash
# One-command local deployment.
#
# Brings up PostgreSQL, applies migrations, seeds reference data and a
# demonstration corpus, then serves the app. Safe to re-run: seeding is
# idempotent and migrations are versioned.
#
#   ./deploy-local.sh            # serve on 127.0.0.1:8000
#   HOST=0.0.0.0 PORT=9000 ./deploy-local.sh
#   RESET=1 ./deploy-local.sh    # drop the schema and rebuild from migrations
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
DB_NAME="${DB_NAME:-sreoi}"
export SREOI_DATABASE_URL="${SREOI_DATABASE_URL:-postgresql+psycopg://sreoi:sreoi@127.0.0.1:5432/${DB_NAME}}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "PostgreSQL"
if ! pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  pg_ctlcluster 16 main start 2>/dev/null || service postgresql start 2>/dev/null || true
  for _ in $(seq 1 30); do pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break; sleep 1; done
fi
pg_isready -h 127.0.0.1 -p 5432 || { echo "PostgreSQL is not reachable"; exit 1; }

# Role and database, created only if absent.
su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='sreoi'\"" 2>/dev/null | grep -q 1 \
  || su postgres -c "psql -qc \"CREATE ROLE sreoi LOGIN PASSWORD 'sreoi' SUPERUSER\""
su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'\"" 2>/dev/null | grep -q 1 \
  || su postgres -c "createdb -O sreoi ${DB_NAME}"

if [ "${RESET:-0}" = "1" ]; then
  say "Resetting schema (RESET=1)"
  PGPASSWORD=sreoi psql -h 127.0.0.1 -U sreoi -d "${DB_NAME}" -q \
    -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
fi

say "Extensions (PostGIS, pg_trgm)"
.venv/bin/python -c "from sreoi_persistence.db import ensure_extensions; ensure_extensions()"

say "Migrations"
.venv/bin/alembic upgrade head

say "Reference data and demonstration corpus"
# --offline skips the live KAPSARC index pull; drop it to fetch the real index.
.venv/bin/python -m sreoi_pipeline.cli seed ${SEED_ARGS:---offline}
.venv/bin/python -m sreoi_pipeline.cli corpus

say "Source health"
.venv/bin/python -m sreoi_pipeline.cli health || true

say "Serving on http://${HOST}:${PORT}"
cat <<BANNER

  Opportunities   http://${HOST}:${PORT}/
  Map             http://${HOST}:${PORT}/map
  Watchlists      http://${HOST}:${PORT}/watchlists
  Data quality    http://${HOST}:${PORT}/admin/quality
  Source health   http://${HOST}:${PORT}/admin/sources
  API docs        http://${HOST}:${PORT}/docs

  NOTE: there is no authentication yet. Do not expose this beyond localhost.

BANNER
exec .venv/bin/uvicorn sreoi_api.main:app --host "${HOST}" --port "${PORT}"
