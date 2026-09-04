"""Authentication enforcement.

Enforcement is a middleware rather than a per-route dependency on purpose:
routers are discovered automatically (see `routers/__init__.py`), so a new
feature module would otherwise arrive unprotected by default. Here the default
is deny, and exposure is an explicit entry in `PUBLIC_PATHS`.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from sreoi_api.auth import (
    API_KEY_HEADER,
    SESSION_COOKIE,
    AuthConfigurationError,
    AuthSettings,
    current_organization,
    load_settings,
    resolve_principal,
)
from sreoi_persistence.db import get_session_factory

# Deliberately short, and every entry is a decision.
#   /health          liveness, no data
#   /docs /redoc /openapi.json  the API description, no data
#   /static/*        vendored assets
#   /auth/*          how a caller obtains a credential in the first place
PUBLIC_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/health/?$"),
    re.compile(r"^/docs/?$"),
    re.compile(r"^/redoc/?$"),
    re.compile(r"^/openapi\.json$"),
    re.compile(r"^/static/.*$"),
    # Credential acquisition only. /auth/me and /auth/api-keys are NOT public:
    # they need a principal, so they fall through to enforcement.
    re.compile(r"^/auth/config/?$"),
    re.compile(r"^/auth/login/?$"),
    re.compile(r"^/auth/logout/?$"),
    # The browser sign-in page and the link that clears the cookie. Both must
    # be reachable without a credential or there is no way in.
    re.compile(r"^/auth/signin/?$"),
    re.compile(r"^/auth/signout/?$"),
    re.compile(r"^/favicon\.ico$"),
)


def is_public(path: str) -> bool:
    return any(pattern.match(path) for pattern in PUBLIC_PATHS)


def wants_html(request: Request) -> bool:
    """A navigating browser, as opposed to an API client.

    Only GET counts: bouncing a POST to a sign-in page would silently discard
    the body, and a client that gets a redirect instead of 401 cannot tell that
    its write did not happen.
    """
    if request.method != "GET":
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Resolve a credential for every non-public request, or refuse it."""

    def __init__(self, app: Callable[..., object], settings: AuthSettings | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        # None means "read the environment on each request". Capturing settings
        # at construction made the app's auth configuration frozen at import
        # time, which is both surprising in operation and untestable: changing
        # the configuration required reloading the module, and a reloaded module
        # leaked its state into everything imported afterwards.
        self._settings = settings

    def _current_settings(self) -> AuthSettings:
        return self._settings if self._settings is not None else load_settings()

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method == "OPTIONS" or is_public(request.url.path):
            return await call_next(request)

        try:
            settings = self._current_settings()
        except AuthConfigurationError as exc:
            return JSONResponse(
                status_code=503,
                content={"detail": f"authentication is misconfigured: {exc}"},
            )

        session = get_session_factory()()
        try:
            principal = resolve_principal(
                session,
                settings,
                authorization=request.headers.get("authorization"),
                api_key=request.headers.get(API_KEY_HEADER),
                cookie=request.cookies.get(SESSION_COOKIE),
            )
            session.commit()
        except HTTPException as exc:
            session.rollback()
            # A browser gets the sign-in page with somewhere to return to; an
            # API client gets the status code, because a redirect to HTML would
            # look like success to anything checking only `response.ok`.
            if exc.status_code == 401 and wants_html(request):
                target = request.url.path
                if request.url.query:
                    target = f"{target}?{request.url.query}"
                return RedirectResponse(
                    f"/auth/signin?next={quote(target, safe='')}", status_code=303
                )
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
            )
        finally:
            session.close()

        request.state.principal = principal
        # Bind the tenant for this request so row-level security can key on it.
        token = current_organization.set(principal.organization_id)
        try:
            response = await call_next(request)
        finally:
            current_organization.reset(token)

        response.headers["X-Organization"] = str(principal.organization_id)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline response hardening."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            # Vendored assets only; no third-party origins, matching the
            # decision to self-host MapLibre rather than use a CDN.
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
        return response
