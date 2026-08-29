"""Engine and session management."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_URL = "postgresql+psycopg://sreoi:sreoi@127.0.0.1:5432/sreoi"

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


def ensure_postgis(engine: Engine | None = None) -> None:
    eng = engine or get_engine()
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))


def reset_engine() -> None:
    """Used by tests that switch database URLs."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
