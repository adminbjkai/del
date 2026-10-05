"""Read-only DB query helpers shared across the web UI: row normalization,
resource-type maps, scan/owner lookups. No routes, no rendering."""
from __future__ import annotations

import json
from typing import Any

from del_app.db import latest_done_scan_id, q
from del_app.discovery.docker_src import _normalize_image_ref

# DB resource types are SINGULAR. Ordered for the Resources tab bar.
ALL_RESOURCE_TYPES = [
    "container", "image", "volume", "network", "compose_project",
    "nginx_site", "systemd_unit", "systemd_timer", "cron_entry",
    "process", "port", "directory", "git_repo", "env_file",
    "bind_mount", "tmux_session",
]

RESOURCE_TYPE_LABELS = {
    "container": "Containers", "image": "Images", "volume": "Volumes",
    "network": "Networks", "compose_project": "Compose projects",
    "nginx_site": "Nginx sites", "systemd_unit": "systemd units",
    "systemd_timer": "systemd timers", "cron_entry": "Cron entries",
    "process": "Processes", "port": "Ports", "directory": "Directories",
    "git_repo": "Git repos", "env_file": "Env files",
    "bind_mount": "Bind mounts", "tmux_session": "tmux sessions",
}

RESOURCE_TYPE_SINGULAR = {
    "container": "Container", "image": "Image", "volume": "Volume",
    "network": "Network", "compose_project": "Compose project",
    "nginx_site": "Nginx site", "systemd_unit": "systemd unit",
    "systemd_timer": "systemd timer", "cron_entry": "Cron entry",
    "process": "Process", "port": "Port", "directory": "Directory",
    "git_repo": "Git repo", "env_file": "Env file",
    "bind_mount": "Bind mount", "tmux_session": "tmux session",
}


def resource_type_label(res_type: str | None, singular: bool = False) -> str:
    """Human label for a resource type ("Containers" / "Container")."""
    if not res_type:
        return "—"
    table = RESOURCE_TYPE_SINGULAR if singular else RESOURCE_TYPE_LABELS
    return table.get(res_type, res_type)


# Accept legacy plural URLs (e.g. /resources/containers) -> singular DB type.
RESOURCE_TYPE_MAP = {t + "s": t for t in ALL_RESOURCE_TYPES}
# a couple of irregular/legacy aliases pointing at the same singular
RESOURCE_TYPE_MAP.update({
    "cron_entries": "cron_entry",
    "git_repos": "git_repo",
    "directories": "directory",
})


def _normalize_type(res_type: str) -> str:
    """Map any accepted URL form (singular or plural) to the singular DB type."""
    if res_type in ALL_RESOURCE_TYPES:
        return res_type
    return RESOURCE_TYPE_MAP.get(res_type, res_type)


def _rows(rows: list[Any]) -> list[dict]:
    """Normalize a list of sqlite3.Row/dict/pydantic-model rows to dicts."""
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
        elif hasattr(r, "keys"):
            out.append(dict(r))
        elif hasattr(r, "model_dump"):
            out.append(r.model_dump())
        else:
            out.append(r)
    return out


