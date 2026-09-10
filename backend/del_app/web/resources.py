"""Resources tab: per-type listing with owner/shared annotations."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from del_app import auth
from del_app.auth import User
from del_app.db import get_db, q
from del_app.web.queries import (
    RESOURCE_TYPE_LABELS,
    _json_or,
    _latest_scan_id,
    _normalize_type,
    _owner_map,
    _rows,
    _type_counts,
)
from del_app.web.render import _render

router = APIRouter()


@router.get("/resources")
def resources_index(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> RedirectResponse:
    # 303, not 307: 307 preserves the method, which is meaningless for a GET
    # landing redirect and surprising if anything ever POSTs here.
    return RedirectResponse(url="/resources/container", status_code=303)


@router.get("/resources/{res_type}", response_class=HTMLResponse)
def resources_view(
    res_type: str,
    request: Request,
    response: Response,
    user: User = Depends(auth.require_user),
    filter: str = "",
) -> HTMLResponse:
    db_type = _normalize_type(res_type)
    conn = get_db()
    try:
        latest = _latest_scan_id(conn)
        type_counts = _type_counts(conn, latest)
        if latest is not None:
            rows = _rows(
                q(
                    conn,
                    "SELECT * FROM resources WHERE type = ? AND last_seen = ? ORDER BY display",
                    (db_type, latest),
                )
            )
        else:
            rows = _rows(
                q(conn, "SELECT * FROM resources WHERE type = ? ORDER BY display", (db_type,))
            )
        owners = _owner_map(conn, [r["id"] for r in rows])
    finally:
        conn.close()
    for r in rows:
        r["data"] = _json_or(r.get("data_json"), {})
        info = owners.get(r["id"], {"apps": [], "shared": False})
        r["owners"] = info["apps"]
        r["shared"] = info["shared"]
    # `filter` is a client-side prefill only (the table's own JS-side search
    # box); it is never applied to the SQL query above.
    return _render(
        "resources.html",
        request,
        response,
        res_type=db_type,
        res_label=RESOURCE_TYPE_LABELS.get(db_type, db_type),
        rows=rows,
        type_counts=type_counts,
        prefill=filter,
    )
