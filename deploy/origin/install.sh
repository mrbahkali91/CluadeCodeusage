#!/usr/bin/env bash
# Provision the SREOI origin on a Linux server. No Docker.
#
#   sudo ./install.sh --check            verify prerequisites, change nothing
#   sudo ./install.sh                    provision, install services, start
#   sudo ./install.sh --seed             ...and load the synthetic demo corpus
#   sudo ./install.sh --install-packages ...and apt-get the missing prerequisites
#
# WHAT THIS BUILDS
#   PostgreSQL 16 + PostGIS   local socket / 127.0.0.1:5432
#   sreoi-engine.service      127.0.0.1:8000   valuation, scoring, credentials
#   sreoi-api.service         127.0.0.1:3000   auth, tenancy, queries
#   cloudflared               outbound only    the ONLY route in from outside
#
# NOTHING LISTENS ON A PUBLIC INTERFACE. cloudflared dials out to Cloudflare
# and receives requests over that connection, so this host needs no inbound
# firewall rule and no TLS certificate of its own, and a port scan of it finds
# nothing. That is the whole reason the deployment is shaped this way.
#
# Re-running is safe. Every step checks before it acts.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(cd ../.. && pwd)"

CHECK_ONLY=0; SEED=0; INSTALL_PACKAGES=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --seed) SEED=1 ;;
    --install-packages) INSTALL_PACKAGES=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

TARGET=/opt/sreoi
ENVDIR=/etc/sreoi
SERVICE_USER=sreoi
DB_NAME=sreoi
DB_ROLE=sreoi

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m    %s\n' "$*"; }
die()  { printf '  \033[31mx\033[0m    %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------- checks
say "Prerequisites"

[ "$(id -u)" -eq 0 ] || die "run with sudo: this creates a system user, writes /etc/sreoi and installs systemd units"

# `command -v systemctl` is not the check. Containers ship the binary without
# running systemd as PID 1, and the difference only surfaced halfway through a
# run -- after a system user had been created and the whole repository copied --
# as "Failed to connect to bus: Host is down". Probe the bus, not the binary.
command -v systemctl >/dev/null 2>&1 \
  || die "no systemctl. This script targets a systemd Linux server."
systemctl is-system-running >/dev/null 2>&1 || case "$(systemctl is-system-running 2>&1)" in
  degraded|starting|running|maintenance) : ;;
  *) die "systemd is not running as PID 1 here ($(systemctl is-system-running 2>&1 | head -1)). This script installs systemd units; run it on a real server." ;;
esac

MISSING=()
need() { command -v "$1" >/dev/null 2>&1 || MISSING+=("$2"); }
need psql        "postgresql-16 postgresql-16-postgis-3"
need python3     "python3 python3-venv"
need curl        "curl"

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; then
  MISSING+=("python3.11-or-newer")
fi
if ! python3 -c 'import venv' 2>/dev/null; then
  MISSING+=("python3-venv")
fi

if [ "${#MISSING[@]}" -gt 0 ]; then
  if [ "$INSTALL_PACKAGES" -eq 1 ] && command -v apt-get >/dev/null 2>&1; then
    say "Installing system packages"
    # Only with the explicit flag. Installing system packages on someone's
    # server as a side effect of "deploy my app" is not a decision this script
    # gets to make quietly.
    apt-get update -qq
    apt-get install -y --no-install-recommends \
      postgresql-16 postgresql-16-postgis-3 python3 python3-venv python3-dev \
      build-essential libpq-dev curl unzip
    ok "packages installed"
  else
    printf '  missing: %s\n' "${MISSING[*]}"
    die "install them, or re-run with --install-packages (apt only)"
  fi
else
  ok "postgres, python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])'), curl"
fi

