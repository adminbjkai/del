"""FastAPI app factory for DEL."""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from del_app.auth import NeedsLogin
from del_app.db import get_db, latest_done_scan_id

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
    try:
        from del_app.jobs import abandon_interrupted_jobs

        abandon_interrupted_jobs()
    except Exception:
        logger.exception("startup: abandon_interrupted_jobs failed")
    stop = threading.Event()
    try:
        from del_app.config import get_settings
        from del_app.scanner import start_scheduler

        start_scheduler(get_settings().scan_interval_hours, stop)
    except Exception:
        logger.exception("startup: scan scheduler not started")
    yield
    stop.set()


def create_app() -> FastAPI:
    # No public API docs: every route except /login, /healthz and /static needs a session.
    app = FastAPI(title="DEL", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(NeedsLogin)
    async def _needs_login_handler(request: Request, exc: NeedsLogin) -> RedirectResponse:
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # Monitoring must detect a missing/broken schema, not just a live process.
        conn = None
        try:
            conn = get_db()
            scan_id = latest_done_scan_id(conn)
            for table in ("applications", "resources", "associations", "jobs", "sessions"):
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
            conn.execute("SELECT user_excluded, user_shared FROM associations LIMIT 1")
            return JSONResponse({"ok": True, "scan": scan_id})
        except Exception:
            logger.exception("healthz: database unavailable or schema incomplete")
            return JSONResponse({"ok": False, "scan": None}, status_code=503)
        finally:
            if conn is not None:
                conn.close()

    # Deliberately NOT guarded: a broken import here used to be swallowed,
    # producing a process that started cleanly, answered /healthz green (it is
    # defined above, in this module) and served 404 for the entire UI. A failed
    # deploy must fail loudly.
    from del_app.web.routes import router

    app.include_router(router)

    return app


app = create_app()
