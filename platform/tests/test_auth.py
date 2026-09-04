"""Authentication, authorisation and tenant isolation.

The properties asserted here are the ones that matter if this is ever exposed:
unauthenticated requests are refused, an unconfigured deployment serves
nothing, a token cannot grant a role the database did not, and one tenant
cannot read another's data even when the application forgets to filter.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sreoi_api.auth import (
    AuthConfigurationError,
    AuthSettings,
    Principal,
)
from sreoi_api.middleware import is_public
from sreoi_persistence.credentials import (
    generate_api_key,
    hash_password,
    verify_password,
)
from sreoi_persistence.models_identity import (
    ApiKey,
    Organization,
    Role,
    User,
)
from sreoi_pipeline.identity import bootstrap, ensure_membership, ensure_user
from tests.conftest import requires_db

DEV_SECRET = "test-dev-secret-not-a-real-key-long-enough-for-hs256"


# --------------------------------------------------------------- unit level


def test_unconfigured_deployment_is_not_configured() -> None:
    """Fail closed: no mechanism configured means nothing is served."""
    assert not AuthSettings(None, None, None, False, None).configured


def test_dev_mode_alongside_oidc_is_refused() -> None:
    """A development password issuer must not be reachable next to a real IdP."""
    with pytest.raises(AuthConfigurationError, match="Refusing to start"):
        AuthSettings("https://issuer", "aud", "https://jwks", True, "s").validate()


def test_dev_mode_requires_a_secret() -> None:
    with pytest.raises(AuthConfigurationError, match="SREOI_DEV_TOKEN_SECRET"):
        AuthSettings(None, None, None, True, None).validate()


def test_a_short_dev_secret_is_refused() -> None:
    """A weak HMAC key is silent: tokens still verify, they are just cheaper to
    forge. So it has to be rejected at startup rather than at use."""
    with pytest.raises(AuthConfigurationError, match="at least 32 bytes"):
        AuthSettings(None, None, None, True, "too-short").validate()
    AuthSettings(None, None, None, True, "x" * 32).validate()


def test_password_hash_is_not_reversible_and_verifies() -> None:
    hashed = hash_password("correct horse battery staple")
    assert "correct horse" not in hashed
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong", hashed)


def test_api_key_secret_is_never_stored() -> None:
    secret, prefix, stored = generate_api_key()
    assert secret.startswith(prefix)
    assert secret not in stored
    assert verify_password(secret, stored)
    assert not verify_password(secret + "x", stored)


def test_role_ordering() -> None:
    assert Role.ORG_ADMIN.at_least(Role.ANALYST)
    assert not Role.VIEWER.at_least(Role.ANALYST)
    assert Role.PLATFORM_ADMIN.at_least(Role.ORG_ADMIN)


def test_principal_require_raises_for_insufficient_role() -> None:
    principal = Principal(subject="s", organization_id=uuid.uuid4(), role=Role.VIEWER)
    principal.require(Role.VIEWER)
    with pytest.raises(Exception, match="insufficient"):
        principal.require(Role.ADMIN)


def test_public_surface_is_minimal() -> None:
    """Anything not explicitly public must be protected."""
    for path in (
        "/health",
        "/docs",
        "/openapi.json",
        "/static/x.css",
        "/auth/config",
        "/auth/login",
        "/auth/logout",
    ):
        assert is_public(path), path
    for path in (
        "/",
        "/map",
        "/watchlists",
        "/admin/quality",
        "/admin/sources",
        "/api/v1/opportunities",
        "/api/v1/admin/health",
        "/auth/me",
        "/auth/api-keys",
    ):
        assert not is_public(path), path


# ----------------------------------------------------- integration level


@pytest.fixture
def dev_auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SREOI_AUTH_DEV_MODE", "1")
    monkeypatch.setenv("SREOI_DEV_TOKEN_SECRET", DEV_SECRET)
    for key in ("SREOI_OIDC_ISSUER", "SREOI_OIDC_AUDIENCE", "SREOI_OIDC_JWKS_URL"):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def tenants(seeded_db: None, dev_auth: None) -> dict[str, Any]:
    """Two organisations with a user each, so isolation can be tested."""
    left = bootstrap(
        slug="acme",
        name="Acme Property",
        email="acme@example.com",
        password="acme-pass",
        role=Role.ORG_ADMIN,
    )
    right = bootstrap(
        slug="rival",
        name="Rival Capital",
        email="rival@example.com",
        password="rival-pass",
        role=Role.ORG_ADMIN,
    )
    return {"left": left, "right": right}


@pytest.fixture
def client(tenants: dict[str, Any]) -> Iterator[TestClient]:
    """No module reload needed: the middleware reads settings per request."""
    from sreoi_api.main import app

    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, email: str, password: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


@requires_db
def test_protected_routes_refuse_anonymous_requests(client: TestClient) -> None:
    """The headline property."""
    for path in (
        "/",
        "/api/v1/search/opportunities",
        "/admin/quality",
        "/api/v1/admin/health",
        "/map",
    ):
        response = client.get(path)
        assert response.status_code == 401, f"{path} returned {response.status_code}"


@requires_db
def test_public_routes_remain_reachable(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/auth/config").status_code == 200


@requires_db
def test_login_then_access(client: TestClient) -> None:
    token = _login(client, "acme@example.com", "acme-pass")
    response = client.get(
        "/api/v1/search/opportunities?limit=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


@requires_db
def test_wrong_password_is_refused_without_confirming_the_address(
    client: TestClient,
) -> None:
    unknown = client.post("/auth/login", json={"email": "nobody@example.com", "password": "x"})
    wrong = client.post("/auth/login", json={"email": "acme@example.com", "password": "wrong"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json(), "responses must not distinguish the two"


@requires_db
def test_garbage_and_tampered_tokens_are_refused(client: TestClient) -> None:
    token = _login(client, "acme@example.com", "acme-pass")
    for bad in ("garbage", token[:-4] + "AAAA", ""):
        response = client.get(
            "/api/v1/search/opportunities",
            headers={"Authorization": f"Bearer {bad}"},
        )
        assert response.status_code == 401


@requires_db
def test_api_key_grants_access_and_cannot_exceed_the_minters_role(
    client: TestClient,
) -> None:
    token = _login(client, "acme@example.com", "acme-pass")
    auth = {"Authorization": f"Bearer {token}"}

    created = client.post("/auth/api-keys", json={"name": "ci", "role": "ANALYST"}, headers=auth)
    assert created.status_code == 201, created.text
    secret = created.json()["secret"]

    # The key works.
    assert (
        client.get(
            "/api/v1/search/opportunities?limit=1", headers={"x-api-key": secret}
        ).status_code
        == 200
    )

    # And it cannot be minted above the caller's own role.
    escalation = client.post(
        "/auth/api-keys",
        json={"name": "escalate", "role": "PLATFORM_ADMIN"},
        headers=auth,
    )
    assert escalation.status_code == 403


@requires_db
def test_api_key_secret_is_shown_once_and_not_persisted(
    client: TestClient, session: Session
) -> None:
    token = _login(client, "acme@example.com", "acme-pass")
    created = client.post(
        "/auth/api-keys",
        json={"name": "once", "role": "VIEWER"},
        headers={"Authorization": f"Bearer {token}"},
    )
    secret = created.json()["secret"]
    prefix = created.json()["prefix"]
    record = session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    assert record is not None
    assert secret not in record.key_hash


@requires_db
def test_a_token_cannot_claim_a_role_the_database_did_not_grant(
    client: TestClient, session: Session, tenants: dict[str, Any]
) -> None:
    """The database is authoritative; a forged role claim must not be honoured."""
    from sreoi_api.auth import issue_dev_token, load_settings

    user, _ = ensure_user(session, email="viewer@example.com", password="viewer-pass")
    org = session.scalar(select(Organization).where(Organization.slug == "acme"))
    assert org is not None
    ensure_membership(session, user=user, organization=org, role=Role.VIEWER)
    session.commit()

    forged = issue_dev_token(
        load_settings(),
        subject=user.subject,
        organization_id=org.id,
        role=Role.PLATFORM_ADMIN,
    )
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert me.status_code == 200
    assert me.json()["role"] == Role.VIEWER.value, "stored role must win"


@requires_db
def test_row_level_security_blocks_cross_tenant_reads(
    tenants: dict[str, Any], session: Session
) -> None:
    """The backstop: isolation holds even with no application filter at all.

    This query deliberately omits any organization_id predicate. If RLS were
    absent or unforced it would return the other tenant's row.
    """
    left = session.scalar(select(Organization).where(Organization.slug == "acme"))
    right = session.scalar(select(Organization).where(Organization.slug == "rival"))
    assert left is not None and right is not None

    session.execute(
        text(
            "INSERT INTO watchlists "
            "(id, organization_id, owner_ref, name, enabled, created_at) "
            "VALUES (gen_random_uuid(), :org, 'owner-left', 'Left list', true, now())"
        ),
        {"org": str(left.id)},
    )
    session.execute(
        text(
            "INSERT INTO watchlists "
            "(id, organization_id, owner_ref, name, enabled, created_at) "
            "VALUES (gen_random_uuid(), :org, 'owner-right', 'Right list', true, now())"
        ),
        {"org": str(right.id)},
    )
    session.commit()

    from sreoi_persistence.db import bind_tenant

    bind_tenant(session, left.id)
    visible = session.execute(text("SELECT name FROM watchlists")).scalars().all()
    assert "Left list" in visible
    assert "Right list" not in visible, "row-level security did not isolate the tenant"

    bind_tenant(session, right.id)
    visible = session.execute(text("SELECT name FROM watchlists")).scalars().all()
    assert "Right list" in visible
    assert "Left list" not in visible

    bind_tenant(session, None)
    session.rollback()


@requires_db
def test_writing_into_another_tenant_is_rejected(tenants: dict[str, Any], session: Session) -> None:
    """WITH CHECK: a tenant cannot plant a row under someone else's id."""
    from sqlalchemy.exc import DatabaseError

    from sreoi_persistence.db import bind_tenant

    left = session.scalar(select(Organization).where(Organization.slug == "acme"))
    right = session.scalar(select(Organization).where(Organization.slug == "rival"))
    assert left is not None and right is not None

    bind_tenant(session, left.id)
    with pytest.raises(DatabaseError):
        session.execute(
            text(
                "INSERT INTO watchlists "
                "(id, organization_id, owner_ref, name, enabled, created_at) "
                "VALUES (gen_random_uuid(), :org, 'x', 'Planted', true, now())"
            ),
            {"org": str(right.id)},
        )
    session.rollback()
    bind_tenant(session, None)