# Reachability, checked here rather than in the Database section. It used to be
# checked after the service account had been created and the repository copied,
# so an unstarted cluster left half a deployment behind before saying so.
if ! systemctl is-active --quiet postgresql; then
  if [ "$CHECK_ONLY" -eq 1 ]; then
    warn "postgresql is not running (would be started)"
  else
    systemctl start postgresql || die "could not start postgresql"
    systemctl enable --quiet postgresql 2>/dev/null || true
  fi
fi
if ! su - postgres -c 'psql -qtAX -c "SELECT 1"' >/dev/null 2>&1; then
  die "cannot reach PostgreSQL as the local postgres user over the unix socket"
fi
ok "postgresql reachable as superuser over the unix socket"

# Bun runs the API; pnpm installs its dependencies. Both are resolved to
# absolute paths, and both are checked BY RUNNING THEM AS THE SERVICE ACCOUNT.
#
# That last part is the whole point. `command -v pnpm` succeeding for root
# proves nothing: on the machine this was written on, pnpm lived on root's PATH
# only and /usr/local/bin/bun was a symlink into /root, which the unprivileged
# service account cannot even traverse. Both checks passed, and then the build
# step failed with "pnpm: command not found" and the systemd unit would have
# failed at first boot with a path that works fine when a human tests it.
# There is ONE definition of "run this as the service account", declared after
# the tool paths are known and used by both the preflight check and every build
# step below. Three separate attempts at this drifted from each other and each
# failed for a reason the real invocation did not have: no HOME (pnpm mkdir
# EACCES under a home that does not exist), then no PATH (pnpm re-spawns itself
# to honour `packageManager` and got ENOENT). A check that fails differently
# from the thing it checks is worse than no check, so they share one function.
as_service() { # as_service <workdir> <command string>
  su "$SERVICE_USER" -s /bin/bash -c \
    "cd '$1' && export HOME='$STATE' PATH='$TOOLPATH':\"\$PATH\" && $2"
}

if [ ! -x /usr/local/bin/bun ] && [ "$CHECK_ONLY" -eq 0 ]; then
  say "Installing bun"
  # Into /usr/local so the unit's ExecStart is a real file on a stable path,
  # not a link into somebody's home directory.
  BUN_INSTALL=/usr/local bash -c 'curl -fsSL https://bun.sh/install | bash' >/dev/null
fi
BUN=/usr/local/bin/bun
[ -x "$BUN" ] || BUN="$(command -v bun 2>/dev/null || true)"
[ -n "$BUN" ] || die "bun is not installed. Re-run without --check to install it."

PNPM="$(command -v pnpm 2>/dev/null || true)"
if [ -z "$PNPM" ] && [ "$CHECK_ONLY" -eq 0 ]; then
  say "Installing pnpm"
  "$BUN" install -g pnpm >/dev/null 2>&1 || true
  PNPM="$(command -v pnpm 2>/dev/null || true)"
fi
[ -n "$PNPM" ] || die "pnpm is not installed (npm i -g pnpm)"
# NOT `readlink -f`. pnpm's entry point is a symlink to a `.cjs` file, and
# resolving it yields a path that is not directly executable -- the check below
# then fails for a reason that has nothing to do with the actual problem. The
# wrapper as found is what must be runnable; where it points is diagnostics.
TOOLPATH="$(dirname "$BUN"):$(dirname "$PNPM")"

# ---------------------------------------------------------------------- env
say "Configuration"
[ -f .env ] || die ".env is missing. Copy .env.example to .env and fill it in first."
# shellcheck disable=SC1091
set -a; . ./.env; set +a

require() {
  local name="$1"
  [ -n "${!name:-}" ] || die "$name is empty in .env"
}
require SREOI_DB_PASSWORD
require PUBLIC_ORIGIN
require CLOUDFLARE_TUNNEL_TOKEN

# One of the two authentication modes, never both and never neither. The
# engine refuses to start with both configured; refusing here as well means
# the failure names the file to fix rather than appearing in a service log.
if [ -n "${SREOI_OIDC_ISSUER:-}" ] && [ -n "${SREOI_AUTH_DEV_MODE:-}" ]; then
  die "SREOI_OIDC_ISSUER and SREOI_AUTH_DEV_MODE are both set. Choose one."
