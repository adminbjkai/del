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

_TREND_SCANS = 90
_TREND_W, _TREND_H = 240.0, 48.0
_TREND_SERIES = (
    ("apps_total", "Applications", "line", ""),
    ("resources_total", "Resources", "line", ""),
    ("duration_seconds", "Scan time", "bars", "s"),
)


def _scan_trend(rows: list[dict]) -> list[dict]:
    """Small multiples over recent completed scans (oldest first): one SVG-
    ready series per figure scans record in stats_json. Failed scans and
    scans missing a figure are left out of that series, never drawn as 0."""
    out = []
    for key, label, mark, unit in _TREND_SERIES:
        pts = [
            (r["id"], r.get("started"), float(r["stats"][key]))
            for r in rows
            if isinstance((r.get("stats") or {}).get(key), (int, float))
        ]
        if len(pts) < 2:
            continue
        values = [v for _, _, v in pts]
        lo, hi = min(values), max(values)
        # Lines get a floor below the minimum so small changes stay visible;
        # bars start at zero so their heights compare honestly.
        base = 0.0 if mark == "bars" else lo - max((hi - lo) * 0.25, 1.0)
        span = (hi - base) or 1.0
        step = _TREND_W / len(pts)
        marks = []
        for i, (sid, started, v) in enumerate(pts):
            h = (v - base) / span * (_TREND_H - 4)
            marks.append({
                "scan": sid, "started": started, "value": v,
                "x": round(i * step, 2), "w": round(step, 2),
                "cx": round(i * step + step / 2, 2), "y": round(_TREND_H - h, 2), "h": round(h, 2),
            })
        out.append({
            "key": key, "label": label, "mark": mark, "unit": unit,
            "latest": values[-1], "lo": lo, "hi": hi, "count": len(pts),
            "first_scan": pts[0][0], "marks": marks,
            "points": " ".join(f"{m['cx']},{m['y']}" for m in marks),
            "w": _TREND_W, "h": _TREND_H,
        })
    return out


@router.get("/settings", response_class=HTMLResponse)
def settings_view(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    settings = get_settings()
    conn = get_db(read_snapshot=True)
    try:
        recent_scans = _rows(q(conn, "SELECT * FROM scans ORDER BY id DESC LIMIT 25"))
        trend_rows = _rows(q(
            conn,
            "SELECT id, started, stats_json FROM scans WHERE status = 'done' ORDER BY id DESC LIMIT ?",
            (_TREND_SCANS,),
        ))
    finally:
        conn.close()
    for scan in recent_scans + trend_rows:
        scan["stats"] = _json_or(scan.get("stats_json"), {})
    return _render(
        "settings.html",
        request,
        response,
        settings=settings.model_dump(),
        scan_interval_hours=settings.scan_interval_hours,
        recent_scans=recent_scans,
        trend=_scan_trend(list(reversed(trend_rows))),
        assistant_status=assistant_web.current_status(),
        sessions=auth.list_sessions(request, user.id),
        user=user,
    )


@router.post("/settings/sessions/end-others")
def end_other_sessions(
    request: Request, user: User = Depends(auth.require_user), csrf_token: str = Form("")
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    ended = auth.end_other_sessions(request, user.id)
    auditlog.audit(user.id, "sessions.end_others", f"user:{user.id}", {"ended": ended})
    noun = "session" if ended == 1 else "sessions"
    return RedirectResponse(url=f"/settings?flash=Signed+out+{ended}+other+{noun}#account", status_code=303)


def _background_scan() -> None:
    try:
        scanner.run_scan()
    except scanner.ScanInProgressError:
        pass
    except Exception:
        logger.exception("background scan failed; previous inventory retained")


@router.post("/scan")
def trigger_scan(
    request: Request, user: User = Depends(auth.require_user), csrf_token: str = Form("")
) -> Response:
    # The sidebar scan stamp posts with Accept: application/json and stays on
    # the page; a plain form post (no JS) gets the old redirect.
    wants_json = "application/json" in request.headers.get("accept", "")
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    if scanner.scan_state().get("running"):
        if wants_json:
            return JSONResponse({"started": False, "error": "A scan is already in progress"}, status_code=409)
        return RedirectResponse(url="/settings?error=Scan+already+in+progress", status_code=303)
    threading.Thread(target=_background_scan, name="del-scan", daemon=True).start()
    auditlog.audit(user.id, "scan.run", "scanner", {})
    if wants_json:
        return JSONResponse({"started": True})
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
