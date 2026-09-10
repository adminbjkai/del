"""Settings page, manual scan trigger + status polling, and manifest editing."""
from __future__ import annotations

import threading

import yaml
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from del_app import auditlog, auth
from del_app.auth import User
from del_app.config import get_settings
from del_app.db import get_db, q
from del_app.web.queries import _rows
from del_app.web.render import _csrf_response, _render, _require_csrf

try:
    from del_app import scanner
except ImportError:  # pragma: no cover
    scanner = None  # type: ignore[assignment]

try:
    from del_app import manifests
except ImportError:  # pragma: no cover
    manifests = None  # type: ignore[assignment]

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_view(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    settings = get_settings()
    conn = get_db()
    try:
        db_settings = _rows(q(conn, "SELECT * FROM settings"))
        recent_scans = _rows(q(conn, "SELECT * FROM scans ORDER BY id DESC LIMIT 10"))
    finally:
        conn.close()
    return _render(
        "settings.html",
        request,
        response,
        settings=settings.model_dump(),
        db_settings=db_settings,
        recent_scans=recent_scans,
        user=user,
    )


@router.post("/scan")
def trigger_scan(
    request: Request, user: User = Depends(auth.require_user), csrf_token: str = Form("")
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    if scanner is None:  # pragma: no cover
        return RedirectResponse(url="/settings?error=Scanner+unavailable", status_code=303)

    # run_scan()'s own in-process lock is non-blocking and raises immediately
    # when held, but that happens inside the background thread where nothing
    # here could observe it. scan_state() reads the same lock synchronously,
    # so check it first; the scanner module remains the single source of
    # truth for "is a scan running" (a race here just means two scans start
    # within the same instant, which the lock inside run_scan still prevents).
    state_fn = getattr(scanner, "scan_state", None)
    if state_fn is not None and state_fn().get("running"):
        return RedirectResponse(url="/settings?error=Scan+already+in+progress", status_code=303)

    def _run() -> None:
        try:
            scanner.run_scan()
        except scanner.ScanInProgressError:
            pass

    threading.Thread(target=_run, name="del-scan", daemon=True).start()

    auditlog.audit(user.id, "scan.run", "scanner", {})
    return RedirectResponse(url="/settings?flash=Scan+started", status_code=303)


@router.get("/scan/status")
def scan_status(user: User = Depends(auth.require_user)) -> JSONResponse:
    if scanner is None:  # pragma: no cover
        return JSONResponse({"running": False})
    # Lane A owns scan_state(); guard for import-time safety in case this
    # module loads before that lands.
    state = getattr(scanner, "scan_state", None)
    payload = dict(state()) if state is not None else {"running": False}

    conn = get_db()
    try:
        rows = _rows(q(conn, "SELECT * FROM scans ORDER BY id DESC LIMIT 1"))
    finally:
        conn.close()
    if rows:
        last = rows[0]
        payload["last_scan"] = {
            "id": last.get("id"),
            "status": last.get("status"),
            "finished": last.get("finished"),
        }
    return JSONResponse(payload)


@router.get("/manifests/{slug}", response_class=HTMLResponse)
def manifest_edit_form(
    slug: str, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    yaml_text = ""
    if manifests is not None:
        all_manifests = manifests.load_all()
        m = all_manifests.get(slug)
        if m is not None:
            data = m.model_dump() if hasattr(m, "model_dump") else dict(m)
            yaml_text = yaml.safe_dump(data, sort_keys=False)
    return _render(
        "manifest_edit.html",
        request,
        response,
        slug=slug,
        yaml_text=yaml_text,
        validation_errors=[],
    )


@router.post("/manifests/{slug}", response_class=HTMLResponse)
def manifest_edit_submit(
    slug: str,
    request: Request,
    response: Response,
    user: User = Depends(auth.require_user),
    csrf_token: str = Form(""),
    yaml_text: str = Form(""),
) -> HTMLResponse:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    errors: list[str] = []
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        errors.append(f"Invalid YAML: {exc}")
        data = None

    if data is not None and manifests is not None:
        try:
            manifest = manifests.Manifest(**data)
        except Exception as exc:  # pydantic ValidationError or similar
            errors.append(str(exc))
        else:
            manifests.save(manifest)
            auditlog.audit(user.id, "manifest.save", slug, {})
            return RedirectResponse(url=f"/apps/{slug}?flash=Manifest+saved", status_code=303)
    elif data is not None and manifests is None:  # pragma: no cover
        errors.append("Manifests module unavailable")

    return _render(
        "manifest_edit.html",
        request,
        response,
        slug=slug,
        yaml_text=yaml_text,
        validation_errors=errors,
    )
