"""Login / logout routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from del_app import auditlog, auth
from del_app.auth import User
from del_app.web.render import _csrf_response, _render, _require_csrf

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return _render("login.html", request, HTMLResponse(""))


@router.post("/login")
def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
) -> Response:
    if not _require_csrf(request, csrf_token):
        return RedirectResponse(url="/login?error=Invalid+request", status_code=303)

    ip = request.client.host if request.client else "unknown"
    if auth.rate_limited(ip):
        return RedirectResponse(
            url="/login?error=Too+many+attempts%2C+try+again+later", status_code=303
        )
    auth.record_attempt(ip)

    user_id = auth.verify(username, password)
    if user_id is None:
        return RedirectResponse(url="/login?error=Invalid+credentials", status_code=303)

    redirect = RedirectResponse(url="/", status_code=303)
    auth.login_session(redirect, user_id, ip=ip)
    auditlog.audit(user_id, "login", "session", {"ip": ip})
    return redirect


@router.post("/logout")
def logout(
    request: Request, user: User = Depends(auth.require_user), csrf_token: str = Form("")
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    redirect = RedirectResponse(url="/login", status_code=303)
    auth.logout_session(request, redirect)
    auditlog.audit(user.id, "logout", "session", {})
    return redirect
