"""Scope-aware context builders for the assistant: read-only SQL over DEL's
inventory, rendered to a compact deterministic text block and cut to a
character budget (docs/ASSISTANT.md "Scopes, targets and context builders").

Query rules: everything is scoped with the latest completed scan exactly like
the UI; associations exclude `excluded = 1`; ownership uses the same
`_owner_map` helper as the Resources page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from del_app.assistant.errors import AssistantError
from del_app.db import q
from del_app.web.formatting import _level
from del_app.web.orphans import classify_orphan_candidate
from del_app.web.queries import (
    RESOURCE_TYPE_LABELS,
    _compose_declared_images,
    _disk_usage_bytes,
    _json_or,
    _latest_scan_id,
    _normalize_image_ref,
    _orphan_query,
    _owner_map,
    _rows,
    _type_counts,
)

SCOPES = ("general", "app", "orphans", "resource_type", "resource")
RESOURCE_TYPE_TARGETS = ("container", "image", "network", "volume")

# Resource `data` keys never sent to the model: anything secret-like by name
# (jobs._SECRET_RE style) plus environment blocks, whose values are secrets by
# definition. `env_var_names` (names only) is fine.
_SECRET_KEY_RE = re.compile(r"(?i)(password|passwd|token|secret|credential|api_?key|private_?key|auth)")
_NOISE_KEYS = frozenset({"env", "environment", "labels", "raw", "inspect"})
_MAX_VALUE_CHARS = 200
_MAX_EVIDENCE = 3
_TRUNCATION_NOTE = "[context truncated: {n} more lines omitted]"


@dataclass
class ContextBundle:
    scope: str
    target: str | None
    title: str
    facts: dict = field(default_factory=dict)
    text: str = ""
    truncated: bool = False


def render_budgeted(lines: list[str], budget: int) -> tuple[str, bool]:
    """Join lines, cutting on a line boundary at `budget` chars. When cut, the
    last line is the truncation note and the total still fits the budget."""
    text = "\n".join(lines)
    if len(text) <= budget:
        return text, False
    kept: list[str] = []
    used = 0
    # Reserve room for the note (N is at most the line count).
    reserve = len(_TRUNCATION_NOTE.format(n=len(lines))) + 1
    for line in lines:
        extra = len(line) + (1 if kept else 0)
        if used + extra + reserve > budget:
            break
        kept.append(line)
        used += extra
    omitted = len(lines) - len(kept)
    kept.append(_TRUNCATION_NOTE.format(n=omitted))
    return "\n".join(kept), True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clean_data(data: dict | None) -> dict:
    """Strip secret-like and noisy keys, shorten long values."""
    out: dict = {}
    for k, v in (data or {}).items():
        ks = str(k)
        if ks in _NOISE_KEYS or _SECRET_KEY_RE.search(ks):
            continue
        if v is None or v == "" or v == [] or v == {}:
            continue
        out[ks] = v
    return out


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        s = ", ".join(_fmt(v) for v in value)
    elif isinstance(value, dict):
        s = "; ".join(f"{k}={_fmt(v)}" for k, v in value.items())
    else:
        s = str(value)
    s = " ".join(s.split())
    if len(s) > _MAX_VALUE_CHARS:
        s = s[: _MAX_VALUE_CHARS - 1] + "…"
    return s


def _size_of(data: dict) -> int | None:
    for key in ("size_bytes", "size"):
        v = data.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    v = data.get("size_kb")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v) * 1024
    return None


def _human_bytes(n: int | None) -> str:
    if n is None:
        return "unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return str(n)


def _orphan_rows(conn, latest: int | None) -> list[dict]:
    """The orphan candidate list exactly as /orphans computes it (all buckets),
    each row carrying data/bucket/label/reason."""
    sql, params = _orphan_query(latest)
    rows = _rows(q(conn, sql, params))
    compose_images = _compose_declared_images(conn)
    for r in rows:
        r["data"] = _json_or(r.get("data_json"), {})
        cls = classify_orphan_candidate(
            r.get("type") or "", r.get("key") or "", r.get("display") or "",
            r.get("path"), r["data"], compose_images,
        )
        r["bucket"] = cls["bucket"]
        r["bucket_label"] = cls["label"]
        r["reason"] = cls["reason"]
    return rows


def _scan_line(conn, latest: int | None) -> str:
    if latest is None:
        return "scan: none completed yet"
    rows = _rows(q(conn, "SELECT id, started, finished FROM scans WHERE id = ?", (latest,)))
    if not rows:
        return f"scan: #{latest}"
    return f"scan: #{latest} finished {rows[0].get('finished') or rows[0].get('started')}"


def _apps_in_scan(conn, latest: int | None) -> list[dict]:
    sql = "SELECT * FROM applications"
    params: tuple = ()
    if latest is not None:
        sql += " WHERE last_seen = ?"
        params = (latest,)
    sql += " ORDER BY name"
    return _rows(q(conn, sql, params))


def _app_aggregates(conn, app_ids: list[int], latest: int | None) -> dict[int, dict]:
    """Per app: resource count, warning count, domains, ports — mirrors /apps."""
    out: dict[int, dict] = {
        aid: {"res_count": 0, "warn_count": 0, "domains": set(), "ports": set()}
        for aid in app_ids
    }
    if not app_ids:
        return out
    ph = ",".join("?" for _ in app_ids)
    for r in _rows(q(
        conn,
        f"""
        SELECT ap.id AS app_id, COUNT(a.id) AS res_count,
               SUM(CASE WHEN a.ownership = 'possible' OR a.confidence < 50 THEN 1 ELSE 0 END) AS warn_count
        FROM applications ap
        LEFT JOIN associations a ON a.app_id = ap.id AND a.excluded = 0
        WHERE ap.id IN ({ph}) GROUP BY ap.id
        """,
        tuple(app_ids),
    )):
        out[r["app_id"]]["res_count"] = r.get("res_count") or 0
        out[r["app_id"]]["warn_count"] = r.get("warn_count") or 0
    detail_sql = f"""
        SELECT a.app_id AS app_id, r.type AS type, r.data_json AS data_json
        FROM associations a JOIN resources r ON r.id = a.resource_id
        WHERE a.excluded = 0 AND a.app_id IN ({ph})
          AND r.type IN ('nginx_site', 'port', 'container')
    """
    params: list[Any] = list(app_ids)
    if latest is not None:
        detail_sql += " AND r.last_seen = ?"
        params.append(latest)
    for d in _rows(q(conn, detail_sql, tuple(params))):
        data = _json_or(d.get("data_json"), {})
        entry = out[d["app_id"]]
        if d["type"] == "nginx_site":
            if data.get("enabled", False):
                entry["domains"].update(data.get("server_names") or [])
        elif d["type"] == "port":
            if data.get("port") is not None:
                entry["ports"].add(str(data["port"]))
        elif d["type"] == "container":
            entry["ports"].update(str(p) for p in data.get("published_ports") or [])
    return out


def _assoc_line(a: dict, *, with_resource: bool) -> list[str]:
    """Bullet line(s) for one association row (joined with its resource)."""
    level = _level(a.get("confidence"), a.get("source"))
    head = f"{a['resource_type']} {a['resource_key']}" if with_resource else f"app {a['slug']}"
    parts = [head]
    if with_resource and a.get("resource_display") and a["resource_display"] != a["resource_key"]:
        parts.append(f"display={a['resource_display']}")
    if with_resource and a.get("resource_path"):
        parts.append(f"path={a['resource_path']}")
    if with_resource and a.get("resource_state"):
        parts.append(f"state={a['resource_state']}")
    parts.append(f"confidence={a.get('confidence')} ({level})")
    parts.append(f"ownership={a.get('ownership') or 'unknown'}")
    parts.append(f"shared={'true' if a.get('shared') else 'false'}")
    parts.append(f"data_loss_risk={a.get('data_loss_risk') or 'unknown'}")
    parts.append(f"removal_eligible={a.get('removal_eligible') or 'unknown'}")
    if a.get("recommended_action"):
        parts.append(f"recommended_action={a['recommended_action']}")
    lines = ["- " + " | ".join(parts)]
    evidence = _json_or(a.get("evidence_json"), []) or []
    for ev in evidence[:_MAX_EVIDENCE]:
        stmt = ev.get("statement") if isinstance(ev, dict) else str(ev)
        if stmt:
            lines.append(f"    evidence: {_fmt(stmt)}")
    return lines


def _risk_rank(shared: bool, risk: str | None, owners: int = 1) -> tuple:
    """Sort key: shared / multi-owner / data-risk first (0 sorts first)."""
    return (0 if (shared or owners > 1) else 1, 0 if risk == "data" else 1)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def _shared_resource_rows(conn, latest: int | None) -> list[dict]:
    """Resources claimed by more than one current app, or flagged shared."""
    if latest is None:
        return []
    return _rows(
        q(
            conn,
            """
            SELECT r.type, r.key, r.display, r.path, r.state,
                   GROUP_CONCAT(DISTINCT ap.slug) AS slugs,
                   COUNT(DISTINCT a.app_id) AS n,
                   MAX(a.shared) AS flagged_shared
            FROM associations a
            JOIN resources r ON r.id = a.resource_id
            JOIN applications ap ON ap.id = a.app_id
            WHERE a.excluded = 0
              AND r.last_seen = ?
              AND ap.last_seen = ?
            GROUP BY r.id
            HAVING n > 1 OR flagged_shared = 1
            ORDER BY n DESC, r.type, r.key
            """,
            (latest, latest),
        )
    )


def _type_owner_rows(conn, latest: int | None, res_type: str) -> list[dict]:
    """Every current resource of one type with owner slugs (empty = unassigned)."""
    if latest is None:
        return []
    return _rows(
        q(
            conn,
            """
            SELECT r.type, r.key, r.display, r.state,
                   GROUP_CONCAT(DISTINCT ap.slug) AS slugs,
                   COUNT(DISTINCT a.app_id) AS n,
                   MAX(a.shared) AS flagged_shared,
                   MAX(CASE WHEN a.data_loss_risk = 'data' THEN 1 ELSE 0 END) AS has_data
            FROM resources r
            LEFT JOIN associations a ON a.resource_id = r.id AND a.excluded = 0
            LEFT JOIN applications ap ON ap.id = a.app_id AND ap.last_seen = ?
            WHERE r.last_seen = ? AND r.type = ?
            GROUP BY r.id
            ORDER BY n DESC, r.key
            """,
            (latest, latest, res_type),
        )
    )


def _owner_index_lines(rows: list[dict], heading: str) -> list[str]:
    lines = [f"## {heading}: {len(rows)}"]
    if not rows:
        lines.append("(none)")
        return lines
    for r in rows:
        slugs = r.get("slugs") or "-"
        lines.append(
            f"- {r['type']} {r['key']} | display={r.get('display') or '-'} | "
            f"owners={r.get('n') or 0} [{slugs}] | state={r.get('state') or '-'} | "
            f"shared={'true' if (r.get('flagged_shared') or (r.get('n') or 0) > 1) else 'false'} | "
            f"data_loss={'data' if r.get('has_data') else '-'}"
        )
    return lines


def build_general(conn, budget: int) -> ContextBundle:
    latest = _latest_scan_id(conn)
    apps = _apps_in_scan(conn, latest)
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for a in apps:
        by_status[a.get("status") or "unknown"] = by_status.get(a.get("status") or "unknown", 0) + 1
        by_kind[a.get("kind") or "unknown"] = by_kind.get(a.get("kind") or "unknown", 0) + 1
    counts = [c for c in _type_counts(conn, latest) if c["count"]]
    disk = _disk_usage_bytes(conn, latest)
    orphans = _orphan_rows(conn, latest)
    actionable = sum(1 for o in orphans if o["bucket"] == "actionable")
    agg = _app_aggregates(conn, [a["id"] for a in apps], latest)
    shared_rows = _shared_resource_rows(conn, latest)
    volume_rows = _type_owner_rows(conn, latest, "volume")
    image_rows = _type_owner_rows(conn, latest, "image")
    network_rows = _type_owner_rows(conn, latest, "network")
    container_rows = _type_owner_rows(conn, latest, "container")

    lines = [
        _scan_line(conn, latest),
        f"applications: {len(apps)}",
        "apps_by_status: " + (", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "none"),
        "apps_by_kind: " + (", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())) or "none"),
        "resources_by_type: " + (", ".join(f"{c['type']}={c['count']}" for c in counts) or "none"),
        f"disk_usage: {_human_bytes(disk)} ({disk} bytes; directories + volumes)",
        f"actionable_orphans: {actionable}",
        f"shared_or_multi_owner_resources: {len(shared_rows)}",
        "",
        f"## Shared resources (multi-owner): {len(shared_rows)}",
    ]
    if not shared_rows:
        lines.append("(none — no resource is claimed by more than one current app)")
    else:
        for r in shared_rows:
            slugs = r.get("slugs") or "-"
            lines.append(
                f"- {r['type']} {r['key']} | display={r.get('display') or '-'} | "
                f"owners={r.get('n')} [{slugs}] | state={r.get('state') or '-'} | "
                f"flagged_shared={'true' if r.get('flagged_shared') else 'false'}"
            )
    # Ownership indexes before the app list so truncation cannot drop owners.
    lines += [""] + _owner_index_lines(volume_rows, "Volumes (every current volume + owners)")
    lines += [""] + _owner_index_lines(image_rows, "Images (every current image + owners)")
    lines += [""] + _owner_index_lines(network_rows, "Networks (every current network + owners)")
    lines += [""] + _owner_index_lines(container_rows, "Containers (every current container + owners)")
    lines += ["", "## Applications"]
    for a in sorted(apps, key=lambda r: (r.get("name") or r.get("slug") or "").lower()):
        g = agg.get(a["id"], {})
        parts = [
            f"{a['slug']}",
            f"name={a.get('name')}",
            f"kind={a.get('kind') or 'unknown'}",
            f"status={a.get('status') or 'unknown'}",
            f"protected={'true' if a.get('protected') else 'false'}",
            f"domains={', '.join(sorted(g.get('domains', ()))) or '-'}",
            f"ports={', '.join(sorted(g.get('ports', ()), key=lambda s: (len(s), s))) or '-'}",
            f"resources={g.get('res_count', 0)}",
            f"warnings={g.get('warn_count', 0)}",
        ]
        lines.append("- " + " | ".join(parts))
    text, truncated = render_budgeted(lines, budget)
    facts = {
        "scan_id": latest,
        "app_count": len(apps),
        "apps_by_status": by_status,
        "apps_by_kind": by_kind,
        "resource_counts": {c["type"]: c["count"] for c in counts},
        "disk_usage_bytes": disk,
        "actionable_orphans": actionable,
        "shared_or_multi_owner": len(shared_rows),
        "volume_count": len(volume_rows),
        "image_count": len(image_rows),
    }
    return ContextBundle("general", None, "Whole server", facts, text, truncated)


def build_app(conn, slug: str, budget: int) -> ContextBundle:
    found = _rows(q(conn, "SELECT * FROM applications WHERE slug = ?", (slug,)))
    if not found:
        raise AssistantError("target", f"no such application: {slug}", 404)
    app = found[0]
    latest = _latest_scan_id(conn)
    # Removed app: fall back to its own last scan (same rule as app_detail).
    assoc_scan = latest
    if app.get("last_seen") is not None and latest is not None and int(app["last_seen"]) < int(latest):
        assoc_scan = int(app["last_seen"])
    sql = """
        SELECT a.*, ap.slug AS slug, r.type AS resource_type, r.key AS resource_key,
               r.display AS resource_display, r.path AS resource_path,
               r.state AS resource_state, r.data_json AS resource_data_json
        FROM associations a
        JOIN applications ap ON ap.id = a.app_id
        JOIN resources r ON r.id = a.resource_id
        WHERE a.app_id = ? AND a.excluded = 0
    """
    params: list[Any] = [app["id"]]
    if assoc_scan is not None:
        sql += " AND r.last_seen = ?"
        params.append(assoc_scan)
    assocs = _rows(q(conn, sql, tuple(params)))
    agg = _app_aggregates(conn, [app["id"]], assoc_scan).get(app["id"], {})

    lines = [
        _scan_line(conn, latest),
        f"app: {app['slug']}",
        f"name: {app.get('name')}",
        f"kind: {app.get('kind') or 'unknown'}",
        f"status: {app.get('status') or 'unknown'}",
        f"protected: {'true' if app.get('protected') else 'false'}",
        f"manifest: {'present (' + str(app['manifest_path']) + ')' if app.get('manifest_path') else 'none'}",
        f"removed: {'true (no longer present in latest scan)' if assoc_scan != latest else 'false'}",
        f"domains: {', '.join(sorted(agg.get('domains', ()))) or '-'}",
        f"ports: {', '.join(sorted(agg.get('ports', ()), key=lambda s: (len(s), s))) or '-'}",
        f"associations: {len(assocs)}",
        "",
        "## Associations (shared / data-risk first)",
    ]
    assocs.sort(key=lambda a: (
        _risk_rank(bool(a.get("shared")), a.get("data_loss_risk")),
        a.get("resource_type") or "",
        (a.get("resource_display") or a.get("resource_key") or "").lower(),
    ))
    for a in assocs:
        lines.extend(_assoc_line(a, with_resource=True))
    text, truncated = render_budgeted(lines, budget)
    facts = {
        "slug": app["slug"],
        "name": app.get("name"),
        "association_count": len(assocs),
        "shared_count": sum(1 for a in assocs if a.get("shared")),
        "data_risk_count": sum(1 for a in assocs if a.get("data_loss_risk") == "data"),
    }
    return ContextBundle("app", slug, f"Application {app.get('name') or slug}", facts, text, truncated)


_BUCKET_ORDER = {"actionable": 0, "expected": 1, "system": 2}


def build_orphans(conn, budget: int) -> ContextBundle:
    latest = _latest_scan_id(conn)
    rows = _orphan_rows(conn, latest)
    counts = {"actionable": 0, "system": 0, "expected": 0}
    for r in rows:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
    rows.sort(key=lambda r: (
        _BUCKET_ORDER.get(r["bucket"], 9),
        r.get("type") or "",
        (r.get("display") or r.get("key") or "").lower(),
    ))
    lines = [
        _scan_line(conn, latest),
        f"orphan_candidates: {len(rows)}",
        f"by_bucket: actionable={counts['actionable']}, expected={counts['expected']}, system={counts['system']}",
        "note: /orphans shows the actionable bucket by default; expected and system rows are listed for completeness",
        "",
        "## Candidates (actionable first)",
    ]
    for r in rows:
        data = r["data"]
        parts = [
            f"{r['type']} {r['key']}",
            f"bucket={r['bucket']}",
        ]
        if r.get("display") and r["display"] != r["key"]:
            parts.append(f"display={r['display']}")
        if r.get("path"):
            parts.append(f"path={r['path']}")
        if r.get("state"):
            parts.append(f"state={r['state']}")
        size = _size_of(data)
        if size is not None:
            parts.append(f"size={_human_bytes(size)}")
        parts.append(f"reason={_fmt(r['reason'])}")
        lines.append("- " + " | ".join(parts))
    text, truncated = render_budgeted(lines, budget)
    facts = {"total": len(rows), "counts": counts}
    return ContextBundle("orphans", None, "Orphan candidates", facts, text, truncated)


def _resources_of_type(conn, res_type: str, latest: int | None) -> list[dict]:
    sql = "SELECT * FROM resources WHERE type = ?"
    params: tuple = (res_type,)
    if latest is not None:
        sql += " AND last_seen = ?"
        params = (res_type, latest)
    sql += " ORDER BY display"
    rows = _rows(q(conn, sql, params))
    owners = _owner_map(conn, [r["id"] for r in rows])
    for r in rows:
        r["data"] = _clean_data(_json_or(r.get("data_json"), {}))
        info = owners.get(r["id"], {"apps": [], "shared": False})
        r["owners"] = info["apps"]
        r["shared"] = info["shared"]
    return rows


def _resource_extras(res_type: str, data: dict) -> list[str]:
    parts = []
    if res_type == "image":
        parts.append(f"size={_human_bytes(_size_of(data))}")
        parts.append(f"dangling={'true' if data.get('dangling') else 'false'}")
        parts.append(f"containers_using={', '.join(data.get('containers_using') or []) or '-'}")
    elif res_type == "volume":
        if data.get("driver"):
            parts.append(f"driver={data['driver']}")
        parts.append(f"containers_using={', '.join(data.get('containers_using') or []) or '-'}")
        size = _size_of(data)
        if size is not None:
            parts.append(f"size={_human_bytes(size)}")
    elif res_type == "network":
        if data.get("driver"):
            parts.append(f"driver={data['driver']}")
        if data.get("containers"):
            parts.append(f"containers={_fmt(data['containers'])}")
    elif res_type == "container":
        if data.get("image"):
            parts.append(f"image={data['image']}")
        if data.get("compose_project"):
            parts.append(f"compose_project={data['compose_project']}")
        if data.get("published_ports"):
            parts.append(f"published_ports={_fmt(data['published_ports'])}")
    return parts


def build_resource_type(conn, res_type: str, budget: int) -> ContextBundle:
    if res_type not in RESOURCE_TYPE_TARGETS:
        raise AssistantError(
            "target", f"resource type must be one of {', '.join(RESOURCE_TYPE_TARGETS)}", 404
        )
    latest = _latest_scan_id(conn)
    rows = _resources_of_type(conn, res_type, latest)
    orphan_by_id = {o["id"]: o for o in _orphan_rows(conn, latest) if o["type"] == res_type}
    rows.sort(key=lambda r: (
        _risk_rank(bool(r["shared"]), None, len(r["owners"])),
        (r.get("display") or r.get("key") or "").lower(),
    ))
    label = RESOURCE_TYPE_LABELS.get(res_type, res_type)
    shared_n = sum(1 for r in rows if r["shared"] or len(r["owners"]) > 1)
    unowned_n = sum(1 for r in rows if not r["owners"])
    lines = [
        _scan_line(conn, latest),
        f"resource_type: {res_type} ({label})",
        f"count: {len(rows)}",
        f"shared_or_multi_owner: {shared_n}",
        f"without_owner: {unowned_n}",
        f"orphan_candidates: {len(orphan_by_id)}",
        "",
        f"## {label} (shared first)",
    ]
    for r in rows:
        parts = [f"{res_type} {r['key']}"]
        if r.get("display") and r["display"] != r["key"]:
            parts.append(f"display={r['display']}")
        if r.get("path"):
            parts.append(f"path={r['path']}")
        parts.append(f"state={r.get('state') or 'unknown'}")
        parts.append(f"owners={', '.join(a['slug'] for a in r['owners']) or '-'}")
        parts.append(f"shared={'true' if r['shared'] else 'false'}")
        parts.extend(_resource_extras(res_type, r["data"]))
        o = orphan_by_id.get(r["id"])
        parts.append(f"orphan={o['bucket'] if o else 'no'}")
        lines.append("- " + " | ".join(parts))
    text, truncated = render_budgeted(lines, budget)
    facts = {
        "type": res_type,
        "count": len(rows),
        "shared_or_multi_owner": shared_n,
        "without_owner": unowned_n,
        "orphan_candidates": len(orphan_by_id),
    }
    return ContextBundle("resource_type", res_type, f"All {label.lower()}", facts, text, truncated)


def _parse_resource_target(target: str | None) -> tuple[str, str]:
    if not target or ":" not in target:
        raise AssistantError("target", "resource target must be <type>:<key>", 404)
    res_type, key = target.split(":", 1)
    if res_type not in RESOURCE_TYPE_TARGETS or not key:
        raise AssistantError("target", f"unknown resource target: {target}", 404)
    return res_type, key


def build_resource(conn, target: str, budget: int) -> ContextBundle:
    res_type, key = _parse_resource_target(target)
    latest = _latest_scan_id(conn)
    sql = "SELECT * FROM resources WHERE type = ? AND key = ?"
    params: tuple = (res_type, key)
    if latest is not None:
        sql += " AND last_seen = ?"
        params = (res_type, key, latest)
    found = _rows(q(conn, sql, params))
    if not found:
        raise AssistantError("target", f"no such resource in the latest scan: {target}", 404)
    r = found[0]
    data = _clean_data(_json_or(r.get("data_json"), {}))
    owners = _owner_map(conn, [r["id"]]).get(r["id"], {"apps": [], "shared": False})
    assocs = _rows(q(
        conn,
        """
        SELECT a.*, ap.slug AS slug, ap.name AS app_name
        FROM associations a JOIN applications ap ON ap.id = a.app_id
        WHERE a.resource_id = ? AND a.excluded = 0
        ORDER BY a.shared DESC, a.confidence DESC, ap.slug
        """,
        (r["id"],),
    ))
    orphan = next((o for o in _orphan_rows(conn, latest) if o["id"] == r["id"]), None)

    lines = [
        _scan_line(conn, latest),
        f"resource: {res_type} {key}",
        f"display: {r.get('display') or key}",
        f"path: {r.get('path') or '-'}",
        f"state: {r.get('state') or 'unknown'}",
        f"owners: {', '.join(a['slug'] for a in owners['apps']) or '- (none)'}",
        f"owner_count: {len(owners['apps'])}",
        f"shared: {'true' if owners['shared'] else 'false'}",
        f"orphan_candidate: {orphan['bucket'] + ' — ' + _fmt(orphan['reason']) if orphan else 'no'}",
    ]
    if res_type == "image":
        using = data.get("containers_using") or []
        lines.append(f"containers_using: {', '.join(using) or '-'}")
        repo_tag = _normalize_image_ref(data.get("repo_tag") or r.get("display"))
        declared = _compose_declared_images(conn)
        projects = sorted({p for ref, p in declared.items() if ref == repo_tag})
        lines.append(f"compose_projects_declaring: {', '.join(projects) or '-'}")
    lines.append("")
    lines.append("## Data")
    for k in sorted(data):
        lines.append(f"{k}: {_fmt(data[k])}")
    lines.append("")
    lines.append("## Owner associations")
    if not assocs:
        lines.append("- none")
    for a in assocs:
        lines.extend(_assoc_line(a, with_resource=False))
    text, truncated = render_budgeted(lines, budget)
    facts = {
        "type": res_type,
        "key": key,
        "display": r.get("display"),
        "owners": [a["slug"] for a in owners["apps"]],
        "shared": owners["shared"],
        "orphan_bucket": orphan["bucket"] if orphan else None,
    }
    return ContextBundle("resource", target, f"{res_type} {r.get('display') or key}", facts, text, truncated)


def build_context(conn, scope: str, target: str | None, budget: int) -> ContextBundle:
    """Dispatch on scope. Unknown scope → AssistantError("scope", 400);
    unknown/missing target → AssistantError("target", 404)."""
    if scope not in SCOPES:
        raise AssistantError("scope", f"unknown scope: {scope}", 400)
    if scope == "general":
        return build_general(conn, budget)
    if scope == "orphans":
        return build_orphans(conn, budget)
    if not target:
        raise AssistantError("target", f"scope {scope} requires a target", 404)
    if scope == "app":
        return build_app(conn, target, budget)
    if scope == "resource_type":
        return build_resource_type(conn, target, budget)
    return build_resource(conn, target, budget)


def list_targets(conn, scope: str, resource_type: str | None = None) -> list[dict]:
    """[{"value","label"}] selectable targets for a scope: apps in the latest
    scan for `app`; the four docker types for `resource_type`; that type's
    resources for `resource` (requires resource_type); [] otherwise."""
    if scope not in SCOPES:
        raise AssistantError("scope", f"unknown scope: {scope}", 400)
    if scope == "app":
        latest = _latest_scan_id(conn)
        return [
            {"value": a["slug"], "label": f"{a.get('name') or a['slug']} ({a['slug']})"}
            for a in _apps_in_scan(conn, latest)
        ]
    if scope == "resource_type":
        return [{"value": t, "label": RESOURCE_TYPE_LABELS.get(t, t)} for t in RESOURCE_TYPE_TARGETS]
    if scope == "resource":
        if resource_type not in RESOURCE_TYPE_TARGETS:
            return []
        latest = _latest_scan_id(conn)
        sql = "SELECT type, key, display FROM resources WHERE type = ?"
        params: tuple = (resource_type,)
        if latest is not None:
            sql += " AND last_seen = ?"
            params = (resource_type, latest)
        sql += " ORDER BY display"
        return [
            {"value": f"{r['type']}:{r['key']}", "label": r.get("display") or r["key"]}
            for r in _rows(q(conn, sql, params))
        ]
    return []
