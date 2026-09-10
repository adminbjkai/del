"""Dashboard route: stat cards, recent scans/jobs, last-scan strip, and the
'Needs attention' panel (apps with uncertain resource mappings)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from del_app import auth
from del_app.auth import User
from del_app.db import get_db, q
from del_app.web.formatting import _duration, _relative_dt
from del_app.web.gallery import _reclaimable_bytes
from del_app.web.orphans import _actionable_orphan_count
from del_app.web.queries import _disk_usage_bytes, _latest_scan_id, _rows
from del_app.web.render import _render

router = APIRouter()

# Cap the "Needs attention" panel: it links out to the full apps/resources
# pages for anything beyond a quick glance.
_ATTENTION_LIMIT = 10


def _attention_apps(conn, latest: int | None) -> list[dict[str, Any]]:
    """Apps with at least one uncertain (below-threshold) association, for the
    dashboard's 'Needs attention' panel. Reuses the same removal_eligible =
    'uncertain' definition as the `uncertain_mappings` stat card."""
    sql = """
        SELECT ap.slug AS slug, ap.name AS name, COUNT(a.id) AS n
        FROM associations a
        JOIN applications ap ON ap.id = a.app_id
        JOIN resources r ON r.id = a.resource_id
        WHERE a.removal_eligible = 'uncertain'
    """
    params: list[Any] = []
    if latest is not None:
        sql += " AND ap.last_seen = ? AND r.last_seen = ?"
        params.extend([latest, latest])
    sql += " GROUP BY ap.id ORDER BY n DESC, ap.name LIMIT ?"
    params.append(_ATTENTION_LIMIT)
    rows = _rows(q(conn, sql, tuple(params)))
    return [
        {
            "slug": r["slug"],
            "name": r["name"],
            "reason": f"{r['n']} uncertain resource mapping{'s' if r['n'] != 1 else ''}",
            "url": f"/apps/{r['slug']}",
        }
        for r in rows
    ]


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        latest = _latest_scan_id(conn)
        # Dashboard stat cards must match the counts on the pages they link
        # to (/apps, /orphans): scope everything to the latest scan, not
        # every application/resource ever seen across scan history.
        apps_sql = "SELECT * FROM applications"
        apps_params: tuple = ()
        if latest is not None:
            apps_sql += " WHERE last_seen = ?"
            apps_params = (latest,)
        apps = _rows(q(conn, apps_sql, apps_params))
        running_jobs = _rows(
            q(conn, "SELECT * FROM jobs WHERE status = 'running'")
        )
        # COUNT(*) rather than materialising rows just to call len(). The
        # EXISTS form is deliberate: the equivalent JOIN becomes plan-unstable
        # once sqlite_stat1 exists (it flips to a skip-scan and degrades ~10x).
        uncertain_sql = """
                SELECT COUNT(*) AS n FROM associations a
                WHERE a.removal_eligible = 'uncertain'
                  AND EXISTS (SELECT 1 FROM resources r
                              WHERE r.id = a.resource_id
                """
        uncertain_params: list[Any] = []
        if latest is not None:
            uncertain_sql += " AND r.last_seen = ?"
            uncertain_params.append(latest)
        uncertain_sql += """)
                  AND EXISTS (SELECT 1 FROM applications ap
                              WHERE ap.id = a.app_id
                """
        if latest is not None:
            uncertain_sql += " AND ap.last_seen = ?"
            uncertain_params.append(latest)
        uncertain_sql += ")"
        uncertain_count = _rows(q(conn, uncertain_sql, tuple(uncertain_params)))[0]["n"]

        # Scope shared resources the same way. Counting across all scan history
        # made this the one stat card that disagreed with the page it links to.
        shared_sql = """
                SELECT COUNT(DISTINCT a.resource_id) AS n FROM associations a
                JOIN resources r ON r.id = a.resource_id
                JOIN applications ap ON ap.id = a.app_id
                WHERE a.shared = 1 AND a.excluded = 0
                """
        shared_params: tuple = ()
        if latest is not None:
            shared_sql += " AND r.last_seen = ? AND ap.last_seen = ?"
            shared_params = (latest, latest)
        shared_count = _rows(q(conn, shared_sql, shared_params))[0]["n"]

        orphan_actionable = _actionable_orphan_count(conn, latest)
        recent_scans = _rows(
            q(conn, "SELECT * FROM scans ORDER BY id DESC LIMIT 5")
        )
        recent_jobs = _rows(q(conn, "SELECT * FROM jobs ORDER BY id DESC LIMIT 5"))
        disk_usage_bytes = _disk_usage_bytes(conn, latest)
        attention = _attention_apps(conn, latest)
    finally:
        conn.close()

    # Last completed scan summary for the dashboard header strip.
    last_scan = None
    if recent_scans:
        for s in recent_scans:
            if s.get("status") == "done":
                last_scan = s
                break
        if last_scan is None:
            last_scan = recent_scans[0]

    stats = {
        "apps": len(apps),
        "running": len(running_jobs),
        "orphan_candidates": orphan_actionable,
        "shared_resources": shared_count,
        "uncertain_mappings": uncertain_count,
        "disk_usage_bytes": disk_usage_bytes,
        "reclaimable_bytes": _reclaimable_bytes(),
    }
    return _render(
        "dashboard.html",
        request,
        response,
        stats=stats,
        recent_scans=recent_scans,
        recent_jobs=recent_jobs,
        last_scan=last_scan,
        last_scan_age=_relative_dt(last_scan.get("finished") or last_scan.get("started")) if last_scan else "",
        last_scan_duration=_duration(
            last_scan.get("started"), last_scan.get("finished")
        ) if last_scan else "—",
        attention=attention,
        user=user,
    )
