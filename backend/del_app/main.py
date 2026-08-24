"""FastAPI app factory for DEL."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from del_app.auth import NeedsLogin
from del_app.db import get_db, q

logger = logging.getLogger("del_app.main")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # On every process start: mark scans left 'running' by a prior kill/restart
    # as failed so Settings never shows ghost in-progress rows.
    try:
        from del_app.scanner import abandon_stale_scans

        n = abandon_stale_scans()
        if n:
            logger.warning("startup: abandoned %s stale running scan(s)", n)
    except Exception:
        logger.exception("startup: abandon_stale_scans failed")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="DEL", lifespan=_lifespan)

    @app.exception_handler(NeedsLogin)
    async def _needs_login_handler(request: Request, exc: NeedsLogin) -> RedirectResponse:
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # Report latest *completed* scan (not a mid-flight / abandoned row).
        scan_id = None
        conn = get_db()
        try:
            rows = q(
                conn,
                "SELECT id FROM scans WHERE status = 'done' ORDER BY id DESC LIMIT 1",
            )
            if rows:
                scan_id = rows[0]["id"]
        except Exception:
            scan_id = None
        finally:
            conn.close()
        return JSONResponse({"ok": True, "scan": scan_id})

    # Deliberately NOT guarded: a broken import here used to be swallowed,
    # producing a process that started cleanly, answered /healthz green (it is
    # defined above, in this module) and served 404 for the entire UI. A failed
    # deploy must fail loudly.
    from del_app.web.routes import router

    app.include_router(router)

    return app


app = create_app()
