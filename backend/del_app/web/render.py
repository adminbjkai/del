"""Template rendering plumbing: Jinja env, CSRF seed cookie, and the shared
`_render` helper every route module uses to produce an HTMLResponse."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from del_app import auth
from del_app.web.formatting import (
    _duration,
    _format_dt,
    _human_size,
    _iso_sort_key,
    _level,
    _relative_dt,
)
from del_app.web.queries import RESOURCE_TYPE_LABELS

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


def _render(name: str, request: Request, response: Response, **extra) -> HTMLResponse:
    csrf_token, seed = _csrf_seed(request)
    ctx = {
        "flash": request.query_params.get("flash"),
        "error": request.query_params.get("error"),
        "csrf_token": csrf_token,
    }
    ctx.update(_dock_from_path(request.url.path))
    ctx.update(extra)
    if "assistant_status" not in ctx:
        try:
            from del_app.assistant import status as assistant_status_fn
            st = dict(assistant_status_fn())
            st.pop("api_key", None)
            ctx["assistant_status"] = st
        except Exception:
            ctx["assistant_status"] = {"enabled": False, "configured": False}
    if "assistant_on" not in ctx:
        st = ctx.get("assistant_status") or {}
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
templates.env.globals["level_of"] = _level
templates.env.globals["duration"] = _duration
templates.env.globals["format_dt"] = _format_dt
templates.env.globals["relative_dt"] = _relative_dt
templates.env.globals["iso_sort"] = _iso_sort_key
templates.env.globals["resource_labels"] = RESOURCE_TYPE_LABELS


def assistant_enabled() -> bool:
    """True when the assistant is enabled *and* configured, so templates can
    show/hide "Ask the assistant" deep links. Looked up lazily through
    del_app.web.assistant (which owns the guarded import) so a missing or
    failing assistant lane simply hides the buttons."""
    try:
        from del_app.web import assistant as assistant_web

        return assistant_web.is_enabled()
    except Exception:
        return False


templates.env.globals["assistant_enabled"] = assistant_enabled
