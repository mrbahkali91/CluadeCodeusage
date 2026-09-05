#!/usr/bin/env bash
# Run the whole Saudi Real Estate Opportunity Intelligence platform locally.
# No Docker, no containers. Three tiers, one command.
#
#   ./run-local.sh              provision if needed, then run all three tiers
#   ./run-local.sh --check      only verify prerequisites, change nothing
#   ./run-local.sh --reset      rebuild the database schema from migrations first
#   ./run-local.sh --no-client  engine + API only (no Vite dev server)
#
# What it starts:
#   Python engine   127.0.0.1:8000   valuation, scoring, credentials
#   NestJS API      127.0.0.1:3000   auth, tenancy, queries
#   React client    127.0.0.1:5173   ← open this one
#
# Ctrl-C stops all three.
#
# PORTABILITY. This deliberately does not assume Debian. Starting PostgreSQL and
# obtaining superuser access differ per platform, and the previous script assumed
# `pg_ctlcluster` and an OS user named `postgres` -- both absent on macOS, where
# Homebrew makes the logged-in user the cluster superuser. Both are probed below
# rather than guessed.
set -euo pipefail
cd "$(dirname "$0")"

CHECK_ONLY=0; RESET=0; RUN_CLIENT=1
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --reset) RESET=1 ;;
    --no-client) RUN_CLIENT=0 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

DB_NAME="${DB_NAME:-sreoi}"
DB_ROLE="${DB_ROLE:-sreoi}"
DB_PASS="${DB_PASS:-sreoi}"
PGHOST_="${PGHOST_:-127.0.0.1}"
PGPORT_="${PGPORT_:-5432}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@localhost}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  x %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- platform
case "$(uname -s)" in
  Darwin) OS=mac ;;
  Linux)  OS=linux ;;
  *)      OS=other ;;
esac

hint() {
  case "$1:$OS" in
    postgres:mac)  echo "brew install postgresql@16 postgis && brew services start postgresql@16" ;;
    postgres:*)    echo "sudo apt install postgresql-16 postgresql-16-postgis-3   # or your distro's equivalent" ;;
    postgis:mac)   echo "brew install postgis" ;;
    postgis:*)     echo "sudo apt install postgresql-16-postgis-3" ;;
    python:mac)    echo "brew install python@3.11" ;;
    python:*)      echo "sudo apt install python3.11 python3.11-venv" ;;
    node:mac)      echo "brew install node@22" ;;
    node:*)        echo "curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install nodejs" ;;
    pnpm:*)        echo "npm install -g pnpm@10" ;;
    bun:*)         echo "curl -fsSL https://bun.sh/install | bash" ;;
    *)             echo "" ;;
  esac
}

MISSING=0
need() { # need <command> <hint-key> <why>
  if command -v "$1" >/dev/null 2>&1; then
    printf '  \033[32mok\033[0m   %-12s %s\n' "$1" "$(command -v "$1")"
  else
    printf '  \033[31mmiss\033[0m %-12s %s\n' "$1" "$3"
    local h; h="$(hint "$2")"
    [ -n "$h" ] && printf '       install: %s\n' "$h"
    MISSING=1
  fi
}

say "Prerequisites ($OS)"
need psql     postgres "PostgreSQL client + server 16"
need pg_isready postgres "PostgreSQL readiness probe"
need python3  python   "Python 3.11+ for the valuation engine"
need node     node     "Node 20+ for the API and client"
need pnpm     pnpm     "pnpm 10 (workspace package manager)"
# bun runs the NestJS API: Node's --experimental-strip-types erases types but
# does not transform decorators, which NestJS dependency injection needs.
need bun      bun      "runs the NestJS API (decorators + emitDecoratorMetadata)"

if [ "$MISSING" -eq 1 ]; then
  echo
  die "install the missing tools above, then re-run. Nothing was changed."
fi

# Refuse to start if a port is already taken, BEFORE provisioning anything.
# `wait_for` below polls a health endpoint, which cannot tell this run's process
# from a stranger's -- so a stale server on 8000 gets silently adopted and the
# whole stack is then verified against the wrong build. That happened during
# development twice; it is not a hypothetical.
port_taken() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; return 0; }; return 1; }
BUSY=0
for spec in "8000:Python engine" "3000:NestJS API" "5173:Vite client"; do
  port="${spec%%:*}"; what="${spec#*:}"
  if [ "$port" = "5173" ] && [ "$RUN_CLIENT" -eq 0 ]; then continue; fi
  if port_taken "$port"; then
    printf '  \033[31mbusy\033[0m %-12s port %s is already in use\n' "$what" "$port"
    BUSY=1
  fi
done
if [ "$BUSY" -eq 1 ]; then
  echo
  echo "  Free them first. To find and stop the holder:"
  echo "    lsof -nP -iTCP:8000 -sTCP:LISTEN     # macOS and Linux"
  echo "    kill \$(lsof -t -iTCP:8000 -sTCP:LISTEN)"
  die "ports in use. Nothing was changed."