fi
if [ -z "${SREOI_OIDC_ISSUER:-}" ] && [ -z "${SREOI_AUTH_DEV_MODE:-}" ]; then
  die "neither SREOI_OIDC_ISSUER nor SREOI_AUTH_DEV_MODE is set: the app would serve nothing"
fi
if [ -n "${SREOI_AUTH_DEV_MODE:-}" ]; then
  # 32 bytes is the engine's own minimum for the HS256 signing key.
  [ "${#SREOI_DEV_TOKEN_SECRET}" -ge 32 ] \
    || die "SREOI_DEV_TOKEN_SECRET must be at least 32 characters (openssl rand -base64 48)"
  warn "using the DEVELOPMENT password issuer, not an identity provider."
  warn "acceptable ONLY behind Cloudflare Access -- see step 5 of the runbook."
fi
ok "authentication mode chosen, secrets present"

if [ "$CHECK_ONLY" -eq 1 ]; then
  say "Check only -- nothing changed"
  exit 0
fi

# --------------------------------------------------------------------- user
say "Service account"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  # No login shell and no home directory: this account exists to own a process
  # and a database role, not to be logged into.
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  ok "created system user $SERVICE_USER"
else
  ok "system user $SERVICE_USER exists"
fi

# The account has no home directory, so every tool that touches $HOME -- pip's
# cache, pnpm's store, bun's transpiler cache -- would aim at a path that does
# not exist and is not writable. systemd's StateDirectory= creates this at
# service start; the build steps below run before any service exists, so it is
# created here too and used as HOME throughout.
STATE=/var/lib/sreoi
mkdir -p "$STATE"
chown "$SERVICE_USER:$SERVICE_USER" "$STATE"
ok "state directory $STATE"

# Now that the account exists, prove it can actually run the two runtimes.
for tool in "$BUN" "$PNPM"; do
  as_service / "'$tool' --version" >/dev/null 2>&1 || die \
    "$SERVICE_USER cannot run $tool (-> $(readlink -f "$tool")). If it lives under /root or another home directory, an unprivileged account cannot traverse it; reinstall it under /usr/local. Reproduce with: $(printf "su %s -s /bin/bash -c \"HOME=%s PATH=%s:\\\$PATH %s --version\"" "$SERVICE_USER" "$STATE" "$TOOLPATH" "$tool")"
done
ok "bun $("$BUN" --version) and pnpm $("$PNPM" --version) are runnable by $SERVICE_USER"

# The unit ships an absolute ExecStart. If bun ended up somewhere else, say so
# now rather than letting `systemctl start` fail after everything else worked.
if [ "$(readlink -f "$BUN")" != "$(readlink -f /usr/local/bin/bun 2>/dev/null)" ] && [ ! -x /usr/local/bin/bun ]; then
  warn "bun is at $BUN but sreoi-api.service expects /usr/local/bin/bun"
  ln -sfn "$BUN" /usr/local/bin/bun
  ok "linked /usr/local/bin/bun -> $BUN"
fi

# --------------------------------------------------------------------- code
say "Code at $TARGET"
mkdir -p "$TARGET"
# --delete so a removed file in the repo is a removed file on the server.
# Excludes are what makes this safe to run against a live deployment: the venv
# and node_modules are built in place and rebuilding them on every deploy would
# turn a one-second sync into a five-minute one.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
    --exclude 'dist' --exclude '__pycache__' --exclude '.env' \
    "$REPO"/ "$TARGET"/
