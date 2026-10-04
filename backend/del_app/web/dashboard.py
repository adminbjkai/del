"""Dashboard: the site plan (every app as one lot, coloured by status), the
figures strip, what changed since the previous scan, apps that need
attention, and recent jobs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from del_app import auth
from del_app.auth import User
from del_app.config import get_settings
from del_app.db import get_db, q
from del_app.web.docker_df import docker_df
from del_app.web.orphans import _actionable_orphan_count
from del_app.web.queries import (
    RESOURCE_TYPE_LABELS,
    _app_aggregates,
    _disk_usage_bytes,
    _json_or,
    _latest_scan_id,
    _rows,
)
from del_app.web.render import _render

router = APIRouter()

# Cap the "Needs attention" list; the apps page has the rest.
_ATTENTION_LIMIT = 10
# Site-plan groups, in display order. Anything else lands in "other".
_KIND_ORDER = ["compose", "container", "systemd", "manifest"]
_STATUS_ORDER = ["running", "stopped", "absent", "unknown"]


def _attention_apps(conn, latest: int | None) -> list[dict[str, Any]]:
    """Apps with at least one uncertain (probable 60–79, not yet safe)
    association: the same removal_eligible = 'uncertain' definition as the
    Uncertain mappings figure."""
    sql = """
        SELECT ap.slug AS slug, ap.name AS name, COUNT(a.id) AS n
        FROM associations a
        JOIN applications ap ON ap.id = a.app_id
        JOIN resources r ON r.id = a.resource_id
        WHERE a.removal_eligible = 'uncertain' AND a.excluded = 0
    """
    params: list[Any] = []
    if latest is not None:
        sql += " AND ap.last_seen = ? AND r.last_seen = ?"
        params.extend([latest, latest])
    sql += " GROUP BY ap.id ORDER BY n DESC, ap.name LIMIT ?"
    params.append(_ATTENTION_LIMIT)
    return [
        {
            "slug": r["slug"],
            "name": r["name"],
            "reason": f"{r['n']} uncertain resource mapping{'s' if r['n'] != 1 else ''}",
            "url": f"/apps/{r['slug']}",
        }
        for r in _rows(q(conn, sql, tuple(params)))
    ]


def _site_plan(apps: list[dict], aggregates: dict[int, dict]) -> dict[str, list[dict]]:
    """Lots grouped two ways (by kind, by status) for the site-plan switch."""
    lots = []
    for a in sorted(apps, key=lambda a: (a["name"] or a["slug"]).lower()):
        agg = aggregates.get(a["id"], {})
        status = a.get("status") or "unknown"
        lots.append({
            "slug": a["slug"],
            "name": a["name"] or a["slug"],
            "status": status if status in _STATUS_ORDER else "unknown",
            "kind": a.get("kind") or "other",
            "protected": bool(a.get("protected")),
            "res_count": agg.get("res_count", 0),
            "warn_count": agg.get("warn_count", 0),
        })

    def group(field: str, order: list[str]) -> list[dict]:
        buckets: dict[str, list[dict]] = {}
        for lot in lots:
            buckets.setdefault(lot[field], []).append(lot)
        names = [n for n in order if n in buckets] + sorted(n for n in buckets if n not in order)
        return [{"name": n, "lots": buckets[n]} for n in names]

    return {"kind": group("kind", _KIND_ORDER), "status": group("status", _STATUS_ORDER)}


def _changes(conn, latest: int | None) -> dict | None:
    """What the latest completed scan changed relative to the one before it.

    Apps: first seen now, or seen last time and gone now. Resources: first seen
    now (a resource that left and came back keeps its old first_seen, so it is
    not counted), or present last time and gone now. Status changes need both
    scans' app_status snapshot, which scans record from 2026-10-03 on."""
    if latest is None:
        return None
    prev_row = q(conn, "SELECT MAX(id) AS id FROM scans WHERE status = 'done' AND id < ?", (latest,))
    prev = prev_row[0]["id"] if prev_row else None
    if prev is None:
        return None
    new_apps = _rows(q(
        conn,
        "SELECT slug, name FROM applications WHERE first_seen = ? AND last_seen = ? ORDER BY name COLLATE NOCASE",
        (latest, latest),
    ))
    gone_apps = _rows(q(
        conn, "SELECT slug, name FROM applications WHERE last_seen = ? ORDER BY name COLLATE NOCASE", (prev,),
    ))
    added = {r["type"]: r["n"] for r in q(
        conn, "SELECT type, COUNT(*) AS n FROM resources WHERE first_seen = ? GROUP BY type", (latest,),
    )}
    removed = {r["type"]: r["n"] for r in q(
        conn, "SELECT type, COUNT(*) AS n FROM resources WHERE last_seen = ? GROUP BY type", (prev,),
    )}
    resource_rows = [
        {"type": t, "label": RESOURCE_TYPE_LABELS.get(t, t), "added": added.get(t, 0), "removed": removed.get(t, 0)}
        for t in sorted(set(added) | set(removed), key=lambda t: RESOURCE_TYPE_LABELS.get(t, t).lower())
    ]
    stats = {
        r["id"]: _json_or(r["stats_json"], {})
        for r in q(conn, "SELECT id, stats_json FROM scans WHERE id IN (?, ?)", (prev, latest))
    }
    before = stats.get(prev, {}).get("app_status")
    after = stats.get(latest, {}).get("app_status")
    status_changes = None
    if isinstance(before, dict) and isinstance(after, dict):
        names = {r["slug"]: r["name"] for r in q(
            conn, "SELECT slug, name FROM applications WHERE last_seen = ?", (latest,),
        )}
        status_changes = [
            {"slug": slug, "name": names.get(slug, slug), "before": before[slug], "after": status}
            for slug, status in sorted(after.items())
            if slug in before and before[slug] != status
        ]
    return {
        "prev": prev,
        "latest": latest,
        "new_apps": new_apps,
        "gone_apps": gone_apps,
        "resources": resource_rows,
        "status_changes": status_changes,
        "quiet": not (new_apps or gone_apps or resource_rows or status_changes),
    }


