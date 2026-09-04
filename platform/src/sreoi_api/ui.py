"""The server-rendered UI shell: templates and per-request presentation context.

Extracted from `main` so that auto-discovered routers can render pages too. A
router cannot import `main` -- `main` imports the routers -- and the sign-in
page has to live in the auth router, next to the endpoint it posts to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from sreoi_api.i18n import (
    direction,
    format_number,
    localise_digits,
    normalise_locale,
    translator,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def ui_context(request: Request) -> dict[str, Any]:
    locale = normalise_locale(request.query_params.get("lang"))
    return {
        "locale": locale,
        "dir": direction(locale),
        "t": translator(locale),
        "num": lambda v, d=0: format_number(v, locale, d),
        "digits": lambda s: localise_digits(str(s), locale),
        "other_locale": "en" if locale == "ar" else "ar",
        "query": request.query_params,
        "principal": getattr(request.state, "principal", None),
    }