else
  # tar, not `cp -a`. The excludes above are not optional detail: cp copied
  # .git and node_modules too and turned a 90MB sync into 1.3GB, which on a
  # small server is the difference between deploying and filling the disk.
  # Stale files are still not removed on this path -- tar cannot do --delete --
  # which is why rsync is preferred.
  warn "rsync absent; using tar (honours excludes, but stale files are not removed)"
  tar -C "$REPO" \
    --exclude=.git --exclude=node_modules --exclude=.venv \
    --exclude=dist --exclude=__pycache__ --exclude=.env \
    -cf - . | tar -C "$TARGET" -xf -
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$TARGET"
ok "synced"

# ----------------------------------------------------------------- database
say "Database"
PG="su - postgres -c"
pg() { su - postgres -c "psql -qtAX -c \"$1\"" ; }
pgdb() { su - postgres -c "psql -qtAX -d \"$1\" -c \"$2\"" ; }

# NOSUPERUSER NOBYPASSRLS is load-bearing, not hygiene. PostgreSQL exempts
# superusers from row-level security unconditionally and even FORCE does not
# apply to them, so a superuser application role would leave all sixteen
# tenant policies in place and enforced against nobody. Every organisation
# would see every other organisation's opportunities, with no error anywhere.
if [ "$(pg "SELECT 1 FROM pg_roles WHERE rolname='$DB_ROLE'")" != "1" ]; then
  pg "CREATE ROLE $DB_ROLE LOGIN PASSWORD '$SREOI_DB_PASSWORD' NOSUPERUSER NOBYPASSRLS" >/dev/null
  ok "created role $DB_ROLE"
else
  # Password may have been rotated in .env; apply it either way.
  pg "ALTER ROLE $DB_ROLE LOGIN PASSWORD '$SREOI_DB_PASSWORD'" >/dev/null
  ok "role $DB_ROLE exists (password applied)"
fi
pg "ALTER ROLE $DB_ROLE NOSUPERUSER NOBYPASSRLS" >/dev/null

