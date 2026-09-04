"""Credential endpoints. Public by necessity -- this is how a caller obtains one."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sreoi_api.auth import (
    SESSION_COOKIE,
    Principal,
    issue_dev_token,
    load_settings,
    record_audit,
    record_audit_out_of_band,
)
from sreoi_api.i18n import register_strings
from sreoi_api.ui import TEMPLATES, ui_context
from sreoi_persistence.credentials import generate_api_key, verify_password
from sreoi_persistence.db import get_session_factory
from sreoi_persistence.models_identity import ApiKey, Membership, Organization, Role, User

router = APIRouter(prefix="/auth", tags=["auth"])

register_strings(
    "en",
    {
        "nav.signin": "Sign in",
        "auth.signedin": "Signed in as",
        "auth.signin": "Sign in",
        "auth.signout": "Sign out",
        "auth.email": "Email",
        "auth.password": "Password",
        "auth.subtitle": "Every page except the API description requires a credential.",
        "auth.devwarning": (
            "This deployment uses the development password issuer, not an "
            "identity provider. It is intended for local use only."
        ),
        "auth.oidconly": "This deployment authenticates through its identity provider:",
        "auth.notconfigured": (
            "Authentication is not configured on this deployment, so it is "
            "serving nothing. Configure an OIDC issuer, or set "
            "SREOI_AUTH_DEV_MODE=1 for local use."
        ),
    },
)
register_strings(
    "ar",
    {
        "nav.signin": "تسجيل الدخول",
        "auth.signedin": "مسجل الدخول باسم",
        "auth.signin": "تسجيل الدخول",
        "auth.signout": "تسجيل الخروج",
        "auth.email": "البريد الإلكتروني",
        "auth.password": "كلمة المرور",
        "auth.subtitle": "كل صفحة تتطلب بيانات اعتماد، عدا وصف الواجهة البرمجية.",
        "auth.devwarning": (
            "يستخدم هذا النشر مُصدِر كلمات المرور التطويري، لا مزود هوية. "
            "وهو مخصص للاستخدام المحلي فقط."
        ),
        "auth.oidconly": "يتم التوثيق في هذا النشر عبر مزود الهوية:",
        "auth.notconfigured": (
            "لم يتم إعداد التوثيق في هذا النشر، ولذلك لا يقدم أي بيانات. "
            "قم بإعداد مُصدِر OIDC، أو عيّن SREOI_AUTH_DEV_MODE=1 للاستخدام المحلي."
        ),
    },
)


def safe_next(raw: str | None) -> str:
    """Reduce a caller-supplied redirect target to a same-origin path.

    An unvalidated `next` is an open redirect, and after sign-in it would be a
    credentialled one. Anything that is not a plain absolute path -- a scheme,
    a protocol-relative `//host`, a backslash that some browsers normalise to a
    slash -- collapses to the root.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return "/"
    if raw.startswith("/auth/"):  # never bounce back into sign-in
        return "/"
    return raw


register_strings("ar", {"nav.signin": "تسجيل الدخول", "auth.signedin": "مسجّل الدخول باسم"})


def _session() -> Any:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(_session)]


class LoginRequest(BaseModel):
    # A plain string, not EmailStr. The identity store holds arbitrary
    # addresses -- `admin@localhost` among them -- and an endpoint stricter
    # than its own store makes those accounts unreachable. It is also a lookup
    # key here, so rejecting a format would answer a question (422 vs 401) that
    # the endpoint deliberately refuses to answer.
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1)
    organization: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    organization_id: uuid.UUID
    role: str
    expires_in_seconds: int


class MeResponse(BaseModel):
    subject: str
    email: str | None
    organization_id: uuid.UUID
    organization: str
    role: str
    credential: str


class ApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: Role = Role.ANALYST


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    role: str
    secret: str = Field(description="Shown once and never recoverable. Store it now.")


