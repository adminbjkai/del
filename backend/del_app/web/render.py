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


def _render(name: str, request: Request, response: Response, **extra) -> HTMLResponse:
    csrf_token, seed = _csrf_seed(request)
    ctx = {
        "flash": request.query_params.get("flash"),
        "error": request.query_params.get("error"),
        "csrf_token": csrf_token,
    }
    ctx.update(extra)
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
