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
    # create_all does not create policies, so without this the test suite would
    # never exercise tenant isolation at all.
    from sreoi_persistence.rls import enable_row_level_security

    with get_engine().begin() as connection:
        enable_row_level_security(connection)
    with session_scope() as session:
        # live_index=False keeps the test suite offline and deterministic.
        seed_all(session, live_index=False)
    yield
    reset_engine()


# Mutable tables, in dependency order. Reference data (districts, sources,
# transactions, price index) is deliberately preserved.
_MUTABLE_TABLES = (
    "notifications",
    "alerts",
    "watch_rules",
    "watchlists",
    "investment_memos",
    "document_extractions",
    "documents",
    "backtest_results",
    "backtest_runs",
    "quality_snapshots",
    "rental_estimates",
    "property_timeline",
    "listing_snapshots",
    "listings",
    "verification_checks",
    "score_components",
    "opportunity_scores",
    "cost_line_items",
    "true_acquisition_costs",
    "valuation_comparables",
    "valuations",
    "agent_decisions",
    "llm_calls",
    "agent_runs",
    "property_merges",
    "opportunities",
    "properties",
)


@pytest.fixture
def isolated(seeded_db: None) -> Iterator[None]:
    """Give a test an empty property graph.

    API tests commit through their own sessions, so without this a test that
    ingests a property can silently match one another module committed --
    entity resolution then merges them and the assertions become order
    dependent. Reference data is kept so seeding does not have to repeat.
    """
    from sreoi_persistence.db import session_scope

    with session_scope() as db:
        db.execute(text(f"TRUNCATE {', '.join(_MUTABLE_TABLES)} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def session(seeded_db: None) -> Iterator[Session]:
    from sreoi_persistence.db import get_session_factory

    db = get_session_factory()()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Authentication for API tests.
#
# Auth is enforced by middleware on every non-public route, so a TestClient
# with no credentials gets 401 everywhere. These fixtures give the suite a real
# credential rather than a bypass: the tests exercise the same code path a user
# would, which is the point of having enforcement at all.

TEST_DEV_SECRET = "pytest-dev-secret-do-not-use-outside-tests"
TEST_ORG_SLUG = "pytest-org"
TEST_USER_EMAIL = "pytest@example.com"
TEST_USER_PASSWORD = "pytest-password"


@pytest.fixture(scope="session", autouse=True)
def _dev_auth_env() -> Iterator[None]:
    """Enable the local development issuer for the whole test session."""
    previous = {
        key: os.environ.get(key)
        for key in (
            "SREOI_AUTH_DEV_MODE",
            "SREOI_DEV_TOKEN_SECRET",
            "SREOI_OIDC_ISSUER",
            "SREOI_OIDC_AUDIENCE",
            "SREOI_OIDC_JWKS_URL",
        )
    }
    os.environ["SREOI_AUTH_DEV_MODE"] = "1"
    os.environ["SREOI_DEV_TOKEN_SECRET"] = TEST_DEV_SECRET
    for key in ("SREOI_OIDC_ISSUER", "SREOI_OIDC_AUDIENCE", "SREOI_OIDC_JWKS_URL"):
        os.environ.pop(key, None)
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def auth_headers(seeded_db: None) -> dict[str, str]:
    """A bearer token for an ORG_ADMIN of the test organisation."""
    from sreoi_api.auth import issue_dev_token, load_settings
    from sreoi_persistence.models_identity import Role
    from sreoi_pipeline.identity import bootstrap

    bootstrap(
        slug=TEST_ORG_SLUG,
        name="Pytest organisation",
        email=TEST_USER_EMAIL,
        password=TEST_USER_PASSWORD,
        role=Role.ORG_ADMIN,
    )
    from sqlalchemy import select

    from sreoi_persistence.db import session_scope
    from sreoi_persistence.models_identity import Organization, User

    with session_scope() as db:
        user = db.scalar(select(User).where(User.email == TEST_USER_EMAIL))
        org = db.scalar(select(Organization).where(Organization.slug == TEST_ORG_SLUG))
        assert user is not None and org is not None
        token = issue_dev_token(
            load_settings(),
            subject=user.subject,
            organization_id=org.id,
            role=Role.ORG_ADMIN,
        )
    return {"Authorization": f"Bearer {token}"}
