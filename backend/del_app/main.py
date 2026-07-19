"""FastAPI app factory for DEL."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from del_app.auth import NeedsLogin
from del_app.db import get_db, q


def create_app() -> FastAPI:
    app = FastAPI(title="DEL")

    @app.exception_handler(NeedsLogin)
    async def _needs_login_handler(request: Request, exc: NeedsLogin) -> RedirectResponse:
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        scan_id = None
        conn = get_db()
        try:
            rows = q(conn, "SELECT id FROM scans ORDER BY id DESC LIMIT 1")
            if rows:
                scan_id = rows[0]["id"]
        except Exception:
            scan_id = None
        finally:
            conn.close()
        return JSONResponse({"ok": True, "scan": scan_id})

    try:
        from del_app.web.routes import router
        app.include_router(router)
    except ImportError:
        pass

    return app


app = create_app()
