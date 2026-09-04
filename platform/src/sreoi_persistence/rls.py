"""Row-level security policies, defined once.

These policies were originally written inline in a migration, which meant the
test suite -- which builds its schema with `create_all` rather than by running
migrations -- never exercised them. A security control that no test touches is
a control you cannot claim to have. Defining them here lets the migration and
the test fixtures apply the same thing.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from sreoi_persistence.models_identity import TENANT_TABLES

TENANT_SETTING = "app.organization_id"


def enable_row_level_security(connection: Connection) -> None:
    """Isolate tenant tables by `app.organization_id`.

    Two permissive policies per table:

    * tenant isolation -- rows must match the bound organisation, for reads and
      writes alike, so a tenant can neither see nor plant another's data;
    * platform access -- when no organisation is bound, full access. Migrations,
      seeding and platform administration run outside any tenant context and
      must still work.

    FORCE matters: without it the table owner bypasses its own policy, and since
    the application connects as the owner the isolation would be decorative.
    Note that a PostgreSQL superuser bypasses RLS regardless, so the application
    role must not be one.
    """
    for table in TENANT_TABLES:
        connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        connection.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        connection.execute(text(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}"))
        connection.execute(text(f"DROP POLICY IF EXISTS {table}_platform_access ON {table}"))
        connection.execute(
            text(
                f"""
                CREATE POLICY {table}_tenant_isolation ON {table}
                USING (organization_id::text
                       = current_setting('{TENANT_SETTING}', true))
                WITH CHECK (organization_id::text
                            = current_setting('{TENANT_SETTING}', true))
                """
            )
        )
        connection.execute(
            text(
                f"""
                CREATE POLICY {table}_platform_access ON {table}
                USING (coalesce(current_setting('{TENANT_SETTING}', true), '') = '')
                WITH CHECK (coalesce(current_setting('{TENANT_SETTING}', true), '') = '')
                """
            )
        )


def disable_row_level_security(connection: Connection) -> None:
    for table in TENANT_TABLES:
        connection.execute(text(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}"))
        connection.execute(text(f"DROP POLICY IF EXISTS {table}_platform_access ON {table}"))
        connection.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        connection.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
