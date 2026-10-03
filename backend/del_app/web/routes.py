"""DEL web UI router: combines the routers of the domain modules in this
package (auth_routes, dashboard, apps, gallery, plans_jobs, resources,
orphans, assistant, settings, static_routes).

Every route sits behind auth.require_user except /login, /healthz (owned by
main.py) and the content-hashed files under /static/ (static_routes.py).
Tests that fake planner/jobs/scanner or the gallery probes patch the owning
submodule, because each handler looks names up in its own module globals.
"""
from __future__ import annotations

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

router = APIRouter()
for _module in (
    auth_routes, dashboard, apps, gallery, plans_jobs, resources, orphans,
    assistant, settings, static_routes,
):
    router.include_router(_module.router)
del _module

__all__ = ["router"]