if [ "$(pg "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'")" != "1" ]; then
  pg "CREATE DATABASE \\\"$DB_NAME\\\" OWNER $DB_ROLE" >/dev/null
  ok "created database $DB_NAME"
else
  ok "database $DB_NAME exists"
fi

# Extensions need a superuser, which is exactly why the application role is not
# one. Installed by postgres, then the schema is handed to the app role.
pgdb "$DB_NAME" "CREATE EXTENSION IF NOT EXISTS postgis" >/dev/null \
  || die "PostGIS is not available to this cluster. Install postgresql-16-postgis-3."
pgdb "$DB_NAME" "CREATE EXTENSION IF NOT EXISTS pg_trgm" >/dev/null
pgdb "$DB_NAME" "ALTER SCHEMA public OWNER TO $DB_ROLE" >/dev/null
pgdb "$DB_NAME" "GRANT ALL ON SCHEMA public TO $DB_ROLE" >/dev/null
ok "postgis + pg_trgm, schema owned by $DB_ROLE"

# ---------------------------------------------------------------- env files
say "Environment files at $ENVDIR"
mkdir -p "$ENVDIR"
# 0640 root:sreoi. A systemd unit is world-readable (`systemctl cat` prints it
# for anyone), so secrets go in a file the service can read and other users
# cannot -- not in the unit.
umask 027

DB_URL_PY="postgresql+psycopg://$DB_ROLE:$SREOI_DB_PASSWORD@127.0.0.1:5432/$DB_NAME"
DB_URL_TS="postgresql://$DB_ROLE:$SREOI_DB_PASSWORD@127.0.0.1:5432/$DB_NAME"

cat > "$ENVDIR/engine.env" <<ENGINE
# Written by deploy/origin/install.sh. Edit .env and re-run instead.
SREOI_DATABASE_URL=$DB_URL_PY
SREOI_AUTH_DEV_MODE=${SREOI_AUTH_DEV_MODE:-}
SREOI_DEV_TOKEN_SECRET=${SREOI_DEV_TOKEN_SECRET:-}
SREOI_OIDC_ISSUER=${SREOI_OIDC_ISSUER:-}
SREOI_OIDC_AUDIENCE=${SREOI_OIDC_AUDIENCE:-}
SREOI_OIDC_JWKS_URL=${SREOI_OIDC_JWKS_URL:-}
LOG_LEVEL=${LOG_LEVEL:-2}
ENGINE

cat > "$ENVDIR/api.env" <<API
# Written by deploy/origin/install.sh. Edit .env and re-run instead.
SREOI_DATABASE_URL=$DB_URL_TS
SREOI_ENGINE_URL=http://127.0.0.1:8000
SREOI_API_HOST=127.0.0.1
SREOI_API_PORT=3000
SREOI_CORS_ORIGIN=$PUBLIC_ORIGIN
# One proxy hop: cloudflared. At 0 the sign-in throttle would bucket every
# client under cloudflared's own address, so one user's failures would lock out
# everyone; trusting the whole chain instead would let an internet-supplied
# X-Forwarded-For impersonate an address and evict someone else's lockout.
SREOI_TRUST_PROXY=1
API

chown root:"$SERVICE_USER" "$ENVDIR"/engine.env "$ENVDIR"/api.env
chmod 640 "$ENVDIR"/engine.env "$ENVDIR"/api.env
umask 022
ok "engine.env, api.env (0640 root:$SERVICE_USER)"

# ------------------------------------------------------------------- python
say "Python engine"
if [ ! -x "$TARGET/platform/.venv/bin/python" ]; then
  as_service "$TARGET/platform" "python3 -m venv .venv"
  as_service "$TARGET/platform" ".venv/bin/pip install --quiet --upgrade pip"
  ok "created venv"
fi
# Not `-e .[dev]`: a server has no business carrying the test and lint
# toolchain, and `pip install .` would drop the editable link the CLI is run
# through. `-e .` keeps `python -m sreoi_pipeline.cli` working from $TARGET.
as_service "$TARGET/platform" ".venv/bin/pip install --quiet -e ."
ok "dependencies installed"

as_service "$TARGET/platform" \
  "SREOI_DATABASE_URL='$DB_URL_PY' .venv/bin/alembic upgrade head" >/dev/null
ok "schema at head"

# --------------------------------------------------------------------- node
say "Node workspace"
# --ignore-scripts, for two reasons that happen to point the same way.
#
# Correctness: the workspace root's `prepare` script runs
# `git config --local core.hooksPath .githooks`, and the deployed copy
# deliberately excludes .git -- so it exits 128 and aborts the whole install.
# The alternative was editing the shared root package.json, which would change
# behaviour for every contributor to fix a deployment concern.
#
# Security: install scripts are arbitrary code from every transitive
# dependency, executing on a server that holds the database credentials. The
# API's own dependencies (@nestjs/*, pg, jose, rxjs, zod, reflect-metadata)
# need none of them; the scripts that do exist here belong to test tooling in
# other workspace packages that this deployment never runs.
#
# --prod would be wrong, though: catalog resolution needs the full lockfile
# graph, and a partial install fails on a workspace protocol reference.
as_service "$TARGET" "'$PNPM' install --frozen-lockfile --ignore-scripts --filter @sreoi/api" \
  || die "pnpm install failed"
ok "API dependencies installed"

# Prove the API can actually be loaded before systemd is asked to keep it
# running. `bun --version` says nothing about whether 737 packages resolved.
as_service "$TARGET/apps/sreoi-api" "'$BUN' -b --eval \"import('./src/app.module.ts').then(()=>process.exit(0),e=>{console.error(String(e));process.exit(1)})\"" \
  || die "the API's module graph does not load. Re-run pnpm install, or check journalctl after start."
ok "API module graph loads"

# ----------------------------------------------------------------- identity
say "Identity"
BOOT=(--email "${ADMIN_EMAIL:-admin@localhost}" --role ORG_ADMIN)
[ -n "${ADMIN_PASSWORD:-}" ] && BOOT+=(--password "$ADMIN_PASSWORD")
as_service "$TARGET/platform" \
  "SREOI_DATABASE_URL='$DB_URL_PY' .venv/bin/python -m sreoi_pipeline.identity ${BOOT[*]}"

if [ "$SEED" -eq 1 ]; then
  say "Synthetic demonstration corpus (--seed)"
  warn "this data is GENERATED, not registered sales. Every page says so."
  as_service "$TARGET/platform" \
    "SREOI_DATABASE_URL='$DB_URL_PY' .venv/bin/python -m sreoi_pipeline.cli reset-and-demo"
fi

# ------------------------------------------------------------------ systemd
say "Services"
install -m 644 systemd/sreoi-engine.service systemd/sreoi-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet sreoi-engine.service sreoi-api.service
systemctl restart sreoi-engine.service
systemctl restart sreoi-api.service
ok "sreoi-engine, sreoi-api enabled and started"

health() { # health <url> <label>
  for _ in $(seq 1 45); do
    curl -sf -o /dev/null "$1" && { ok "$2"; return 0; }
    sleep 1
  done
  warn "$2 did not come up. journalctl -u ${3} -n 50 --no-pager"
  return 1
}
FAILED=0
health http://127.0.0.1:8000/health "engine  127.0.0.1:8000" sreoi-engine || FAILED=1
health http://127.0.0.1:3000/health "api     127.0.0.1:3000" sreoi-api || FAILED=1

# --------------------------------------------------------------- cloudflared
say "Cloudflare tunnel"
if [ ! -x /usr/local/bin/cloudflared ] && ! command -v cloudflared >/dev/null 2>&1; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64) CF_ARCH=amd64 ;;
    aarch64|arm64) CF_ARCH=arm64 ;;
    *) die "no cloudflared build for $ARCH; install it manually" ;;
  esac
  curl -fsSL -o /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH"
  chmod 755 /usr/local/bin/cloudflared
  ok "installed cloudflared $(/usr/local/bin/cloudflared --version 2>/dev/null | head -1)"
