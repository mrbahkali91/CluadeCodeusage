"""Integration test fixtures. Skips cleanly when no database is available."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

TEST_DB_URL = os.environ.get(
    "SREOI_TEST_DATABASE_URL",
    os.environ.get("SREOI_DATABASE_URL", "postgresql+psycopg://sreoi:sreoi@127.0.0.1:5432/sreoi"),
)


def _database_available() -> bool:
    try:
        from sqlalchemy import create_engine

        engine = create_engine(TEST_DB_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


requires_db = pytest.mark.skipif(
    not _database_available(), reason="PostgreSQL with PostGIS is not available"
)


@pytest.fixture(scope="session")
def seeded_db() -> Iterator[None]:
    os.environ["SREOI_DATABASE_URL"] = TEST_DB_URL
    from sreoi_persistence.db import ensure_postgis, get_engine, reset_engine, session_scope
    from sreoi_persistence.models import Base
    from sreoi_pipeline.seed import seed_all

    reset_engine()
    ensure_postgis()
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