def _next_scan(conn, latest: int | None, interval_hours: float) -> dict | None:
    """When the scheduler will next rescan: the latest completed scan's finish
    time plus the interval (it checks every 10 minutes after that)."""
    if latest is None or interval_hours <= 0:
        return None
    row = q(conn, "SELECT finished FROM scans WHERE id = ?", (latest,))
    try:
        finished = datetime.strptime(str(row[0]["finished"]), "%Y-%m-%d %H:%M:%S")
    except (IndexError, TypeError, ValueError):
        return None
    at = finished + timedelta(hours=interval_hours)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {"at": at.strftime("%Y-%m-%d %H:%M:%S"), "due": at <= now}


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db(read_snapshot=True)
    try:
        latest = _latest_scan_id(conn)
        # Every figure links to a page; scope them all to the latest completed
        # scan so each one matches the page it opens.
        apps_sql = "SELECT id, slug, name, status, kind, protected FROM applications"
        apps_params: tuple = ()
        if latest is not None:
            apps_sql += " WHERE last_seen = ?"
            apps_params = (latest,)
        apps = _rows(q(conn, apps_sql, apps_params))
        aggregates = _app_aggregates(conn, [a["id"] for a in apps])
        running_jobs = q(conn, "SELECT COUNT(*) AS n FROM jobs WHERE status = 'running'")[0]["n"]
        # EXISTS rather than JOIN: the JOIN form turns plan-unstable once
        # sqlite_stat1 exists (skip-scan, ~10x slower).
        uncertain_sql = """
            SELECT COUNT(*) AS n FROM associations a
            WHERE a.removal_eligible = 'uncertain' AND a.excluded = 0
              AND EXISTS (SELECT 1 FROM resources r WHERE r.id = a.resource_id {r_scope})
              AND EXISTS (SELECT 1 FROM applications ap WHERE ap.id = a.app_id {a_scope})
        """
        scoped = latest is not None
        uncertain_count = q(conn, uncertain_sql.format(
            r_scope="AND r.last_seen = ?" if scoped else "",
            a_scope="AND ap.last_seen = ?" if scoped else "",
        ), (latest, latest) if scoped else ())[0]["n"]
        shared_sql = """
            SELECT r.type AS type, COUNT(DISTINCT a.resource_id) AS n FROM associations a
            JOIN resources r ON r.id = a.resource_id
            JOIN applications ap ON ap.id = a.app_id
            WHERE a.shared = 1 AND a.excluded = 0 {scope}
            GROUP BY r.type ORDER BY n DESC, r.type
        """
        shared_rows = _rows(q(
            conn,
            shared_sql.format(scope="AND r.last_seen = ? AND ap.last_seen = ?" if scoped else ""),
            (latest, latest) if scoped else (),
        ))
        orphan_actionable = _actionable_orphan_count(conn, latest)
        recent_jobs = _rows(q(conn, """
            SELECT j.id, j.mode, j.status, j.started, ap.slug AS app_slug, ap.name AS app_name
            FROM jobs j
            LEFT JOIN plans p ON p.id = j.plan_id
            LEFT JOIN applications ap ON ap.id = p.app_id
            ORDER BY j.id DESC LIMIT 6
        """))
        dir_bytes = _disk_usage_bytes(conn, latest)
        attention = _attention_apps(conn, latest)
        changes = _changes(conn, latest)
        next_scan = _next_scan(conn, latest, get_settings().scan_interval_hours)
    finally:
        conn.close()

    df = docker_df()
    site_plan = _site_plan(apps, aggregates)
    status_counts = {g["name"]: len(g["lots"]) for g in site_plan["status"]}
    stats = {
        "apps": len(apps),
        "running": running_jobs,
        "orphan_candidates": orphan_actionable,
        "shared_resources": sum(r["n"] for r in shared_rows),
        "shared_top_type": shared_rows[0]["type"] if shared_rows else "volume",
        "uncertain_mappings": uncertain_count,
        "directory_bytes": dir_bytes,
        "volume_bytes": df["volumes"],
        "disk_usage_bytes": None if df["volumes"] is None else dir_bytes + df["volumes"],
        "reclaimable_bytes": df["reclaimable"],
        "docker_df_state": df["state"],
    }
    return _render(
        "dashboard.html",
        request,
        response,
        stats=stats,
        site_plan=site_plan,
        status_counts=status_counts,
        changes=changes,
        recent_jobs=recent_jobs,
        attention=attention,
        scan_interval_hours=get_settings().scan_interval_hours,
        next_scan=next_scan,
        user=user,
    )
