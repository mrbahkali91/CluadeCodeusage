"""Authentication, authorisation and tenant context.

The governing property is **fail closed**. If no authentication mechanism is
configured, every protected request is denied. An unconfigured deployment
serves nothing rather than serving everything, because the opposite default is
how systems end up publicly readable by accident.

Three credential types, all resolving to the same `Principal`:

* **OIDC bearer token** -- the production path. Signature verified against the
  issuer's JWKS, with issuer and audience checked. We never trust an unverified
  claim, and never accept a token we cannot verify.
* **API key** -- for CLI and service access. Only an Argon2 hash is stored, so
  a database disclosure does not yield usable keys.
* **Local development password** -- available ONLY when
  `SREOI_AUTH_DEV_MODE=1` is set explicitly. It is off by default, and turning
  it on alongside a configured OIDC issuer is refused rather than silently
  preferred, so a dev backdoor cannot survive into a real deployment.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from sreoi_persistence.credentials import (
    verify_password,
)
from sreoi_persistence.models_identity import (
    ApiKey,
    AuditEvent,
    Membership,
    Organization,
    Role,
    User,
)

SESSION_COOKIE = "sreoi_session"
API_KEY_HEADER = "x-api-key"
DEV_TOKEN_TTL = timedelta(hours=12)
JWKS_CACHE_TTL = 300.0


# Set by the auth middleware, read by the session factory so that PostgreSQL
# row-level security can be keyed on it. A context variable rather than a
# parameter because the session dependency has no access to the request.
current_organization: ContextVar[uuid.UUID | None] = ContextVar(
    "current_organization", default=None
)


MIN_DEV_SECRET_BYTES = 32


class AuthConfigurationError(RuntimeError):
    """Configuration is contradictory or unsafe. Raised at startup, not runtime."""


@dataclass(frozen=True, slots=True)
class AuthSettings:
    oidc_issuer: str | None
    oidc_audience: str | None
    oidc_jwks_url: str | None
    dev_mode: bool
    dev_secret: str | None

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_audience and self.oidc_jwks_url)

    @property
    def configured(self) -> bool:
        return self.oidc_enabled or self.dev_mode

    def validate(self) -> None:
        if self.dev_mode and self.oidc_enabled:
            raise AuthConfigurationError(
                "SREOI_AUTH_DEV_MODE=1 is set alongside a configured OIDC issuer. "
                "Refusing to start: a development password issuer must never be "
                "reachable in an environment that has a real identity provider."
            )
        if self.dev_mode and not self.dev_secret:
            raise AuthConfigurationError(
                "SREOI_AUTH_DEV_MODE=1 requires SREOI_DEV_TOKEN_SECRET to be set."
            )
        # RFC 7518 s3.2: an HS256 key shorter than the hash output weakens the
        # signature. Rejected at startup because a short secret is silent --
        # tokens still verify, they are just cheaper to forge.
        if self.dev_secret is not None and len(self.dev_secret.encode()) < MIN_DEV_SECRET_BYTES:
            raise AuthConfigurationError(
                f"SREOI_DEV_TOKEN_SECRET must be at least {MIN_DEV_SECRET_BYTES} bytes "
                f"(got {len(self.dev_secret.encode())})."
            )


def load_settings() -> AuthSettings:
    settings = AuthSettings(
        oidc_issuer=os.environ.get("SREOI_OIDC_ISSUER") or None,
        oidc_audience=os.environ.get("SREOI_OIDC_AUDIENCE") or None,
        oidc_jwks_url=os.environ.get("SREOI_OIDC_JWKS_URL") or None,
        dev_mode=os.environ.get("SREOI_AUTH_DEV_MODE", "").strip() == "1",
        dev_secret=os.environ.get("SREOI_DEV_TOKEN_SECRET") or None,
    )
    settings.validate()
    return settings


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making this request, and on whose behalf."""

    subject: str
    organization_id: uuid.UUID
    role: Role
    user_id: uuid.UUID | None = None
    credential: str = "oidc"
    email: str | None = None

    def require(self, minimum: Role) -> None:
        if not self.role.at_least(minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role {self.role.value} is insufficient; {minimum.value} required",
            )


# --------------------------------------------------------------------------
# Passwords and API keys


def _constant_time_prefix(raw: str) -> str | None:
    prefix, _, _ = raw.partition(".")
    return prefix or None


# --------------------------------------------------------------------------
# OIDC token verification


_jwks_cache: dict[str, tuple[float, Any]] = {}


def _jwks_client(url: str) -> Any:
    cached = _jwks_cache.get(url)
    now = time.monotonic()
    if cached and now - cached[0] < JWKS_CACHE_TTL:
        return cached[1]
    client = jwt.PyJWKClient(url, cache_keys=True)
    _jwks_cache[url] = (now, client)
    return client


def verify_oidc_token(token: str, settings: AuthSettings) -> dict[str, Any]:
    """Verify signature, issuer, audience and expiry. Never decode unverified."""
    if not settings.oidc_enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC is not configured")
    assert settings.oidc_jwks_url is not None
    try:
        signing_key = _jwks_client(settings.oidc_jwks_url).get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "RS512", "ES256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"token rejected: {exc}") from exc
    return claims


