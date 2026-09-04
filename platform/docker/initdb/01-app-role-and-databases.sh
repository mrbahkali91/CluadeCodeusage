#!/bin/bash
# Runs once, as the container superuser, when the data volume is first created.
#
# Two things happen here that cannot happen later:
#
#   1. The application role is created NOSUPERUSER NOBYPASSRLS. PostgreSQL
#      exempts superusers from row-level security unconditionally, and FORCE
#      ROW LEVEL SECURITY does not apply to them either -- so an app role with
#      superuser would leave every tenant-isolation policy in place and
#      enforced against nobody. This is not a hardening nicety; it is the
#      difference between the isolation working and only appearing to.
#
#   2. PostGIS is installed. CREATE EXTENSION postgis requires superuser (it is
#      not a "trusted" extension), so the app role can never install it itself.
#      That applies to the test database too, which is why it is created here
#      rather than by the test suite: without it, every database-backed test --
#      including all the tenant-isolation tests -- would silently *skip*.
set -euo pipefail

APP_ROLE="${SREOI_DB_ROLE:-sreoi}"
APP_PASSWORD="${SREOI_DB_PASSWORD:-sreoi}"
APP_DB="${SREOI_DB_NAME:-sreoi}"
TEST_DB="${APP_DB}_test"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${APP_ROLE}') THEN
    CREATE ROLE ${APP_ROLE} LOGIN PASSWORD '${APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS;
  END IF;
END
\$\$;
SQL

for db in "$APP_DB" "$TEST_DB"; do
  if ! psql -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" \
      --username "$POSTGRES_USER" --dbname postgres | grep -q 1; then
    createdb --username "$POSTGRES_USER" --owner "$APP_ROLE" "$db"
  fi
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<SQL
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
ALTER SCHEMA public OWNER TO ${APP_ROLE};
GRANT ALL ON SCHEMA public TO ${APP_ROLE};
SQL
done

echo "initdb: role ${APP_ROLE} (NOSUPERUSER), databases ${APP_DB} and ${TEST_DB} ready"
