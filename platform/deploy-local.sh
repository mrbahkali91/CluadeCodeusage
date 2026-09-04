#!/usr/bin/env bash
# One-command local deployment.
#
# Brings up PostgreSQL, applies migrations, seeds reference data and a
# demonstration corpus, creates the first organisation and user, then serves
# the app. Safe to re-run: seeding and bootstrapping are both idempotent and
# migrations are versioned.
#
#   ./deploy-local.sh            # serve on 127.0.0.1:8000
#   HOST=0.0.0.0 PORT=9000 ./deploy-local.sh
#   RESET=1 ./deploy-local.sh    # drop the schema and rebuild from migrations
#
# Authentication is enforced (it fails closed), so this script configures the
# development password issuer. That issuer must never be enabled next to a
# real identity provider; the application refuses to start if both are set.
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
DB_NAME="${DB_NAME:-sreoi}"
export SREOI_DATABASE_URL="${SREOI_DATABASE_URL:-postgresql+psycopg://sreoi:sreoi@127.0.0.1:5432/${DB_NAME}}"

ADMIN_EMAIL="${ADMIN_EMAIL:-admin@localhost}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "PostgreSQL"
if ! pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  pg_ctlcluster 16 main start 2>/dev/null || service postgresql start 2>/dev/null || true
  for _ in $(seq 1 30); do pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break; sleep 1; done
fi
pg_isready -h 127.0.0.1 -p 5432 || { echo "PostgreSQL is not reachable"; exit 1; }

# The application role is deliberately NOT a superuser: PostgreSQL exempts
# superusers from row-level security unconditionally, and FORCE does not apply
# to them either, so a superuser app role would make every tenant policy
# decorative. See docs/security-architecture.md.
su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='sreoi'\"" 2>/dev/null | grep -q 1 \
  || su postgres -c "psql -qc \"CREATE ROLE sreoi LOGIN PASSWORD 'sreoi' NOSUPERUSER NOBYPASSRLS\""
su postgres -c "psql -qc \"ALTER ROLE sreoi NOSUPERUSER NOBYPASSRLS\""
for db in "${DB_NAME}" "${DB_NAME}_test"; do
  su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${db}'\"" 2>/dev/null | grep -q 1 \
    || su postgres -c "createdb -O sreoi ${db}"
done

if [ "${RESET:-0}" = "1" ]; then
  say "Resetting schema (RESET=1)"
  PGPASSWORD=sreoi psql -h 127.0.0.1 -U sreoi -d "${DB_NAME}" -q \
    -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
fi

# CREATE EXTENSION requires superuser, which the app role no longer has, so a
# superuser installs them once and hands the schema back.
# The test database is provisioned here, not by the test suite. CREATE
# EXTENSION postgis needs superuser, which the app role does not have, and the
# suite skips rather than fails when it cannot reach a PostGIS database -- so
# without this, `make test` on a fresh machine would report a green run with
# every database-backed test, tenant isolation included, silently skipped.
say "Extensions (PostGIS, pg_trgm)"
for db in "${DB_NAME}" "${DB_NAME}_test"; do
  su postgres -c "psql -d ${db} -q \
    -c 'CREATE EXTENSION IF NOT EXISTS postgis' \
    -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm' \
    -c 'ALTER SCHEMA public OWNER TO sreoi' \
    -c 'GRANT ALL ON SCHEMA public TO sreoi'"
done

say "Migrations"
.venv/bin/alembic upgrade head

say "Reference data and demonstration corpus"
# --offline skips the live KAPSARC index pull; drop it to fetch the real index.
.venv/bin/python -m sreoi_pipeline.cli seed ${SEED_ARGS:---offline}
.venv/bin/python -m sreoi_pipeline.cli corpus

say "Identity (organisation and first user)"
BOOTSTRAP_ARGS=(--email "${ADMIN_EMAIL}" --role ORG_ADMIN)
if [ -n "${ADMIN_PASSWORD}" ]; then
  BOOTSTRAP_ARGS+=(--password "${ADMIN_PASSWORD}")
fi
.venv/bin/python -m sreoi_pipeline.identity "${BOOTSTRAP_ARGS[@]}"

say "Source health"
.venv/bin/python -m sreoi_pipeline.cli health || true

# Development password issuer. Enabled only because this script serves a local
# deployment; the secret is per-run so a token from one run cannot be replayed
# against the next.
export SREOI_AUTH_DEV_MODE=1
export SREOI_DEV_TOKEN_SECRET="${SREOI_DEV_TOKEN_SECRET:-$(.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(48))')}"

say "Serving on http://${HOST}:${PORT}"
cat <<BANNER

  Sign in         http://${HOST}:${PORT}/auth/signin
  Opportunities   http://${HOST}:${PORT}/
  Map             http://${HOST}:${PORT}/map
  Watchlists      http://${HOST}:${PORT}/watchlists
  Data quality    http://${HOST}:${PORT}/admin/quality
  Source health   http://${HOST}:${PORT}/admin/sources
  API docs        http://${HOST}:${PORT}/docs

  Every route except /health, /docs and /auth/* requires a credential; sign in
  as ${ADMIN_EMAIL} with the password printed above.

  NOTE: this run uses the DEVELOPMENT password issuer, not a real identity
  provider. Do not expose it beyond localhost. Configure SREOI_OIDC_ISSUER,
  SREOI_OIDC_AUDIENCE and SREOI_OIDC_JWKS_URL for anything else, and leave
  SREOI_AUTH_DEV_MODE unset -- the app refuses to start with both.

BANNER
exec .venv/bin/uvicorn sreoi_api.main:app --host "${HOST}" --port "${PORT}"
