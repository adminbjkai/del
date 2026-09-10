"""Read-only DB query helpers shared across the web UI: row normalization,
resource-type maps, scan/owner lookups. No routes, no rendering."""
from __future__ import annotations

import json
from typing import Any

from del_app.db import latest_done_scan_id, q

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


def _normalize_image_ref(ref: str | None) -> str:
    """Normalize an image reference for tag matching: append ':latest' when
    there is no explicit tag or digest, matching docker's own convention."""
    if not ref:
        return ""
    if "@" in ref:
        return ref
    tail = ref.rsplit("/", 1)[-1]
    if ":" in tail:
        return ref
    return f"{ref}:latest"


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


def _owner_map(conn, resource_ids: list[int]) -> dict[int, dict]:
    """resource_id -> {"apps": [{slug,name}], "shared": bool} for non-excluded
    associations. Read-only join over associations + applications."""
    out: dict[int, dict] = {}
    if not resource_ids:
        return out
    placeholders = ",".join("?" for _ in resource_ids)
    rows = _rows(
        q(
            conn,
            f"""
            SELECT a.resource_id AS rid, a.shared AS shared,
                   ap.slug AS slug, ap.name AS name
            FROM associations a
            JOIN applications ap ON ap.id = a.app_id
            WHERE a.excluded = 0 AND a.resource_id IN ({placeholders})
            """,
            tuple(resource_ids),
        )
    )
    for r in rows:
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
              AND a.confidence >= ?
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


