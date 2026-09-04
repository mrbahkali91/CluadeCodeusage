#!/bin/sh
# Bring the schema and identity up to date, then serve.
#
# Every step is idempotent, so a restart is safe and `docker compose up` twice
# does not duplicate anything.
set -eu

echo "==> Waiting for PostgreSQL"
i=0
until python -c "
import sys, sqlalchemy, os
try:
    sqlalchemy.create_engine(os.environ['SREOI_DATABASE_URL']).connect().close()
except Exception as exc:
    print(exc, file=sys.stderr); sys.exit(1)
" 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -gt 60 ] && { echo "PostgreSQL did not become reachable"; exit 1; }
  sleep 2
done

echo "==> Migrations"
alembic upgrade head

echo "==> Reference data and demonstration corpus"
# --offline uses the embedded price-index snapshot; drop it to fetch the live
# KAPSARC index, which needs outbound network access from the container.
python -m sreoi_pipeline.cli seed ${SEED_ARGS:---offline}
python -m sreoi_pipeline.cli corpus

echo "==> Identity"
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  python -m sreoi_pipeline.identity --email "${ADMIN_EMAIL:-admin@localhost}" \
    --password "${ADMIN_PASSWORD}" --role ORG_ADMIN
else
  python -m sreoi_pipeline.identity --email "${ADMIN_EMAIL:-admin@localhost}" --role ORG_ADMIN
fi

cat <<BANNER

  Sign in at http://localhost:${PORT:-8000}/auth/signin as ${ADMIN_EMAIL:-admin@localhost}

  This stack uses the DEVELOPMENT password issuer, not an identity provider.
  It is bound to localhost and must not be exposed further. For anything else,
  set SREOI_OIDC_ISSUER / SREOI_OIDC_AUDIENCE / SREOI_OIDC_JWKS_URL and unset
  SREOI_AUTH_DEV_MODE -- the app refuses to start with both.

BANNER

echo "==> Serving"
exec "$@"
