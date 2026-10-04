"""Applications: list, detail, rescan-approve, and the command-palette feed."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from del_app import auditlog, auth
from del_app.auth import User
from del_app.db import get_db, q
from del_app.web.formatting import _app_dates, _level
from del_app.web.queries import (
    RESOURCE_TYPE_LABELS,
    _app_aggregates,
    resource_type_label,
    _json_or,
    _latest_scan_id,
    _owner_map,
    _rows,
    _scan_started_map,
)
from del_app.web.render import _render, _require_csrf, _csrf_response

router = APIRouter()

# Maps each association's resource_type to the app-detail tab that lists it,
# so "Resources by type" on Overview can link straight to the right tab
# (mirrors the section_defs grouping in app_detail.html).
_TYPE_TO_SECTION = {
    "container": "docker", "image": "docker", "volume": "docker",
    "network": "docker", "compose_project": "docker",
    "systemd_unit": "systemd", "systemd_timer": "systemd",
    "nginx_site": "nginx",
    "cron_entry": "scheduled",
    "process": "processes", "port": "processes", "tmux_session": "processes",
    "directory": "files", "git_repo": "files", "bind_mount": "files", "env_file": "files",
}

# Overview "wiring": the app's own resources in four lanes, from how traffic
# reaches it to where it keeps data. Excluded rows are not the app's.
_WIRING_LANE_MAX = 6
_RUNNING_STATES = {"running", "active", "listen"}
_IDLE_STATES = {"exited", "created", "inactive", "dead", "stopped", "paused", "not listening"}


def _wiring_item(a: dict, label: str | None = None, state: str | None = None) -> dict:
    rt = a.get("resource_type") or ""
    state = state if state is not None else (a.get("resource_state") or "")
    tone = "ok" if state in _RUNNING_STATES else ("idle" if state in _IDLE_STATES else ("danger" if state == "failed" else ""))
    return {
        "label": label or a.get("resource_display") or a.get("resource_key") or "?",
        "kind": resource_type_label(rt, singular=True),
        "state": state,
        "tone": tone,
        "shared": bool(a.get("shared")),
        "data": a.get("data_loss_risk") == "data",
        "section": _TYPE_TO_SECTION.get(rt),
        "title": a.get("resource_key") or "",
    }


def _wiring(assoc_rows: list[dict], domains: list[str]) -> list[dict]:
    own = [a for a in assoc_rows if not a.get("excluded")]

    def of(*types: str) -> list[dict]:
        return [a for a in own if a.get("resource_type") in types]

    entry = [{"label": d, "kind": "Domain", "href": f"https://{d}", "tone": "", "section": "nginx"} for d in domains]
    listening = [_wiring_item(a) for a in of("port")]
    for a in of("container"):
        running = (a.get("resource_data") or {}).get("state") == "running"
        for pm in a.get("port_mappings") or []:
            if pm.get("host"):
                listening.append(_wiring_item(
                    a, label=f"{pm.get('host')} → {a.get('resource_display')}",
                    state="listen" if running else "not listening",
                ))
    runtime = [
        _wiring_item(a, state=(a.get("resource_data") or {}).get("state") or a.get("resource_state"))
        for a in of("container")
    ] + [_wiring_item(a) for a in of("systemd_unit", "systemd_timer", "compose_project", "cron_entry", "tmux_session")]
    processes = of("process")
    if processes:
        runtime.append({"label": f"{len(processes)} process{'es' if len(processes) != 1 else ''}",
                        "kind": "Processes", "tone": "ok", "section": "processes"})
    storage_order = ("volume", "directory", "bind_mount", "git_repo", "env_file")
    data = [
        (storage_order.index(a["resource_type"]), _wiring_item(a))
        for a in of(*storage_order)
    ]
    # Data-bearing items first (what a removal would destroy), volumes and
    # directories before the individual mounts that point into them.
    data.sort(key=lambda p: (not p[1]["data"], p[0], p[1]["label"]))
    data = [item for _, item in data]
    lanes = [
        {"key": "entry", "title": "Reached at", "items": entry, "empty": "No enabled site"},
        {"key": "listening", "title": "Listens on", "items": listening, "empty": "No listening port"},
        {"key": "runtime", "title": "Runs as", "items": runtime, "empty": "Nothing running or declared"},
        {"key": "data", "title": "Keeps data in", "items": data, "empty": "No storage found"},
    ]
    for lane in lanes:
        lane["more"] = max(0, len(lane["items"]) - _WIRING_LANE_MAX)
        lane["more_section"] = next((i.get("section") for i in lane["items"][_WIRING_LANE_MAX:] if i.get("section")), None)
        lane["items"] = lane["items"][:_WIRING_LANE_MAX]
    return lanes


_HISTORY_SCANS = 48


def _status_history(conn, slug: str) -> dict | None:
    """This app's status in each recent completed scan, oldest first, and
    since when the current status holds. Only scans that recorded per-app
    status (2026-10-03 on) count; an app missing from one was not in that
    scan's inventory."""
    rows = _rows(q(
        conn,
        "SELECT id, started, stats_json FROM scans WHERE status = 'done' "
        "AND stats_json LIKE '%\"app_status\"%' ORDER BY id DESC LIMIT ?",
        (_HISTORY_SCANS,),
    ))
    out = []
    for r in reversed(rows):
        status_map = _json_or(r.get("stats_json"), {}).get("app_status")
        if not isinstance(status_map, dict):
            continue
        out.append({"scan": r["id"], "started": r.get("started"), "status": status_map.get(slug) or "missing"})
    if not out:
        return None
    current = out[-1]["status"]
    since = out[-1]
    for tick in reversed(out):
        if tick["status"] != current:
            break
        since = tick
    return {
        "ticks": out,
        "current": current,
        # None when the status never changed inside the recorded window.
        "since": since if since is not out[0] else None,
        "first": out[0],
    }


