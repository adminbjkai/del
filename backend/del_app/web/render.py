"""Template rendering plumbing: Jinja env, CSRF seed cookie, and the shared
`_render` helper every route module uses to produce an HTMLResponse."""
from __future__ import annotations

import logging
import secrets
import socket
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from del_app import auth
from del_app.db import get_db
from del_app.web.formatting import (
    _duration,
    _seconds,
    _format_dt,
    _human_size,
    _iso_sort_key,
    _relative_dt,
)
from del_app.web.queries import resource_type_label

logger = logging.getLogger("del_app.web.render")

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _csrf_seed(request: Request) -> tuple[str, str | None]:
    """Return (csrf_token, raw_seed_to_persist_or_None). If the request
    already carries a session cookie, derive the CSRF token from it and
    nothing new needs to be persisted. Otherwise (e.g. a fresh /login visit)
    mint a throwaway anti-forgery seed that must be set as a cookie on the
    response."""
    cookie = request.cookies.get(auth.SESSION_COOKIE_NAME)
    token = auth.unsign_token(cookie) if cookie else None
    if token is not None:
        return auth.csrf_token(token), None
    raw = secrets.token_hex(16)
    return auth.csrf_token(raw), raw


def _dock_from_path(path: str) -> dict:
    """Default assistant-dock scope from the page the operator is on."""
    scope, target, rtype = "general", "", ""
    parts = [p for p in path.split("/") if p]
    if path.startswith("/apps/") and "/plan" not in path and len(parts) >= 2:
        scope, target = "app", parts[1]
    elif path.startswith("/orphans"):
        scope = "orphans"
    elif path.startswith("/resources/") and len(parts) >= 2:
        scope = "resource_type"
        target = rtype = parts[1]
    return {"dock_scope": scope, "dock_target": target, "dock_rtype": rtype}


def glossary_ctx(path: str) -> str:
    """Which Help-rail sections apply to a page (body[data-glossary])."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "general"
    head = parts[0]
    if head == "apps":
        if len(parts) == 1:
            return "apps"
        return "jobs" if "plan" in parts[2:] else "app-detail"
    if head == "resources":
        return f"resources-{parts[1]}" if len(parts) > 1 else "resources"
    if head == "jobs":
        return "job-detail" if len(parts) > 1 else "jobs"
    if head in ("view-apps", "orphans", "assistant"):
        return head
    if head == "plans":
        return "jobs"
    return "general"


def _scan_block() -> dict:
    """The sidebar's scan stamp: latest completed scan and whether one runs now.
    Never fails a page render: a broken DB just shows an empty stamp."""
    from del_app import scanner

    try:
        state = scanner.scan_state()
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT id, started, finished FROM scans WHERE status = 'done' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.exception("scan stamp unavailable")
        return {}
    block = {"running": bool(state.get("running")), "started": state.get("started")}
    if row is not None:
        block.update(
            id=row["id"],
            finished=row["finished"],
            age=_relative_dt(row["finished"]) or "just now",
            duration=_duration(row["started"], row["finished"]),
        )
    return block


def _render(name: str, request: Request, response: Response, **extra) -> HTMLResponse:
    csrf_token, seed = _csrf_seed(request)
    ctx = {
        "flash": request.query_params.get("flash"),
        "error": request.query_params.get("error"),
        "csrf_token": csrf_token,
    }
    if name != "login.html":
        ctx["scan_block"] = _scan_block()
    ctx.update(_dock_from_path(request.url.path))
    ctx.update(extra)
    if "assistant_status" not in ctx:
        from del_app.web import assistant as assistant_web  # imports this module

        ctx["assistant_status"] = assistant_web.current_status()
    if "assistant_on" not in ctx:
        st = ctx["assistant_status"]
        ctx["assistant_on"] = bool(st.get("enabled") and st.get("configured"))
    rendered = templates.TemplateResponse(request, name, ctx)
    if seed is not None:
        # Same cookie name as the real session, so it needs the same flags —
        # notably `secure`, which this pre-login anti-forgery seed was missing
        # while auth.login_session set it correctly.
        rendered.set_cookie(
            auth.SESSION_COOKIE_NAME, auth.sign_token(seed),
            httponly=True, secure=True, samesite="lax",
        )
    return rendered


def _require_csrf(request: Request, submitted: str | None) -> bool:
    return auth.check_csrf(request, submitted or "")


def _csrf_response() -> JSONResponse:
    return JSONResponse({"error": "invalid csrf token"}, status_code=403)


# ---------------------------------------------------------------------------
# Jinja globals (template-side formatting helpers)
# ---------------------------------------------------------------------------
templates.env.globals["human_size"] = _human_size
templates.env.globals["duration"] = _duration
templates.env.globals["seconds"] = _seconds
templates.env.globals["format_dt"] = _format_dt
templates.env.globals["relative_dt"] = _relative_dt
templates.env.globals["iso_sort"] = _iso_sort_key
templates.env.globals["glossary_ctx"] = glossary_ctx
templates.env.globals["resource_type_label"] = resource_type_label
templates.env.globals["host_name"] = socket.gethostname()
