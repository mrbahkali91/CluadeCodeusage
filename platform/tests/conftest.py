"""Integration test fixtures. Skips cleanly when no database is available."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

DEV_DB_URL = os.environ.get(
    "SREOI_DATABASE_URL", "postgresql+psycopg://sreoi:sreoi@127.0.0.1:5432/sreoi"
)


def _derive_test_url(url: str) -> str:
    """Never run destructive fixtures against the development database.

    The session fixture drops and recreates the schema. Defaulting to the same
    database as `make run` would silently destroy a developer's local data
    every time they ran the tests, so the suite uses a sibling `_test`
    database unless one is named explicitly.
    """
    base, _, name = url.rpartition("/")
    if not name or name.endswith("_test"):
        return url
    return f"{base}/{name}_test"


TEST_DB_URL = os.environ.get("SREOI_TEST_DATABASE_URL") or _derive_test_url(DEV_DB_URL)


def _ensure_test_database() -> bool:
    """Create the dedicated test database on first run, then confirm it works."""
    from sqlalchemy import create_engine

    try:
        engine = create_engine(TEST_DB_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        pass

    # Try to create it from the maintenance database.
    try:
        base, _, name = TEST_DB_URL.rpartition("/")
        admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        admin.dispose()
        engine = create_engine(TEST_DB_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


def _database_available() -> bool:
    return _ensure_test_database()


requires_db = pytest.mark.skipif(
    not _database_available(), reason="PostgreSQL with PostGIS is not available"
)


@pytest.fixture(scope="session")
def seeded_db() -> Iterator[None]:
    os.environ["SREOI_DATABASE_URL"] = TEST_DB_URL
    from sreoi_persistence.db import ensure_extensions, get_engine, reset_engine, session_scope
    from sreoi_persistence.models import Base
    from sreoi_pipeline.seed import seed_all

    reset_engine()
    ensure_extensions()
    Base.metadata.drop_all(get_engine())
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        # live_index=False keeps the test suite offline and deterministic.
        seed_all(session, live_index=False)
    yield
    reset_engine()


@pytest.fixture
def session(seeded_db: None) -> Iterator[Session]:
    from sreoi_persistence.db import get_session_factory

    db = get_session_factory()()
    try:
        yield db
        db.rollback()
    finally:
        db.close()