def issue_dev_token(
    settings: AuthSettings, *, subject: str, organization_id: uuid.UUID, role: Role
) -> str:
    if not settings.dev_mode or not settings.dev_secret:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "development token issuance is disabled")
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "org": str(organization_id),
            "role": role.value,
            "iat": int(now.timestamp()),
            "exp": int((now + DEV_TOKEN_TTL).timestamp()),
            "iss": "sreoi-dev",
        },
        settings.dev_secret,
        algorithm="HS256",
    )


def verify_dev_token(token: str, settings: AuthSettings) -> dict[str, Any]:
    if not settings.dev_mode or not settings.dev_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "development auth is disabled")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.dev_secret,
            algorithms=["HS256"],
            issuer="sreoi-dev",
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"token rejected: {exc}") from exc
    return claims


# --------------------------------------------------------------------------
# Resolving a credential to a Principal


def principal_from_claims(
    session: Session, claims: dict[str, Any], *, credential: str
) -> Principal:
    subject = str(claims["sub"])
    user = session.scalar(select(User).where(User.subject == subject))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown or inactive subject")

    requested_org = claims.get("org")
    stmt = select(Membership).where(Membership.user_id == user.id)
    if requested_org:
        stmt = stmt.where(Membership.organization_id == uuid.UUID(str(requested_org)))
    membership = session.scalar(stmt)
    if membership is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "no membership in the requested organisation"
        )

    organization = session.get(Organization, membership.organization_id)
    if organization is None or not organization.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organisation is inactive")

    # The token's role claim may not exceed the stored membership role: the
    # database is authoritative, not the token.
    stored = Role(membership.role)
    claimed = claims.get("role")
    role = stored
    if claimed:
        try:
            wanted = Role(str(claimed))
        except ValueError:
            wanted = stored
        role = wanted if stored.at_least(wanted) else stored

    return Principal(
        subject=subject,
        organization_id=organization.id,
        role=role,
        user_id=user.id,
        credential=credential,
        email=user.email,
    )


def principal_from_api_key(session: Session, raw_key: str) -> Principal:
    prefix = _constant_time_prefix(raw_key)
    if not prefix:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed API key")
    record = session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if record is None or not record.is_active or record.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
    if not verify_password(raw_key, record.key_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")

    organization = session.get(Organization, record.organization_id)
    if organization is None or not organization.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organisation is inactive")

    record.last_used_at = datetime.now(UTC)
    return Principal(
        subject=f"apikey:{record.prefix}",
        organization_id=record.organization_id,
        role=Role(record.role),
        user_id=record.created_by_user_id,
        credential="api_key",
    )


def resolve_principal(
    session: Session,
    settings: AuthSettings,
    *,
    authorization: str | None,
    api_key: str | None,
    cookie: str | None,
) -> Principal:
    """Resolve any accepted credential, or refuse."""
    if not settings.configured:
        # Fail closed: an unconfigured deployment serves nothing.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "authentication is not configured; refusing all requests. Set "
            "SREOI_OIDC_* for production or SREOI_AUTH_DEV_MODE=1 for local use.",
        )

    if api_key:
        return principal_from_api_key(session, api_key)

    token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif cookie:
        token = cookie

    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no credentials supplied")

    if settings.oidc_enabled:
        claims = verify_oidc_token(token, settings)
        return principal_from_claims(session, claims, credential="oidc")
    claims = verify_dev_token(token, settings)
    return principal_from_claims(session, claims, credential="dev")


# --------------------------------------------------------------------------
# Audit


def record_audit(
    session: Session,
    *,
    action: str,
    outcome: str,
    principal: Principal | None = None,
    target: str | None = None,
    detail: str | None = None,
    client_ip: str | None = None,
    organization_id: uuid.UUID | None = None,
) -> None:
    """Append-only audit. Detail must never carry a secret -- callers pass a
    description, never a token, password or key."""
    session.add(
        AuditEvent(
            organization_id=organization_id or (principal.organization_id if principal else None),
            actor_subject=principal.subject if principal else None,
            actor_role=principal.role.value if principal else None,
            action=action,
            target=target,
            outcome=outcome,
            detail=detail[:1000] if detail else None,
            client_ip=client_ip,
        )
    )


def record_audit_out_of_band(
    *,
    action: str,
    outcome: str,
    principal: Principal | None = None,
    target: str | None = None,
    detail: str | None = None,
    client_ip: str | None = None,
    organization_id: uuid.UUID | None = None,
) -> None:
    """Audit an event whose own transaction is about to be rolled back.

    A refused login raises, which rolls back the request session -- and an audit
    written there would vanish with it. Security-relevant *failures* are exactly
    the ones that must survive, so they are committed on their own connection.
    """
    from sreoi_persistence.db import get_session_factory

    session = get_session_factory()()
    try:
        record_audit(
            session,
            action=action,
            outcome=outcome,
            principal=principal,
            target=target,
            detail=detail,
            client_ip=client_ip,
            organization_id=organization_id,
        )
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def fingerprint(value: str) -> str:
    """A short non-reversible marker, for correlating without storing a secret."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]
