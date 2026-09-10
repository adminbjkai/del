"""DEL web UI routes: dashboard, apps, plans, jobs, resources, orphans,
settings, manifests. All routes here (except /login, /healthz which is owned
by main.py, and the six static assets — app.css, app.js, theme-init.js,
favicon.svg, assistant.js, assistant.css — served unauthenticated from
static_routes.py) sit behind auth.require_user.

This module is a thin aggregator over the domain modules in this package
(dashboard, apps, plans_jobs, resources, orphans, gallery, settings,
assistant, auth_routes, static_routes). Each owns its own routes; this module just
combines their routers and re-exports the handful of pure helpers/constants
that tests import directly from `del_app.web.routes`.

Other lanes' modules (planner, jobs, scanner, manifests) are imported lazily
/ defensively by the modules that use them (plans_jobs.py, settings.py), so
this package still imports cleanly (and is testable with monkeypatched
fakes) even before those lanes land. Tests that need to fake `planner`/
`jobs`/`scanner`/`_probe_domain(s)`/`_cached_icon` must patch them on the
owning submodule (`del_app.web.plans_jobs`, `del_app.web.settings`,
`del_app.web.gallery`) — patching the re-export here would not affect the
route handler, which looks the name up in its own module's globals.
"""
from __future__ import annotations

import urllib.request

from fastapi import APIRouter

from del_app.web import (
    apps,
    assistant,
    auth_routes,
    dashboard,
    gallery,
    orphans,
    plans_jobs,
    resources,
    settings,
    static_routes,
)
from del_app.web.formatting import (
    _format_dt,
    _installed_at_from_resources,
    _iso_sort_key,
    _parse_dt,
)
from del_app.web.gallery import (
    _APP_PROBE_CACHE,
    _APP_PROBE_LOCK,
    _gallery_category,
    _ICON_CACHE,
    _probe_domains,
    _PROBE_REFRESHING,
    _valid_gallery_domain,
)
from del_app.web.orphans import classify_orphan_candidate
from del_app.web.plans_jobs import JOBS_PAGE_LIMIT

# `import urllib.request` above exists purely so `routes.urllib.request` is a
# valid attribute path for tests to monkeypatch (e.g. urlopen); it patches the
# real stdlib module, shared by every submodule that also imports it.
assert urllib.request is not None

router = APIRouter()
for _module in (
    auth_routes, dashboard, apps, gallery, plans_jobs, resources, orphans,
    assistant, settings, static_routes,
):
    router.include_router(_module.router)
del _module

__all__ = [
    "router",
    "classify_orphan_candidate",
    "_format_dt",
    "_parse_dt",
    "_iso_sort_key",
    "_installed_at_from_resources",
    "_gallery_category",
    "_valid_gallery_domain",
    "_probe_domains",
    "_APP_PROBE_CACHE",
    "_APP_PROBE_LOCK",
    "_PROBE_REFRESHING",
    "_ICON_CACHE",
    "JOBS_PAGE_LIMIT",
]
