"""Engine and session management."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from sreoi_persistence.model_modules import load_all

DEFAULT_URL = "postgresql+psycopg://sreoi:sreoi@127.0.0.1:5432/sreoi"

# Feature tables live in models_<feature>.py and are discovered rather than
# hand-imported, so that concurrent work never contends over an import list.
_LOADED_MODEL_MODULES = load_all()

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def database_url() -> str:
    return os.environ.get("SREOI_DATABASE_URL", DEFAULT_URL)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(database_url(), pool_pre_ping=True, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def bind_tenant(session: Session, organization_id: object | None) -> None:
    """Set the PostgreSQL session variable that row-level security keys on.

    SET LOCAL scopes it to the surrounding transaction, so a pooled connection
    cannot leak one request's tenant into the next.
    """
    if organization_id is None:
        session.execute(text("SELECT set_config('app.organization_id', '', true)"))
        return
    session.execute(
        text("SELECT set_config('app.organization_id', :org, true)"),
        {"org": str(organization_id)},
    )


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ensure_extensions(engine: Engine | None = None) -> None:
    """PostGIS for geospatial, pg_trgm for entity-resolution text similarity."""
    eng = engine or get_engine()
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))


# Backwards-compatible alias.
ensure_postgis = ensure_extensions


def reset_engine() -> None:
    """Used by tests that switch database URLs."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
