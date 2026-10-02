"""Settings page, manual scan trigger + status polling, and manifest editing."""
from __future__ import annotations

import logging
import threading

import yaml
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from del_app import auditlog, auth, manifests, scanner
from del_app.auth import User
from del_app.config import get_settings
from del_app.db import get_db, q
from del_app.web import assistant as assistant_web
from del_app.web.queries import _json_or, _rows
from del_app.web.render import _csrf_response, _render, _require_csrf

logger = logging.getLogger("del_app.web.settings")

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_view(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    settings = get_settings()
    conn = get_db(read_snapshot=True)
    try:
        db_settings = _rows(q(conn, "SELECT * FROM settings"))
        recent_scans = _rows(q(conn, "SELECT * FROM scans ORDER BY id DESC LIMIT 10"))
    finally:
        conn.close()
    for scan in recent_scans:
        scan["stats"] = _json_or(scan.get("stats_json"), {})
    return _render(
        "settings.html",
        request,
        response,
        settings=settings.model_dump(),
        db_settings=db_settings,
        recent_scans=recent_scans,
        assistant_status=assistant_web.current_status(),
        user=user,
    )


@router.post("/scan")
def trigger_scan(
    request: Request, user: User = Depends(auth.require_user), csrf_token: str = Form("")
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    if scanner.scan_state().get("running"):
        return RedirectResponse(url="/settings?error=Scan+already+in+progress", status_code=303)

    def _run() -> None:
        try:
            scanner.run_scan()
        except scanner.ScanInProgressError:
            pass
        except Exception:
            logger.exception("background scan failed; previous inventory retained")

    threading.Thread(target=_run, name="del-scan", daemon=True).start()

    auditlog.audit(user.id, "scan.run", "scanner", {})
    return RedirectResponse(url="/settings?flash=Scan+started", status_code=303)


@router.get("/scan/status")
def scan_status(user: User = Depends(auth.require_user)) -> JSONResponse:
    payload = scanner.scan_state()

    conn = get_db(read_snapshot=True)
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
    m = manifests.load_all().get(slug)
    if m is not None:
        yaml_text = yaml.safe_dump(m.model_dump(), sort_keys=False)
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

    if data is not None:
        try:
            manifest = manifests.Manifest(**data)
        except Exception as exc:  # pydantic ValidationError or similar
            errors.append(str(exc))
        else:
            if manifest.id != slug:
                errors.append(f"Manifest id must match the application slug ({slug!r})")
            else:
                manifests.save(manifest)
                auditlog.audit(user.id, "manifest.save", slug, {})
                return RedirectResponse(url=f"/apps/{slug}?flash=Manifest+saved", status_code=303)

    return _render(
        "manifest_edit.html",
        request,
        response,
        slug=slug,
        yaml_text=yaml_text,
        validation_errors=errors,
    )
