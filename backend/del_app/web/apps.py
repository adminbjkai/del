"""Applications: list, detail, rescan-approve, and the command-palette feed."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from del_app import auditlog, auth
from del_app.auth import User
from del_app.db import get_db, q
from del_app.web.formatting import _installed_at_from_resources, _level
from del_app.web.queries import _json_or, _latest_scan_id, _rows, _scan_started_map
from del_app.web.render import _render, _require_csrf, _csrf_response

router = APIRouter()

# Sidebar nav entries surfaced by the command palette. Kept in one place so
# a page rename here does not silently fall out of sync with the sidebar.
_PALETTE_PAGES = [
    {"title": "Dashboard", "url": "/"},
    {"title": "Applications", "url": "/apps"},
    {"title": "App Gallery", "url": "/view-apps"},
    {"title": "Resources", "url": "/resources"},
    {"title": "Orphans", "url": "/orphans"},
    {"title": "Assistant", "url": "/assistant"},
    {"title": "Jobs", "url": "/jobs"},
    {"title": "Settings", "url": "/settings"},
]


@router.get("/apps", response_class=HTMLResponse)
def apps_list(
    request: Request,
    response: Response,
    user: User = Depends(auth.require_user),
    search: str = "",
    status: str = "",
) -> HTMLResponse:
    show_removed = request.query_params.get("show") == "removed"
    conn = get_db()
    try:
        latest = _latest_scan_id(conn)
        scan_times = _scan_started_map(conn)
        sql = "SELECT * FROM applications WHERE 1=1"
        params: list[Any] = []
        if not show_removed and latest is not None:
            sql += " AND last_seen = ?"
            params.append(int(latest))
        if search:
            sql += " AND (name LIKE ? OR slug LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like])
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY name"
        apps = _rows(q(conn, sql, tuple(params)))

        # Per-app aggregates: resource count, warning count (possible / low
        # confidence associations), plus domains & ports from associated
        # resource data_json. Read-only.
        #
        # Both queries are scoped to the app ids actually being rendered. They
        # used to run across every application and association ever recorded,
        # so listing ~190 apps re-read and json.loads-ed the resource payloads
        # of a further ~136 applications that no longer exist — and a `search=`
        # filter narrowing the page to three rows did not narrow this at all.
        app_ids = [a["id"] for a in apps if a.get("id") is not None]
        if app_ids:
            id_ph = ",".join("?" for _ in app_ids)
            agg = _rows(
                q(
                    conn,
                    f"""
                    SELECT ap.id AS app_id,
                           COUNT(a.id) AS res_count,
                           SUM(CASE WHEN a.ownership = 'possible' OR a.confidence < 50
                                    THEN 1 ELSE 0 END) AS warn_count
                    FROM applications ap
                    LEFT JOIN associations a
                           ON a.app_id = ap.id AND a.excluded = 0
                    WHERE ap.id IN ({id_ph})
                    GROUP BY ap.id
                    """,
                    tuple(app_ids),
                )
            )
            detail_sql = f"""
                SELECT a.app_id AS app_id, r.type AS type, r.data_json AS data_json
                FROM associations a
                JOIN resources r ON r.id = a.resource_id
                WHERE a.excluded = 0
                  AND a.app_id IN ({id_ph})
                  AND r.type IN ('nginx_site', 'port', 'container', 'directory')
            """
            detail_params: list[Any] = list(app_ids)
            # Not scoped in ?show=removed: a removed app's resources carry a
            # stale last_seen too, and filtering them out would blank the
            # domains/ports on exactly the rows that view exists to show.
            if latest is not None and not show_removed:
                detail_sql += " AND r.last_seen = ?"
                detail_params.append(int(latest))
            detail = _rows(q(conn, detail_sql, tuple(detail_params)))
        else:
            agg, detail = [], []
        agg_map = {r["app_id"]: r for r in agg}
    finally:
        conn.close()

    domains: dict[int, set] = {}
    ports: dict[int, set] = {}
    install_signals: dict[int, list[dict]] = {}
    for d in detail:
        data = _json_or(d.get("data_json"), {})
        aid = d["app_id"]
        if d["type"] == "nginx_site":
            # Only enabled sites contribute domains: non-enabled/stale
            # sites-available copies must never leak their server_names.
            if not data.get("enabled", False):
                continue
            for sn in data.get("server_names", []) or []:
                domains.setdefault(aid, set()).add(sn)
        elif d["type"] == "port":
            p = data.get("port")
            if p is not None:
                ports.setdefault(aid, set()).add(str(p))
        elif d["type"] == "container":
            for p in data.get("published_ports", []) or []:
                ports.setdefault(aid, set()).add(str(p))
            install_signals.setdefault(aid, []).append(
                {"type": "container", "data_json": d.get("data_json")}
            )
        elif d["type"] == "directory":
            install_signals.setdefault(aid, []).append(
                {"type": "directory", "data_json": d.get("data_json")}
            )

    for app in apps:
        aid = app.get("id")
        a = agg_map.get(aid, {})
        app["res_count"] = a.get("res_count") or 0
        app["warn_count"] = a.get("warn_count") or 0
        app["domains"] = sorted(domains.get(aid, set()))
        app["ports"] = sorted(ports.get(aid, set()), key=lambda x: (len(x), x))

        first_seen_id = app.get("first_seen")
        last_seen_id = app.get("last_seen")
        first_seen_at = scan_times.get(int(first_seen_id)) if first_seen_id is not None else None
        last_seen_at = scan_times.get(int(last_seen_id)) if last_seen_id is not None else None
        installed = _installed_at_from_resources(install_signals.get(aid, []))
        # Prefer host signals; fall back to when DEL first discovered the app.
        app["installed_at"] = installed or first_seen_at
        app["first_seen_at"] = first_seen_at
        app["last_seen_at"] = last_seen_at
        app["is_removed"] = bool(
            latest is not None
            and last_seen_id is not None
            and int(last_seen_id) < int(latest)
        )

    return _render(
        "apps.html",
        request,
        response,
        apps=apps,
        search=search,
        status=status,
        show_removed=show_removed,
        latest_scan=latest,
    )


@router.get("/apps/{slug}", response_class=HTMLResponse)
def app_detail(
    slug: str, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        rows = q(conn, "SELECT * FROM applications WHERE slug = ?", (slug,))
        found = _rows(rows)
        if not found:
            raise HTTPException(status_code=404, detail=f"no such application: {slug}")
        app = found[0]
        latest_scan = _latest_scan_id(conn)
        scan_times = _scan_started_map(conn)
        # Only show associations to resources still present as of the latest
        # scan; otherwise a resource removed in an earlier scan (stale
        # last_seen) would keep showing up here forever.
        # For a removed app (last_seen < latest), fall back to that app's own
        # last_seen scan so history pages still list its final resources.
        assoc_scan = latest_scan
        if (
            app.get("last_seen") is not None
            and latest_scan is not None
            and int(app["last_seen"]) < int(latest_scan)
        ):
            assoc_scan = int(app["last_seen"])
        assoc_sql = """
                SELECT a.*, r.type as resource_type, r.key as resource_key,
                       r.display as resource_display, r.path as resource_path,
                       r.state as resource_state, r.data_json as resource_data_json
                FROM associations a
                JOIN resources r ON r.id = a.resource_id
                WHERE a.app_id = (SELECT id FROM applications WHERE slug = ?)
                """
        assoc_params: tuple = (slug,)
        if assoc_scan is not None:
            assoc_sql += " AND r.last_seen = ?"
            assoc_params = (slug, assoc_scan)
        assoc_rows = _rows(q(conn, assoc_sql, assoc_params))
    finally:
        conn.close()

    for a in assoc_rows:
        a["evidence"] = _json_or(a.get("evidence_json"), [])
        a["level"] = _level(a.get("confidence"), a.get("source"))
        a["resource_data"] = _json_or(a.get("resource_data_json"), {})
        a["port_mappings"] = a["resource_data"].get("port_mappings") if a.get("resource_type") == "container" else None

    sections = {
        "docker": [a for a in assoc_rows if a.get("resource_type") in ("container", "image", "volume", "network", "compose_project")],
        "systemd": [a for a in assoc_rows if a.get("resource_type") in ("systemd_unit", "systemd_timer")],
        "nginx": [a for a in assoc_rows if a.get("resource_type") == "nginx_site"],
        "scheduled": [a for a in assoc_rows if a.get("resource_type") == "cron_entry"],
        "processes": [a for a in assoc_rows if a.get("resource_type") in ("process", "port", "tmux_session")],
        "files": [a for a in assoc_rows if a.get("resource_type") in ("directory", "git_repo", "bind_mount", "env_file")],
        "shared": [a for a in assoc_rows if a.get("shared")],
    }

    # Domains: only from enabled nginx sites (not excluded), never from
    # stale/non-enabled sites-available copies.
    domains: set = set()
    for a in sections["nginx"]:
        if a.get("excluded"):
            continue
        data = a.get("resource_data") or {}
        if data.get("enabled"):
            domains.update(data.get("server_names") or [])
    app["domains"] = sorted(domains)

    # Human dates: resolve scan IDs → scan.started; install time from resources.
    first_seen_id = app.get("first_seen")
    last_seen_id = app.get("last_seen")
    app["first_seen_at"] = (
        scan_times.get(int(first_seen_id)) if first_seen_id is not None else None
    )
    app["last_seen_at"] = (
        scan_times.get(int(last_seen_id)) if last_seen_id is not None else None
    )
    install_rows = [
        {
            "type": a.get("resource_type"),
            "data_json": a.get("resource_data_json"),
        }
        for a in assoc_rows
        if a.get("resource_type") in ("container", "directory")
    ]
    app["installed_at"] = (
        _installed_at_from_resources(install_rows) or app.get("first_seen_at")
    )

    return _render(
        "app_detail.html",
        request,
        response,
        app=app,
        associations=assoc_rows,
        sections=sections,
        removed=bool(latest_scan and app.get("last_seen") is not None and app["last_seen"] < latest_scan),
    )


@router.post("/apps/{slug}/rescan-approve")
def rescan_approve(
    slug: str,
    request: Request,
    user: User = Depends(auth.require_user),
    csrf_token: str = Form(""),
    association_id: int = Form(...),
    action: str = Form(...),
) -> Response:
    """Toggle approve / exclude / mark-shared for one app<->resource
    association."""
    if not _require_csrf(request, csrf_token):
        return _csrf_response()

    # An unrecognised action used to fall through silently: nothing was
    # updated, yet an audit record was written and the UI flashed success.
    setters = {
        "approve": "approved_by_user = 1, excluded = 0",
        "exclude": "excluded = 1",
        "mark-shared": "shared = 1",
    }
    if action not in setters:
        return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)

    conn = get_db()
    try:
        cur = conn.execute(
            f"UPDATE associations SET {setters[action]} "
            "WHERE id = ? AND app_id = (SELECT id FROM applications WHERE slug = ?)",
            (association_id, slug),
        )
        changed = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    # rowcount 0 means the association does not exist or belongs to a different
    # app. Say so rather than claiming a change that did not happen.
    if not changed:
        return RedirectResponse(
            url=f"/apps/{slug}?error=Association+not+found+for+this+application",
            status_code=303,
        )

    auditlog.audit(user.id, f"association.{action}", f"{slug}#{association_id}", {})
    labels = {"approve": "approved", "exclude": "excluded", "mark-shared": "marked+shared"}
    return RedirectResponse(
        url=f"/apps/{slug}?flash=Association+{labels[action]}", status_code=303
    )


@router.get("/palette.json")
def palette_json(user: User = Depends(auth.require_user)) -> JSONResponse:
    """Command-palette data feed: known apps (from the latest completed scan)
    plus the sidebar's static page list."""
    conn = get_db()
    try:
        latest = _latest_scan_id(conn)
        apps_sql = "SELECT slug, name, status FROM applications"
        params: tuple = ()
        if latest is not None:
            apps_sql += " WHERE last_seen = ?"
            params = (latest,)
        apps_sql += " ORDER BY name"
        app_rows = _rows(q(conn, apps_sql, params))
        app_ids = []
        slug_to_id = {}
        if app_rows:
            id_rows = _rows(q(
                conn,
                "SELECT id, slug FROM applications WHERE slug IN ({})".format(
                    ",".join("?" for _ in app_rows)
                ),
                tuple(a["slug"] for a in app_rows),
            ))
            for r in id_rows:
                slug_to_id[r["slug"]] = r["id"]
            app_ids = list(slug_to_id.values())

        domains_by_app: dict[int, set] = {}
        if app_ids and latest is not None:
            id_ph = ",".join("?" for _ in app_ids)
            site_rows = _rows(q(
                conn,
                f"""
                SELECT a.app_id AS app_id, r.data_json AS data_json
                FROM associations a
                JOIN resources r ON r.id = a.resource_id
                WHERE a.excluded = 0 AND a.app_id IN ({id_ph})
                  AND r.type = 'nginx_site' AND r.last_seen = ?
                """,
                tuple(app_ids) + (latest,),
            ))
            for r in site_rows:
                data = _json_or(r.get("data_json"), {})
                if not data.get("enabled", False):
                    continue
                for sn in data.get("server_names", []) or []:
                    domains_by_app.setdefault(r["app_id"], set()).add(sn)
    finally:
        conn.close()

    apps = [
        {
            "slug": a["slug"],
            "name": a["name"],
            "status": a.get("status"),
            "domains": sorted(domains_by_app.get(slug_to_id.get(a["slug"]), set())),
            "url": f"/apps/{a['slug']}",
        }
        for a in app_rows
    ]
    return JSONResponse({"apps": apps, "pages": _PALETTE_PAGES})
