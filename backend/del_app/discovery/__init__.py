"""Discovery source modules.

Each submodule exposes a single ``collect() -> list[Resource]`` function that is
read-only against the host: it never stops, starts, or modifies anything, and it
never captures secret VALUES (only names, e.g. env var names) into Resource.data.
Every collector tolerates partial failure: on error it logs and returns whatever
it could gather (or an empty list), it never raises out of ``collect()``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("del_app.discovery")
