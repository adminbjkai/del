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

# `?filter=` values applied on the server so a drill-down shows exactly the
# rows its dashboard tile counted. Any other value is only a client-side
# quick-filter prefill.
SERVER_FILTERS = {
    "shared": "shared by more than one current app",
    "dangling": "dangling images (untagged, unused by any container)",
    "unassigned": "no owner app (no association rows)",
}


def _shared_resource_types(conn, latest: int | None) -> dict[int, str]:
    """resource_id -> type for shared, non-excluded associations — the same
    definition as the dashboard's "Shared resources" tile."""
    sql = """
        SELECT DISTINCT r.id AS id, r.type AS type FROM associations a
        JOIN resources r ON r.id = a.resource_id
        JOIN applications ap ON ap.id = a.app_id
        WHERE a.shared = 1 AND a.excluded = 0
    """
    params: tuple = ()
    if latest is not None:
        sql += " AND r.last_seen = ? AND ap.last_seen = ?"
        params = (latest, latest)
    return {r["id"]: r["type"] for r in _rows(q(conn, sql, params))}


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
        active_filter = filter if filter in SERVER_FILTERS else ""
        shared_types = _shared_resource_types(conn, latest) if active_filter == "shared" else {}
    finally:
        conn.close()
    for r in rows:
        r["data"] = _json_or(r.get("data_json"), {})
        info = owners.get(r["id"], {"apps": [], "shared": False})
        r["owners"] = info["apps"]
        r["shared"] = info["shared"]
    total = len(rows)
    filter_counts: list[dict] = []
    if active_filter == "shared":
        rows = [r for r in rows if r["id"] in shared_types]
        per_type: dict[str, int] = {}
        for t in shared_types.values():
            per_type[t] = per_type.get(t, 0) + 1
        filter_counts = [
            {"type": t, "label": RESOURCE_TYPE_LABELS.get(t, t), "count": n}
            for t, n in sorted(per_type.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
    elif active_filter == "dangling":
        rows = [r for r in rows if r["data"].get("dangling")]
    elif active_filter == "unassigned":
        rows = [r for r in rows if not r["owners"]]
    return _render(
        "resources.html",
        request,
        response,
        res_type=db_type,
        res_label=RESOURCE_TYPE_LABELS.get(db_type, db_type),
        rows=rows,
        type_counts=type_counts,
        prefill="" if active_filter else filter,
        active_filter=active_filter,
        active_filter_label=SERVER_FILTERS.get(active_filter, ""),
        filter_counts=filter_counts,
        total_rows=total,
    )
