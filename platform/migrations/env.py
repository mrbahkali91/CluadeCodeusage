"""Alembic environment."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from sreoi_persistence.db import database_url
from sreoi_persistence.models import Base

# PostGIS creates and owns these; migrations must neither manage nor drop them.
POSTGIS_OWNED = {"spatial_ref_sys", "geometry_columns", "geography_columns"}


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    if type_ == "table" and name in POSTGIS_OWNED:
        return False
    return True


config = context.config
config.set_main_option("sqlalchemy.url", database_url())
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