else
  ok "cloudflared present"
fi

if systemctl list-unit-files cloudflared.service >/dev/null 2>&1 \
   && systemctl is-enabled --quiet cloudflared.service 2>/dev/null; then
  ok "cloudflared.service already installed; restarting"
  systemctl restart cloudflared
else
  # `service install <token>` writes the unit and enables it. The token names
  # the tunnel and carries its secret, so no config file or credentials file is
  # needed on this host.
  /usr/local/bin/cloudflared service install "$CLOUDFLARE_TUNNEL_TOKEN"
  systemctl enable --quiet cloudflared 2>/dev/null || true
  systemctl start cloudflared
  ok "cloudflared.service installed and started"
fi

# ------------------------------------------------------------------- report
say "Listening sockets"
# Printed so the claim "nothing is public" is checkable rather than asserted.
if command -v ss >/dev/null 2>&1; then
  ss -ltnp 2>/dev/null | awk 'NR==1 || /:(8000|3000|5432)\b/' || true
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE '^(0\.0\.0\.0|\[?::\]?):(8000|3000|5432)$'; then
    warn "a service is bound to a PUBLIC interface. It should be 127.0.0.1 only."
  else
    ok "engine, api and postgres are on loopback only"
  fi
fi

cat <<BANNER

  ────────────────────────────────────────────────────────────────
   Origin is up. Nothing here is reachable from the internet
   except through the tunnel.

   Next, in the Cloudflare dashboard:
     1. Route the tunnel's public hostname to  http://127.0.0.1:3000
     2. Put an Access service-token policy on that hostname
     3. Deploy the client to Pages and set ORIGIN_URL to it

   Full runbook:  deploy/cloudflare/README.md

   Logs:   journalctl -u sreoi-engine -u sreoi-api -f
   Redeploy after a git pull:  sudo ./install.sh
  ────────────────────────────────────────────────────────────────
BANNER

exit "$FAILED"
