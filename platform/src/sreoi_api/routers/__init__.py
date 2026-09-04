"""Auto-discovered API routers.

A feature adds one module in this package and it is picked up automatically.
This seam exists so that several features can be built concurrently without any
of them editing a shared registration file, which is the usual source of merge
conflicts on a fast-moving codebase.

A module qualifies by exposing a module-level `router: APIRouter`, which should
carry its own `prefix` and `tags`.
"""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter


def discover() -> list[APIRouter]:
    """Import every non-private module here and collect its `router`."""
    routers: list[APIRouter] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers.append(router)
    return routers