def _json_or(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def enabled_server_names(data: Any) -> list[str]:
    """Server names of an enabled nginx site.

    Disabled and stale sites-available copies contribute nothing. This is the
    one rule the apps list, the dashboard site plan and the command palette
    all use, so a non-enabled name cannot leak into a filter or a launcher.
    """
    if not isinstance(data, dict) or not data.get("enabled", False):
        return []
    names = data.get("server_names") or []
    return [sn for sn in names if isinstance(sn, str) and sn]


def enabled_domains_by_app(conn, app_ids: list[int], latest: int | None) -> dict[int, list[str]]:
    """Enabled-site domains for these apps in one scan. Excluded associations
    are not the app's. No completed scan means no domains (nothing to scope to)."""
    if not app_ids or latest is None:
        return {}
    id_ph = ",".join("?" for _ in app_ids)
    rows = _rows(q(
        conn,
        f"""
        SELECT a.app_id AS app_id, r.data_json AS data_json
        FROM associations a
        JOIN resources r ON r.id = a.resource_id
        WHERE a.excluded = 0 AND a.app_id IN ({id_ph})
          AND r.type = 'nginx_site' AND r.last_seen = ?
        """,
        tuple(app_ids) + (int(latest),),
    ))
    found: dict[int, set[str]] = {}
    for row in rows:
        aid = row.get("app_id")
        if aid is None:
            continue
        for name in enabled_server_names(_json_or(row.get("data_json"), {})):
            found.setdefault(int(aid), set()).add(name)
    return {aid: sorted(names) for aid, names in found.items()}


def _latest_scan_id(conn) -> int | None:
    """Latest *completed* scan id. Thin wrapper over db.latest_done_scan_id so
    tests can monkeypatch it on this module; both call sites share one
    definition of "latest scan" (see db.latest_done_scan_id for why the
    status filter matters)."""
    return latest_done_scan_id(conn)


def _scan_started_map(conn) -> dict[int, str]:
    """scan_id -> started timestamp (sqlite datetime string)."""
    rows = _rows(q(conn, "SELECT id, started FROM scans"))
    out: dict[int, str] = {}
    for r in rows:
        sid = r.get("id")
        started = r.get("started")
        if sid is not None and started:
            out[int(sid)] = str(started)
    return out


def _compose_declared_images(conn) -> dict[str, str]:
    """Normalized image ref -> compose project display name, from every
    discovered compose_project resource's declared `image:` entries (latest
    scan), so an unreferenced-by-running-container image that a compose file
    still declares can be told apart from a truly unreferenced one."""
    latest = _latest_scan_id(conn)
    sql = "SELECT display, data_json FROM resources WHERE type = 'compose_project'"
    params: tuple = ()
    if latest is not None:
        sql += " AND last_seen = ?"
        params = (latest,)
    rows = _rows(q(conn, sql, params))
    mapping: dict[str, str] = {}
    for r in rows:
        data = _json_or(r.get("data_json"), {})
        for img in data.get("images", []) or []:
            mapping.setdefault(_normalize_image_ref(img), r["display"])
    return mapping


def _disk_usage_bytes(conn, latest_scan: int | None) -> int:
    """Single-pass sum of on-disk bytes for the latest scan's directory and
    volume resources (directory: data_json.size_kb * 1024; volume: whatever
    size field is present, if any). Read-only."""
    if latest_scan is None:
        return 0
    rows = _rows(
        q(
            conn,
            "SELECT type, data_json FROM resources WHERE last_seen = ? "
            "AND type IN ('directory', 'volume')",
            (latest_scan,),
        )
    )
    total = 0
    for r in rows:
        data = _json_or(r.get("data_json"), {})
        if r["type"] == "directory":
            size_kb = data.get("size_kb")
            if isinstance(size_kb, (int, float)):
                total += int(size_kb) * 1024
        elif r["type"] == "volume":
            size_bytes = data.get("size_bytes")
            if isinstance(size_bytes, (int, float)):
                total += int(size_bytes)
    return total


def _app_aggregates(conn, app_ids: list[int]) -> dict[int, dict]:
    """app id -> {res_count, warn_count} over non-excluded associations.
    Warnings are weak claims: ownership 'possible' or confidence below 60."""
    if not app_ids:
        return {}
    out: dict[int, dict] = {}
    for start in range(0, len(app_ids), 400):
        batch = app_ids[start:start + 400]
        id_ph = ",".join("?" for _ in batch)
        for r in q(
            conn,
            f"""
            SELECT ap.id AS app_id,
                   COUNT(a.id) AS res_count,
                   SUM(CASE WHEN a.ownership = 'possible' OR a.confidence < 60
                            THEN 1 ELSE 0 END) AS warn_count
            FROM applications ap
            LEFT JOIN associations a ON a.app_id = ap.id AND a.excluded = 0
            WHERE ap.id IN ({id_ph})
            GROUP BY ap.id
            """,
            tuple(batch),
        ):
            out[r["app_id"]] = {"res_count": r["res_count"] or 0, "warn_count": r["warn_count"] or 0}
    return out


def _type_counts(conn, latest_scan: int | None) -> list[dict]:
    """Per-type resource counts for resources seen in the latest scan."""
    counts: dict[str, int] = {}
    if latest_scan is not None:
        rows = _rows(
            q(
                conn,
                "SELECT type, COUNT(*) AS n FROM resources WHERE last_seen = ? GROUP BY type",
                (latest_scan,),
            )
        )
        counts = {r["type"]: r["n"] for r in rows}
    return [
        {"type": t, "label": RESOURCE_TYPE_LABELS.get(t, t), "count": counts.get(t, 0)}
        for t in ALL_RESOURCE_TYPES
    ]


def _owner_map(conn, resource_ids: list[int], *, latest: int | None = None) -> dict[int, dict]:
    """Non-excluded owners present in the requested completed scan.

    Bound each query below SQLite's legacy 999-parameter limit. A resource
    page or assistant context can contain thousands of process/file rows.
    """
    out: dict[int, dict] = {}
    if not resource_ids:
        return out
    if latest is None:
        latest = _latest_scan_id(conn)
    unique_ids = list(dict.fromkeys(resource_ids))
    for offset in range(0, len(unique_ids), 400):
        chunk = unique_ids[offset:offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        sql = f"""
            SELECT a.resource_id AS rid, a.shared AS shared,
                   ap.slug AS slug, ap.name AS name
            FROM associations a
            JOIN applications ap ON ap.id = a.app_id
            WHERE a.excluded = 0 AND a.resource_id IN ({placeholders})
        """
        params = tuple(chunk)
        if latest is not None:
            sql += " AND ap.last_seen = ?"
            params += (latest,)
        for r in q(conn, sql, params):
            entry = out.setdefault(r["rid"], {"apps": [], "shared": False})
            if not any(a["slug"] == r["slug"] for a in entry["apps"]):
                entry["apps"].append({"slug": r["slug"], "name": r["name"]})
            if r["shared"]:
                entry["shared"] = True
    return out


# An association only counts as "this resource has an owner" when it points at
# an application that still exists in the latest scan AND carries at least
# `probable` confidence. Without the first condition, a resource left behind by
# an app that was removed scans ago stays permanently hidden from Orphans —
# which is precisely the leftover the page exists to surface. Without the
# second, a sub-60 name-similarity guess (Step 11's difflib fallback) is enough
# to hide a resource while still being too weak to make it removable anywhere:
# a dead zone where the resource is neither actionable nor cleanable.
# Protected applications are the deliberate exception: their weak/shared
# associations still mean "keep this in the protected tree", so those rows must
# not reappear as actionable orphans.
_ORPHAN_MIN_OWNING_CONFIDENCE = 60


def _orphan_query(latest: int | None) -> tuple[str, tuple]:
    """SQL + params for 'resources with no current, confident owner'."""
    sql = """
        SELECT r.* FROM resources r
        WHERE NOT EXISTS (
            SELECT 1 FROM associations a
            JOIN applications ap ON ap.id = a.app_id
            WHERE a.resource_id = r.id
              AND a.excluded = 0
              AND (a.confidence >= ? OR ap.protected = 1)
    """
    params: list[Any] = [_ORPHAN_MIN_OWNING_CONFIDENCE]
    if latest is not None:
        sql += " AND ap.last_seen = ?"
        params.append(latest)
    sql += " )"
    if latest is not None:
        sql += " AND r.last_seen = ?"
        params.append(latest)
    sql += " ORDER BY r.type, r.display"
    return sql, tuple(params)