def _dates_from(assoc: dict) -> bool:
    """Whether an association's resource dates count for its app.

    Shared resources never do. Excluded ones only when a manifest declared
    them (the operator's "this is the app's, but never remove it", e.g. the
    notes source clone); correlator exclusions such as DEL backups are not
    the app's own history.
    """
    if assoc.get("shared"):
        return False
    if not assoc.get("excluded"):
        return True
    evidence = _json_or(assoc.get("evidence_json"), [])
    return bool(evidence) and all(
        isinstance(e, dict) and e.get("source") == "manifest" for e in evidence
    )

@router.get("/apps", response_class=HTMLResponse)
def apps_list(
    request: Request,
    response: Response,
    user: User = Depends(auth.require_user),
    search: str = "",
    status: str = "",
) -> HTMLResponse:
    show_removed = request.query_params.get("show") == "removed"
    conn = get_db(read_snapshot=True)
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
        sql += " ORDER BY name COLLATE NOCASE"
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
        agg_map = _app_aggregates(conn, app_ids)
        if app_ids:
            id_ph = ",".join("?" for _ in app_ids)
            detail_sql = f"""
                SELECT a.app_id AS app_id, r.type AS type, r.data_json AS data_json,
                       a.shared AS shared, a.excluded AS excluded, a.evidence_json AS evidence_json
                FROM associations a
                JOIN resources r ON r.id = a.resource_id
                WHERE a.app_id IN ({id_ph})
                  AND r.type IN ('nginx_site', 'port', 'container', 'directory', 'systemd_unit')
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
            detail = []
    finally:
        conn.close()

    domains: dict[int, set] = {}
    ports: dict[int, set] = {}        # something listens on it now
    idle_ports: dict[int, set] = {}   # published by a container that is not running
    date_rows: dict[int, list[dict]] = {}
    for d in detail:
        data = _json_or(d.get("data_json"), {})
        aid = d["app_id"]
        if _dates_from(d):
            date_rows.setdefault(aid, []).append({"type": d["type"], "data": data})
        if d.get("excluded"):
            continue
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
            running = data.get("state") == "running"
            for p in data.get("published_ports", []) or []:
                (ports if running else idle_ports).setdefault(aid, set()).add(str(p))

    for app in apps:
        aid = app.get("id")
        a = agg_map.get(aid, {})
        app["res_count"] = a.get("res_count") or 0
        app["warn_count"] = a.get("warn_count") or 0
        app["domains"] = sorted(domains.get(aid, set()))
        live = ports.get(aid, set())
        app["ports"] = sorted(live, key=lambda x: (len(x), x))
        app["idle_ports"] = sorted(idle_ports.get(aid, set()) - live, key=lambda x: (len(x), x))

        first_seen_id = app.get("first_seen")
        last_seen_id = app.get("last_seen")
        first_seen_at = scan_times.get(int(first_seen_id)) if first_seen_id is not None else None
        last_seen_at = scan_times.get(int(last_seen_id)) if last_seen_id is not None else None
        # Capped only by when DEL first saw the app itself: resource rows keyed
        # by port number or image id are reused across apps and predate them.
        app.update(_app_dates(date_rows.get(aid, []), first_seen_at))
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
    conn = get_db(read_snapshot=True)
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
        # Every other app (if any) that also holds a non-excluded association
        # to one of this app's resources — used for the shared-resource
        # callout's "other apps" links and the Overview "Related apps" list.
        resource_ids = [a["resource_id"] for a in assoc_rows if a.get("resource_id") is not None]
        owner_map = _owner_map(conn, resource_ids, latest=assoc_scan)
        history = _status_history(conn, slug)
    finally:
        conn.close()

    for a in assoc_rows:
        a["evidence"] = _json_or(a.get("evidence_json"), [])
        a["level"] = _level(a.get("confidence"), a.get("source"))
        a["resource_data"] = _json_or(a.get("resource_data_json"), {})
        a["port_mappings"] = a["resource_data"].get("port_mappings") if a.get("resource_type") == "container" else None
        entry = owner_map.get(a.get("resource_id")) or {"apps": []}
        a["co_owners"] = [o for o in entry["apps"] if o["slug"] != slug]

    related_apps_map: dict[str, str] = {}
    type_counts: dict[str, int] = {}
    for a in assoc_rows:
        for o in a["co_owners"]:
            related_apps_map.setdefault(o["slug"], o["name"])
        rt = a.get("resource_type")
        if rt:
            type_counts[rt] = type_counts.get(rt, 0) + 1
    related_apps = sorted(
        ({"slug": s, "name": n} for s, n in related_apps_map.items()),
        key=lambda x: x["name"],
    )
    resources_by_type = sorted(
        (
            {
                "type": rt,
                "label": RESOURCE_TYPE_LABELS.get(rt, rt),
                "count": cnt,
                "section": _TYPE_TO_SECTION.get(rt),
            }
            for rt, cnt in type_counts.items()
        ),
        key=lambda r: r["label"],
    )

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

    # Human dates: resolve scan IDs -> scan.started; installed / last changed
    # from the app's own (non-excluded, non-shared) resources.
    first_seen_id = app.get("first_seen")
    last_seen_id = app.get("last_seen")
    app["first_seen_at"] = (
        scan_times.get(int(first_seen_id)) if first_seen_id is not None else None
    )
    app["last_seen_at"] = (
        scan_times.get(int(last_seen_id)) if last_seen_id is not None else None
    )
    # Same count as the Applications list: excluded associations are shown
    # for review but are not the app's resources.
    app["res_count"] = sum(1 for a in assoc_rows if not a.get("excluded"))
    app["excluded_count"] = len(assoc_rows) - app["res_count"]
    own = [a for a in assoc_rows if _dates_from(a)]
    app.update(_app_dates(
        [{"type": a.get("resource_type"), "data": a["resource_data"]} for a in own],
        app["first_seen_at"],
    ))

    return _render(
        "app_detail.html",
        request,
        response,
        app=app,
        associations=assoc_rows,
        sections=sections,
        related_apps=related_apps,
        resources_by_type=resources_by_type,
        wiring=_wiring(assoc_rows, app["domains"]),
        history=history,
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
        "approve": "approved_by_user = 1, excluded = 0, user_excluded = 0",
        "exclude": "excluded = 1, user_excluded = 1",
        "mark-shared": "shared = 1, user_shared = 1",
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

    from del_app.web.orphans import invalidate_orphan_count

    invalidate_orphan_count()
    auditlog.audit(user.id, f"association.{action}", f"{slug}#{association_id}", {})
    labels = {"approve": "approved", "exclude": "excluded", "mark-shared": "marked+shared"}
    return RedirectResponse(
        url=f"/apps/{slug}?flash=Association+{labels[action]}", status_code=303
    )


@router.get("/palette.json")
def palette_json(user: User = Depends(auth.require_user)) -> JSONResponse:
    """Command-palette feed: apps in the latest completed scan with their
    enabled domains. Pages come from the sidebar already in the DOM."""
    conn = get_db(read_snapshot=True)
    try:
        latest = _latest_scan_id(conn)
        apps_sql = "SELECT id, slug, name, status FROM applications"
        params: tuple = ()
        if latest is not None:
            apps_sql += " WHERE last_seen = ?"
            params = (latest,)
        apps_sql += " ORDER BY name COLLATE NOCASE"
        app_rows = _rows(q(conn, apps_sql, params))
        slug_to_id = {a["slug"]: a["id"] for a in app_rows}
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
    return JSONResponse({"apps": apps})