@requires_db
def test_bootstrap_is_idempotent_and_does_not_reset_a_password(
    session: Session, tenants: dict[str, Any]
) -> None:
    again = bootstrap(
        slug="acme",
        name="Acme Property",
        email="acme@example.com",
        password="different-password",
        role=Role.ORG_ADMIN,
    )
    assert again["user_created"] == "False"
    assert "unchanged" in again["password"]
    user = session.scalar(select(User).where(User.email == "acme@example.com"))
    assert user is not None and user.password_hash is not None
    assert verify_password("acme-pass", user.password_hash), "original password kept"


@requires_db
def test_login_is_refused_when_dev_mode_is_off(
    seeded_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment without dev mode must not accept password login."""
    monkeypatch.setenv("SREOI_AUTH_DEV_MODE", "1")
    monkeypatch.setenv("SREOI_DEV_TOKEN_SECRET", DEV_SECRET)
    bootstrap(
        slug="acme2",
        name="Acme Two",
        email="acme2@example.com",
        password="pw",
        role=Role.ORG_ADMIN,
    )
    import sreoi_api.main as main_module

    with TestClient(main_module.app) as authed:
        assert (
            authed.post(
                "/auth/login", json={"email": "acme2@example.com", "password": "pw"}
            ).status_code
            == 200
        )

    # Settings are read per request, so simply removing the variable is enough --
    # no module reload, and therefore no state leaking into later tests.
    monkeypatch.delenv("SREOI_AUTH_DEV_MODE")
    with TestClient(main_module.app) as closed:
        response = closed.post("/auth/login", json={"email": "acme2@example.com", "password": "pw"})
        assert response.status_code in (403, 503)


@requires_db
def test_security_headers_are_present(client: TestClient) -> None:
    response = client.get("/health")
    for header in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Content-Security-Policy",
        "Referrer-Policy",
    ):
        assert header in response.headers, header
    assert "cdn" not in response.headers["Content-Security-Policy"].lower()


@requires_db
def test_login_is_audited(client: TestClient, session: Session) -> None:
    from sreoi_persistence.models_identity import AuditEvent

    client.post("/auth/login", json={"email": "acme@example.com", "password": "nope"})
    _login(client, "acme@example.com", "acme-pass")
    session.commit()
    events = session.scalars(select(AuditEvent).where(AuditEvent.action == "login")).all()
    outcomes = {e.outcome for e in events}
    assert "FAILED" in outcomes and "SUCCEEDED" in outcomes
    # No credential may ever appear in an audit detail.
    assert all("acme-pass" not in (e.detail or "") for e in events)


def test_environment_does_not_leak_dev_secret_by_default() -> None:
    """Dev mode must be opt-in, never the ambient default."""
    assert os.environ.get("SREOI_AUTH_DEV_MODE") != "1" or os.environ.get(
        "SREOI_DEV_TOKEN_SECRET"
    ), "dev mode on without a secret"


# --------------------------------------------------------------------------
# The browser path. Enforcement without a way in is a locked door with no key.


def test_safe_next_collapses_anything_that_is_not_a_local_path() -> None:
    """An unvalidated `next` on a sign-in page is a credentialled open
    redirect: the victim arrives at the attacker's host already authenticated."""
    from sreoi_api.routers.auth_routes import safe_next

    assert safe_next("/map?lang=ar") == "/map?lang=ar"
    for hostile in (
        None,
        "",
        "https://evil.example/steal",
        "//evil.example/steal",
        "/\\evil.example",
        "javascript:alert(1)",
        "/auth/signin?next=/auth/signin",
    ):
        assert safe_next(hostile) == "/", hostile


@requires_db
def test_the_signin_page_is_reachable_without_a_credential(client: TestClient) -> None:
    response = client.get("/auth/signin")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # The form exists because this deployment has dev mode on.
    assert 'id="signin"' in response.text


@requires_db
def test_the_signin_page_hides_the_password_form_when_oidc_owns_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SREOI_AUTH_DEV_MODE")
    monkeypatch.setenv("SREOI_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.setenv("SREOI_OIDC_AUDIENCE", "sreoi")
    monkeypatch.setenv("SREOI_OIDC_JWKS_URL", "https://issuer.example/jwks")
    response = client.get("/auth/signin")
    assert response.status_code == 200
    assert 'id="signin"' not in response.text
    assert "issuer.example" in response.text


@requires_db
def test_a_browser_is_redirected_to_signin_and_an_api_client_is_not(
    client: TestClient,
) -> None:
    browser = client.get("/map?lang=ar", headers={"Accept": "text/html"}, follow_redirects=False)
    assert browser.status_code == 303
    location = browser.headers["location"]
    assert location.startswith("/auth/signin?next=")
    assert "%2Fmap" in location  # the destination is preserved, encoded

    api = client.get("/api/v1/search/opportunities", headers={"Accept": "application/json"})
    assert api.status_code == 401
    assert api.headers.get("WWW-Authenticate") == "Bearer"


@requires_db
def test_a_write_is_never_bounced_to_a_page(client: TestClient) -> None:
    """Redirecting a POST would drop the body and read as success to a caller
    that only checks `response.ok`."""
    response = client.post(
        "/api/v1/watchlists",
        json={"name": "x"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 401


@requires_db
def test_signout_clears_the_cookie_and_returns_to_signin(client: TestClient) -> None:
    _login(client, "acme@example.com", "acme-pass")
    assert client.get("/api/v1/search/opportunities").status_code == 200
    response = client.get("/auth/signout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/signin")
    assert client.get("/api/v1/search/opportunities").status_code == 401


@requires_db
def test_the_signed_in_page_names_the_principal(client: TestClient) -> None:
    _login(client, "acme@example.com", "acme-pass")
    page = client.get("/", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "acme@example.com" in page.text
    assert "/auth/signout" in page.text