@router.get("/config")
def auth_config() -> dict[str, Any]:
    """What this deployment accepts. Deliberately reveals no secrets."""
    settings = load_settings()
    return {
        "oidc_enabled": settings.oidc_enabled,
        "oidc_issuer": settings.oidc_issuer,
        "dev_mode": settings.dev_mode,
        "configured": settings.configured,
        "accepts": (
            ["oidc_bearer", "api_key"]
            if settings.oidc_enabled
            else (["dev_password", "api_key"] if settings.dev_mode else [])
        ),
    }


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest, request: Request, response: Response, session: SessionDep
) -> TokenResponse:
    """Local development login. Refused unless SREOI_AUTH_DEV_MODE=1.

    With OIDC configured this endpoint is unavailable: the identity provider
    owns credentials and the browser flow belongs there, not here.
    """
    settings = load_settings()
    client_ip = request.client.host if request.client else None

    if not settings.dev_mode:
        record_audit_out_of_band(
            action="login",
            outcome="REFUSED",
            detail="password login attempted while dev mode is off",
            client_ip=client_ip,
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "password login is disabled on this deployment; authenticate via OIDC",
        )

    user = session.scalar(select(User).where(User.email == payload.email))
    # Same response whether the user is absent or the password is wrong, so the
    # endpoint does not confirm which addresses exist.
    if (
        user is None
        or not user.is_active
        or not user.password_hash
        or not verify_password(payload.password, user.password_hash)
    ):
        record_audit_out_of_band(
            action="login",
            outcome="FAILED",
            detail="invalid credentials",
            client_ip=client_ip,
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    stmt = select(Membership).where(Membership.user_id == user.id)
    if payload.organization:
        stmt = stmt.join(Organization).where(Organization.slug == payload.organization)
    membership = session.scalar(stmt)
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no organisation membership")

    role = Role(membership.role)
    token = issue_dev_token(
        settings,
        subject=user.subject,
        organization_id=membership.organization_id,
        role=role,
    )
    user.last_login_at = datetime.now(UTC)
    record_audit(
        session,
        action="login",
        outcome="SUCCEEDED",
        target=user.subject,
        organization_id=membership.organization_id,
        client_ip=client_ip,
    )

    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=bool(request.url.scheme == "https"),
        max_age=12 * 3600,
        path="/",
    )
    return TokenResponse(
        access_token=token,
        organization_id=membership.organization_id,
        role=role.value,
        expires_in_seconds=12 * 3600,
    )


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/signin", response_class=HTMLResponse)
def signin_page(request: Request) -> HTMLResponse:
    """The browser sign-in page.

    Public by necessity, and it renders what this deployment actually accepts
    rather than a password form that may be refused: with OIDC configured the
    form is absent, because the identity provider owns credentials.
    """
    settings = load_settings()
    context = ui_context(request)
    context.update(
        page="signin",
        configured=settings.configured,
        dev_mode=settings.dev_mode,
        oidc_issuer=settings.oidc_issuer,
        next_path=safe_next(request.query_params.get("next")),
    )
    return TEMPLATES.TemplateResponse(request, "signin.html", context)


@router.get("/signout")
def signout(request: Request) -> RedirectResponse:
    """Clear the session cookie and return to the sign-in page.

    A GET so it can be an ordinary link in the page header. It destroys only
    the caller's own cookie, so CSRF on it costs a redundant sign-in and
    nothing else -- but it must never be able to redirect off-origin, hence
    the fixed destination.
    """
    locale = ui_context(request)["locale"]
    response = RedirectResponse(f"/auth/signin?lang={locale}", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me", response_model=MeResponse)
def me(request: Request, session: SessionDep) -> MeResponse:
    """Requires a credential. Public prefix, but this handler checks explicitly."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no credentials supplied")
    organization = session.get(Organization, principal.organization_id)
    return MeResponse(
        subject=principal.subject,
        email=principal.email,
        organization_id=principal.organization_id,
        organization=organization.name if organization else "unknown",
        role=principal.role.value,
        credential=principal.credential,
    )


@router.post("/api-keys", response_model=ApiKeyResponse, status_code=201)
def create_api_key(payload: ApiKeyRequest, request: Request, session: SessionDep) -> ApiKeyResponse:
    """Mint an API key for the caller's organisation. ORG_ADMIN or above.

    A key may not be granted a role above the caller's own -- privilege
    escalation by key minting would defeat the role model.
    """
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no credentials supplied")
    principal.require(Role.ORG_ADMIN)
    if not principal.role.at_least(payload.role):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"cannot mint a {payload.role.value} key as {principal.role.value}",
        )

    secret, prefix, key_hash = generate_api_key()
    record = ApiKey(
        organization_id=principal.organization_id,
        created_by_user_id=principal.user_id,
        name=payload.name,
        prefix=prefix,
        key_hash=key_hash,
        role=payload.role.value,
    )
    session.add(record)
    session.flush()
    record_audit(
        session,
        action="api_key.create",
        outcome="SUCCEEDED",
        principal=principal,
        target=prefix,
        detail=f"role={payload.role.value}",  # never the secret
    )
    return ApiKeyResponse(
        id=record.id,
        name=record.name,
        prefix=prefix,
        role=payload.role.value,
        secret=secret,
    )
