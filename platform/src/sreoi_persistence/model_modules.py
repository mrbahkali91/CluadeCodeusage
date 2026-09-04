"""Load every `models_*.py` so SQLAlchemy metadata is complete.

SQLAlchemy only knows about tables whose modules have been imported. Rather
than maintain a hand-edited import list -- another shared file that conflicts
whenever two features add tables at once -- feature tables live in
`models_<feature>.py` and are discovered here.
"""

from __future__ import annotations

import importlib
import pkgutil


def load_all() -> list[str]:
    import sreoi_persistence

    loaded: list[str] = []
    for info in sorted(pkgutil.iter_modules(sreoi_persistence.__path__), key=lambda i: i.name):
        if info.name.startswith("models_"):
            importlib.import_module(f"sreoi_persistence.{info.name}")
            loaded.append(info.name)
    return loaded