fi

PYV="$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
case "$PYV" in
  3.1[1-9]|3.[2-9]*) printf '  \033[32mok\033[0m   python3      %s\n' "$PYV" ;;
  *) die "python3 is $PYV; the engine needs 3.11 or newer. $(hint python)" ;;
esac

# ---------------------------------------------------------------- postgres
say "PostgreSQL"
start_postgres() {
  pg_isready -h "$PGHOST_" -p "$PGPORT_" >/dev/null 2>&1 && return 0
  warn "not accepting connections; trying to start it"
  if [ "$OS" = mac ] && command -v brew >/dev/null 2>&1; then
    brew services start postgresql@16 >/dev/null 2>&1 \
      || brew services start postgresql >/dev/null 2>&1 || true
  elif command -v pg_ctlcluster >/dev/null 2>&1; then
    pg_ctlcluster 16 main start 2>/dev/null \
      || sudo pg_ctlcluster 16 main start 2>/dev/null || true
  elif command -v brew >/dev/null 2>&1; then
    brew services start postgresql >/dev/null 2>&1 || true
  else
    sudo service postgresql start 2>/dev/null || true
  fi
  for _ in $(seq 1 30); do
    pg_isready -h "$PGHOST_" -p "$PGPORT_" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
start_postgres || die "could not reach PostgreSQL at $PGHOST_:$PGPORT_. Start it, then re-run. $(hint postgres)"
pg_isready -h "$PGHOST_" -p "$PGPORT_"

# A superuser is needed exactly twice: to create the application role, and to
# install PostGIS (CREATE EXTENSION postgis is not a "trusted" extension, so the
# app role can never install it itself). Probe the ways a superuser session is
# obtained rather than assuming one.
SUPER=""
probe_super() {
  if psql -h "$PGHOST_" -p "$PGPORT_" -d postgres -tAc 'SELECT 1' >/dev/null 2>&1 \
     && [ "$(psql -h "$PGHOST_" -p "$PGPORT_" -d postgres -tAc 'SELECT rolsuper FROM pg_roles WHERE rolname=current_user')" = "t" ]; then
    SUPER="psql -h $PGHOST_ -p $PGPORT_ -d postgres"; return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; then
    SUPER="sudo -u postgres psql"; return 0
  fi
  if [ "$(id -u)" = "0" ] && su postgres -c "psql -tAc 'SELECT 1'" >/dev/null 2>&1; then
    SUPER="su_postgres"; return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; then
    SUPER="sudo -u postgres psql"; return 0
  fi
  return 1
}
super_sql() { # run SQL as superuser against a named database
  local db="$1"; shift
  if [ "$SUPER" = "su_postgres" ]; then
    su postgres -c "psql -v ON_ERROR_STOP=1 -q -d $db -c \"$*\""
  else
    $SUPER -v ON_ERROR_STOP=1 -q -d "$db" -c "$*" 2>/dev/null \
      || eval "${SUPER/-d postgres/-d $db}" -v ON_ERROR_STOP=1 -q -c "\"$*\""
  fi
}
super_query() { # read one value as superuser
  if [ "$SUPER" = "su_postgres" ]; then
    su postgres -c "psql -tAc \"$*\""
  else
    $SUPER -tAc "$*"
  fi
}

probe_super || die "no superuser access to PostgreSQL. On macOS your own account is
     usually the cluster superuser; on Linux try: sudo -u postgres psql"
printf '  superuser via: %s\n' "$SUPER"

if [ "$CHECK_ONLY" -eq 1 ]; then
  say "Check only — nothing was changed"
  bold "All prerequisites satisfied. Run ./run-local.sh to start the platform."
  exit 0
fi

say "Database role and databases"
# NOSUPERUSER NOBYPASSRLS is load-bearing: PostgreSQL exempts superusers from
# row-level security unconditionally and FORCE does not apply to them, so a
# superuser app role leaves all sixteen tenant policies enforced against nobody.
if [ "$(super_query "SELECT 1 FROM pg_roles WHERE rolname='${DB_ROLE}'")" != "1" ]; then
  super_sql postgres "CREATE ROLE ${DB_ROLE} LOGIN PASSWORD '${DB_PASS}' NOSUPERUSER NOBYPASSRLS"
  echo "  created role ${DB_ROLE}"
else
  echo "  role ${DB_ROLE} exists"
fi
super_sql postgres "ALTER ROLE ${DB_ROLE} NOSUPERUSER NOBYPASSRLS"

# The test database is provisioned here, not by pytest: the suite SKIPS rather
# than fails when PostGIS is unreachable, so without it `make test` reports a
# green run with every database-backed test -- tenant isolation included --
# silently skipped.
for db in "${DB_NAME}" "${DB_NAME}_test"; do
  if [ "$(super_query "SELECT 1 FROM pg_database WHERE datname='${db}'")" != "1" ]; then
    super_sql postgres "CREATE DATABASE \\\"${db}\\\" OWNER ${DB_ROLE}"
    echo "  created database ${db}"
  fi
done

if [ "$RESET" -eq 1 ]; then
  say "Rebuilding schema (--reset)"
  PGPASSWORD="$DB_PASS" psql -h "$PGHOST_" -p "$PGPORT_" -U "$DB_ROLE" -d "$DB_NAME" -q \
    -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
fi

say "Extensions (PostGIS, pg_trgm)"
for db in "${DB_NAME}" "${DB_NAME}_test"; do
  super_sql "$db" "CREATE EXTENSION IF NOT EXISTS postgis" \
    || die "could not install PostGIS in ${db}. $(hint postgis)"
  super_sql "$db" "CREATE EXTENSION IF NOT EXISTS pg_trgm"
  super_sql "$db" "ALTER SCHEMA public OWNER TO ${DB_ROLE}"
  super_sql "$db" "GRANT ALL ON SCHEMA public TO ${DB_ROLE}"
  echo "  ${db}: postgis + pg_trgm"
done

export SREOI_DATABASE_URL="postgresql+psycopg://${DB_ROLE}:${DB_PASS}@${PGHOST_}:${PGPORT_}/${DB_NAME}"

# ------------------------------------------------------------------ python
say "Python engine"
cd platform
if [ ! -x .venv/bin/python ]; then
  echo "  creating .venv"
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv --python 3.11 >/dev/null
    uv pip install --python .venv/bin/python -e ".[dev]" >/dev/null
  else
    python3 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -e ".[dev]"
  fi
fi
echo "  migrations"; .venv/bin/alembic upgrade head >/dev/null
echo "  seed + demonstration corpus"
.venv/bin/python -m sreoi_pipeline.cli seed ${SEED_ARGS:---offline} >/dev/null
.venv/bin/python -m sreoi_pipeline.cli corpus >/dev/null
cd ..

# ------------------------------------------------------------------- node
say "Node workspace"
if [ ! -d node_modules ]; then
  echo "  pnpm install"; pnpm install --silent
else
  echo "  node_modules present"
fi

# ---------------------------------------------------------------- identity
say "Identity"
# The development password issuer. Local only; the app refuses to start if this
# is set alongside a configured OIDC issuer. The secret is per-run, so a token
# from a previous run cannot be replayed against this one.
export SREOI_AUTH_DEV_MODE=1
export SREOI_DEV_TOKEN_SECRET="${SREOI_DEV_TOKEN_SECRET:-$(platform/.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(48))')}"
BOOT=(--email "$ADMIN_EMAIL" --role ORG_ADMIN)
[ -n "$ADMIN_PASSWORD" ] && BOOT+=(--password "$ADMIN_PASSWORD")
( cd platform && .venv/bin/python -m sreoi_pipeline.identity "${BOOT[@]}" )

# ------------------------------------------------------------------- serve
PIDS=()
cleanup() {
  echo
  say "Stopping"
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait_for() { # wait_for <url> <label>
  for _ in $(seq 1 60); do
    curl -sf -o /dev/null "$1" && { printf '  \033[32mup\033[0m   %s\n' "$2"; return 0; }
    sleep 1
  done
  warn "$2 did not come up; see the log above"
  return 1
}

say "Starting three tiers"
( cd platform && exec .venv/bin/uvicorn sreoi_api.main:app --host 127.0.0.1 --port 8000 ) &
PIDS+=($!)
wait_for http://127.0.0.1:8000/health "engine   http://127.0.0.1:8000"

export SREOI_ENGINE_URL="http://127.0.0.1:8000"
( cd apps/sreoi-api && exec bun -b ./src/main.ts ) &
PIDS+=($!)
wait_for http://127.0.0.1:3000/health "api      http://127.0.0.1:3000"

if [ "$RUN_CLIENT" -eq 1 ]; then
  ( cd apps/sreoi-web && exec pnpm dev ) &
  PIDS+=($!)
  wait_for http://127.0.0.1:5173/ "client   http://127.0.0.1:5173"
fi

cat <<BANNER

  ────────────────────────────────────────────────────────────────
   Open   $([ "$RUN_CLIENT" -eq 1 ] && echo "http://127.0.0.1:5173" || echo "http://127.0.0.1:3000/health")
   Sign in as ${ADMIN_EMAIL} with the password printed above.
  ────────────────────────────────────────────────────────────────

  Everything binds to 127.0.0.1, never 0.0.0.0: this run uses the
  DEVELOPMENT password issuer, not an identity provider. For anything
  else set SREOI_OIDC_ISSUER / _AUDIENCE / _JWKS_URL and leave
  SREOI_AUTH_DEV_MODE unset -- the app refuses to start with both.

  Ctrl-C stops all tiers.

BANNER

wait
