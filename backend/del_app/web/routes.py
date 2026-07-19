"""DEL web UI routes: dashboard, apps, plans, jobs, resources, orphans,
settings, manifests. All routes here (except /login, /healthz which is owned
by main.py) sit behind auth.require_user.

Other lanes' modules (planner, jobs, scanner, manifests) are imported lazily
/ defensively so this module still imports cleanly (and is testable with
monkeypatched fakes) even before those lanes land.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates

from del_app import auditlog, auth
from del_app.auth import User
from del_app.config import get_settings
from del_app.db import get_db, q

# Lazy/defensive imports of sibling lanes' modules. Accessed as
# `<name>.<func>` at call time so tests can monkeypatch these module
# references directly on this `routes` module.
try:
    from del_app import planner
except ImportError:  # pragma: no cover - lane not landed yet
    planner = None  # type: ignore[assignment]

try:
    from del_app import jobs
except ImportError:  # pragma: no cover
    jobs = None  # type: ignore[assignment]

try:
    from del_app import scanner
except ImportError:  # pragma: no cover
    scanner = None  # type: ignore[assignment]

try:
    from del_app import manifests
except ImportError:  # pragma: no cover
    manifests = None  # type: ignore[assignment]


WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()

# Jinja globals registered after helpers are defined (see bottom of module).

# DB resource types are SINGULAR. Ordered for the Resources tab bar.
ALL_RESOURCE_TYPES = [
    "container", "image", "volume", "network", "compose_project",
    "nginx_site", "systemd_unit", "systemd_timer", "cron_entry",
    "process", "port", "directory", "git_repo", "env_file",
    "bind_mount", "tmux_session",
]

# Per-type explanation of why a resource with no app association shows up
# on the Orphans page (used as a group-level explainer and, where a type has
# no more specific per-row reason, the per-row reason too).
ORPHAN_REASONS = {
    "container": "not part of any known application (no compose project, no matching evidence)",
    "image": "not used by any container",
    "volume": "attached to no container",
    "network": "not attached to any known application's container(s)",
    "nginx_site": "config file present but not enabled/serving",
    "compose_project": "no running containers or matching directory found for this project",
    "systemd_unit": "WorkingDirectory/ExecStart does not match any known application path",
    "systemd_timer": "does not activate a unit belonging to any known application",
    "cron_entry": "command does not reference any known application path",
    "directory": "no running app matches",
    "git_repo": "no running app matches",
    "env_file": "no running app matches",
    "bind_mount": "not attached to any known application's container",
    "process": "not matched to any known application",
    "port": "not matched to any known application",
    "tmux_session": "not matched to any known application",
}


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


def _orphan_reason(res_type: str, data: dict, compose_images: dict[str, str] | None = None) -> str:
    """Per-row reason a resource is an orphan candidate. Falls back to the
    type-level explainer when there's nothing more specific to say."""
    if res_type == "nginx_site":
        if not data.get("enabled", False):
            return "config file not enabled (stale copy/backup)" if data.get("stale_copy") else "config file present but not enabled/serving"
        return "enabled site not attributed to any app"
    if res_type == "image":
        if data.get("dangling"):
            return "dangling (untagged) image, unused"
        repo_tag = _normalize_image_ref(data.get("repo_tag"))
        project = (compose_images or {}).get(repo_tag)
        if project:
            return f"unused, but referenced by compose project {project} (not currently running)"
        return "not used by any container"
    if res_type == "volume":
        return "attached to no container"
    return ORPHAN_REASONS.get(res_type, "no matching application found")


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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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


def _human_size(num: Any) -> str:
    """Human-format a byte count. Accepts int/float/None; returns e.g. '1.2 GB'."""
    try:
        n = float(num)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while n >= 1000 and i < len(units) - 1:
        n /= 1000.0
        i += 1
    return (f"{n:.0f} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}")


def _level(confidence: Any, source: str | None) -> str:
    """Map numeric confidence + source to a level (mirrors planner mapping)."""
    if source == "manual":
        return "manual"
    try:
        c = int(confidence)
    except (TypeError, ValueError):
        return "possible"
    if c >= 95:
        return "confirmed"
    if c >= 80:
        return "high"
    if c >= 60:
        return "probable"
    if c >= 30:
        return "possible"
    return "unrelated"


def _duration(started: Any, finished: Any) -> str:
    """Human duration between two ISO/sqlite datetime strings, or '—'."""
    from datetime import datetime

    if not started or not finished:
        return "—"
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        s = datetime.fromisoformat(str(started).replace(" ", "T").split(".")[0])
        f = datetime.fromisoformat(str(finished).replace(" ", "T").split(".")[0])
    except ValueError:
        try:
            s = datetime.strptime(str(started)[:19], fmt)
            f = datetime.strptime(str(finished)[:19], fmt)
        except ValueError:
            return "—"
    secs = (f - s).total_seconds()
    if secs < 0:
        return "—"
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.1f}m"
    return f"{secs / 3600:.1f}h"


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


def _parse_docker_size(text: str) -> int:
    """Parse a docker-cli human size string (e.g. '1.2GB', '500MB (40%)',
    '0B') into a byte count. Docker's go-units formats with 1000-based
    units, matching this module's own _human_size."""
    text = (text or "").split("(")[0].strip()
    if not text:
        return 0
    units = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4, "PB": 1000**5}
    for suffix, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if text.upper().endswith(suffix):
            num = text[: -len(suffix)].strip()
            try:
                return int(float(num) * mult)
            except ValueError:
                return 0
    return 0


_RECLAIMABLE_CACHE: dict[str, Any] = {"ts": 0.0, "value": 0}
_RECLAIMABLE_TTL = 300  # 5 minutes


def _reclaimable_bytes() -> int:
    """Sum of docker's reported reclaimable bytes (images + build cache +
    volumes) from `docker system df --format '{{json .}}'`, read-only and
    cached in-process for 5 minutes so the dashboard stays fast."""
    now = time.monotonic()
    if now - _RECLAIMABLE_CACHE["ts"] < _RECLAIMABLE_TTL:
        return _RECLAIMABLE_CACHE["value"]
    total = 0
    try:
        proc = subprocess.run(
            ["docker", "system", "df", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += _parse_docker_size(obj.get("Reclaimable", ""))
    except Exception:
        pass
    _RECLAIMABLE_CACHE["ts"] = now
    _RECLAIMABLE_CACHE["value"] = total
    return total


def _latest_scan_id(conn) -> int | None:
    rows = q(conn, "SELECT MAX(id) AS m FROM scans")
    if rows:
        r = dict(rows[0]) if hasattr(rows[0], "keys") else rows[0]
        return r.get("m")
    return None


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


def _csrf_seed(request: Request) -> tuple[str, str | None]:
    """Return (csrf_token, raw_seed_to_persist_or_None). If the request
    already carries a session cookie, derive the CSRF token from it and
    nothing new needs to be persisted. Otherwise (e.g. a fresh /login visit)
    mint a throwaway anti-forgery seed that must be set as a cookie on the
    response."""
    cookie = request.cookies.get(auth.SESSION_COOKIE_NAME)
    token = auth.unsign_token(cookie) if cookie else None
    if token is not None:
        return auth.csrf_token(token), None
    raw = secrets.token_hex(16)
    return auth.csrf_token(raw), raw


def _render(name: str, request: Request, response: Response, **extra) -> HTMLResponse:
    csrf_token, seed = _csrf_seed(request)
    ctx = {
        "flash": request.query_params.get("flash"),
        "error": request.query_params.get("error"),
        "csrf_token": csrf_token,
    }
    ctx.update(extra)
    rendered = templates.TemplateResponse(request, name, ctx)
    if seed is not None:
        rendered.set_cookie(
            auth.SESSION_COOKIE_NAME, auth.sign_token(seed), httponly=True, samesite="lax"
        )
    return rendered


def _require_csrf(request: Request, submitted: str | None) -> bool:
    return auth.check_csrf(request, submitted or "")


def _csrf_response() -> JSONResponse:
    return JSONResponse({"error": "invalid csrf token"}, status_code=403)


# ---------------------------------------------------------------------------
# auth: login / logout
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return _render("login.html", request, HTMLResponse(""))


@router.post("/login")
def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
) -> Response:
    if not _require_csrf(request, csrf_token):
        return RedirectResponse(url="/login?error=Invalid+request", status_code=303)

    ip = request.client.host if request.client else "unknown"
    if auth.rate_limited(ip):
        return RedirectResponse(
            url="/login?error=Too+many+attempts%2C+try+again+later", status_code=303
        )
    auth.record_attempt(ip)

    user_id = auth.verify(username, password)
    if user_id is None:
        return RedirectResponse(url="/login?error=Invalid+credentials", status_code=303)

    redirect = RedirectResponse(url="/", status_code=303)
    auth.login_session(redirect, user_id, ip=ip)
    auditlog.audit(user_id, "login", "session", {"ip": ip})
    return redirect


@router.post("/logout")
def logout(
    request: Request, user: User = Depends(auth.require_user), csrf_token: str = Form("")
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    redirect = RedirectResponse(url="/login", status_code=303)
    auth.logout_session(request, redirect)
    auditlog.audit(user.id, "logout", "session", {})
    return redirect


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        apps = _rows(q(conn, "SELECT * FROM applications"))
        running_jobs = _rows(
            q(conn, "SELECT * FROM jobs WHERE status = 'running'")
        )
        uncertain = _rows(
            q(
                conn,
                "SELECT * FROM associations WHERE removal_eligible = 'uncertain'",
            )
        )
        shared = _rows(q(conn, "SELECT * FROM resources WHERE state != 'removed'"))
        shared_count = len(
            _rows(q(conn, "SELECT DISTINCT resource_id FROM associations WHERE shared = 1"))
        )
        orphan_candidates = _rows(
            q(
                conn,
                """
                SELECT r.* FROM resources r
                LEFT JOIN associations a ON a.resource_id = r.id
                WHERE a.id IS NULL
                """,
            )
        )
        recent_scans = _rows(
            q(conn, "SELECT * FROM scans ORDER BY id DESC LIMIT 5")
        )
        recent_jobs = _rows(q(conn, "SELECT * FROM jobs ORDER BY id DESC LIMIT 5"))
        disk_usage_bytes = _disk_usage_bytes(conn, _latest_scan_id(conn))
    finally:
        conn.close()

    stats = {
        "apps": len(apps),
        "running": len(running_jobs),
        "orphan_candidates": len(orphan_candidates),
        "shared_resources": shared_count,
        "uncertain_mappings": len(uncertain),
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
        user=user,
    )


# ---------------------------------------------------------------------------
# applications
# ---------------------------------------------------------------------------

@router.get("/apps", response_class=HTMLResponse)
def apps_list(
    request: Request,
    response: Response,
    user: User = Depends(auth.require_user),
    search: str = "",
    status: str = "",
) -> HTMLResponse:
    conn = get_db()
    try:
        latest = _latest_scan_id(conn)
        sql = "SELECT * FROM applications WHERE 1=1"
        if request.query_params.get("show") != "removed" and latest:
            sql += f" AND last_seen = {int(latest)}"
        params: list[Any] = []
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
        agg = _rows(
            q(
                conn,
                """
                SELECT ap.id AS app_id,
                       COUNT(a.id) AS res_count,
                       SUM(CASE WHEN a.ownership = 'possible' OR a.confidence < 50
                                THEN 1 ELSE 0 END) AS warn_count
                FROM applications ap
                LEFT JOIN associations a
                       ON a.app_id = ap.id AND a.excluded = 0
                GROUP BY ap.id
                """,
            )
        )
        agg_map = {r["app_id"]: r for r in agg}

        detail = _rows(
            q(
                conn,
                """
                SELECT a.app_id AS app_id, r.type AS type, r.data_json AS data_json
                FROM associations a
                JOIN resources r ON r.id = a.resource_id
                WHERE a.excluded = 0 AND r.type IN ('nginx_site', 'port', 'container')
                """,
            )
        )
    finally:
        conn.close()

    domains: dict[int, set] = {}
    ports: dict[int, set] = {}
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

    for app in apps:
        aid = app.get("id")
        a = agg_map.get(aid, {})
        app["res_count"] = a.get("res_count") or 0
        app["warn_count"] = a.get("warn_count") or 0
        app["domains"] = sorted(domains.get(aid, set()))
        app["ports"] = sorted(ports.get(aid, set()), key=lambda x: (len(x), x))

    return _render(
        "apps.html", request, response, apps=apps, search=search, status=status
    )


@router.get("/apps/{slug}", response_class=HTMLResponse)
def app_detail(
    slug: str, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        rows = q(conn, "SELECT * FROM applications WHERE slug = ?", (slug,))
        latest_scan = _latest_scan_id(conn)
        app = _rows(rows)[0] if rows else {"slug": slug, "name": slug}
        assoc_rows = _rows(
            q(
                conn,
                """
                SELECT a.*, r.type as resource_type, r.key as resource_key,
                       r.display as resource_display, r.path as resource_path,
                       r.state as resource_state, r.data_json as resource_data_json
                FROM associations a
                JOIN resources r ON r.id = a.resource_id
                WHERE a.app_id = (SELECT id FROM applications WHERE slug = ?)
                """,
                (slug,),
            )
        )
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

    conn = get_db()
    try:
        if action == "approve":
            conn.execute(
                "UPDATE associations SET approved_by_user = 1, excluded = 0 WHERE id = ? AND app_id = (SELECT id FROM applications WHERE slug = ?)",
                (association_id, slug),
            )
        elif action == "exclude":
            conn.execute(
                "UPDATE associations SET excluded = 1 WHERE id = ? AND app_id = (SELECT id FROM applications WHERE slug = ?)",
                (association_id, slug),
            )
        elif action == "mark-shared":
            conn.execute(
                "UPDATE associations SET shared = 1 WHERE id = ? AND app_id = (SELECT id FROM applications WHERE slug = ?)",
                (association_id, slug),
            )
        conn.commit()
    finally:
        conn.close()
    auditlog.audit(user.id, f"association.{action}", f"{slug}#{association_id}", {})
    labels = {"approve": "approved", "exclude": "excluded", "mark-shared": "marked+shared"}
    flash = labels.get(action, "updated")
    return RedirectResponse(url=f"/apps/{slug}?flash=Association+{flash}", status_code=303)


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------

@router.get("/apps/{slug}/plan", response_class=HTMLResponse)
def plan_form(
    slug: str, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        rows = q(conn, "SELECT * FROM applications WHERE slug = ?", (slug,))
        latest_scan = _latest_scan_id(conn)
        app = _rows(rows)[0] if rows else {"slug": slug, "name": slug}
        volumes = _rows(
            q(
                conn,
                """
                SELECT r.* FROM resources r
                JOIN associations a ON a.resource_id = r.id
                JOIN applications ap ON ap.id = a.app_id
                WHERE ap.slug = ? AND r.type = 'volume'
                """,
                (slug,),
            )
        )
    finally:
        conn.close()
    return _render(
        "plan.html", request, response, app=app, volumes=volumes, plan=None
    )


@router.post("/apps/{slug}/plan")
def plan_build(
    slug: str,
    request: Request,
    user: User = Depends(auth.require_user),
    csrf_token: str = Form(""),
    remove_named_volumes: str | None = Form(None),
    approved_volume: list[str] | None = Form(None),
    remove_images: str = Form("none"),
    remove_bind_data: str | None = Form(None),
    remove_repo: str | None = Form(None),
    remove_networks: str | None = Form(None),
    backup: str = Form("none"),
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    if planner is None:  # pragma: no cover
        return JSONResponse({"error": "planner unavailable"}, status_code=503)

    options = {
        "remove_named_volumes": bool(remove_named_volumes),
        "approved_volumes": approved_volume or [],
        "remove_images": remove_images,
        "remove_bind_data": bool(remove_bind_data),
        "remove_repo": bool(remove_repo),
        "remove_networks": bool(remove_networks),
        "backup": backup,
    }
    plan = planner.build_plan(slug, options)
    planner.persist_plan(plan)
    plan_id = plan.id if hasattr(plan, "id") else plan.get("id")
    auditlog.audit(user.id, "plan.build", slug, {"options": options, "plan_id": plan_id})
    return RedirectResponse(url=f"/plans/{plan_id}", status_code=303)


def _load_plan_dict(plan_id: int) -> dict | None:
    """Load a persisted, HMAC-verified plan for display, via planner's
    load_plan (best-effort verification: falls back to the unverified
    load if the integrity check fails, but flags it in the rendered plan)."""
    if planner is None:  # pragma: no cover
        return None
    try:
        plan, stored_hmac, recomputed_hmac = planner.load_plan(plan_id)
    except Exception:
        return None
    tampered = bool(stored_hmac) and stored_hmac != recomputed_hmac
    row = plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)
    stages: dict[str, list] = {}
    for step in row.get("steps", []):
        stages.setdefault(step.get("stage", "other"), []).append(step)
    row["stages"] = stages
    row["tampered"] = tampered
    row.setdefault("status", "draft")
    row.setdefault("created", None)
    return row


@router.get("/plans/{plan_id}", response_class=HTMLResponse)
def plan_view(
    plan_id: int, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    plan_row = _load_plan_dict(plan_id)
    if plan_row is None:
        return _render(
            "plan.html", request, response, app={"slug": "", "name": ""}, volumes=[], plan=None,
            error_message="Plan not found",
        )
    conn = get_db()
    try:
        app_rows = q(conn, "SELECT * FROM applications WHERE slug = ?", (plan_row["app_slug"],))
    finally:
        conn.close()
    app = _rows(app_rows)[0] if app_rows else {"slug": plan_row["app_slug"], "name": plan_row["app_slug"]}
    return _render("plan.html", request, response, app=app, volumes=[], plan=plan_row)


@router.post("/plans/{plan_id}/execute")
def plan_execute(
    plan_id: int,
    request: Request,
    user: User = Depends(auth.require_user),
    csrf_token: str = Form(""),
    mode: str = Form("dry_run"),
    confirm_phrase: str = Form(""),
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    if jobs is None or planner is None:  # pragma: no cover
        return JSONResponse({"error": "jobs engine unavailable"}, status_code=503)

    try:
        plan = planner.verify_plan(plan_id)
    except Exception:
        return JSONResponse({"error": "plan not found or failed integrity check"}, status_code=404)

    has_volume_deletion = any(step.operation == "volume_rm" for step in plan.steps)
    if mode == "live" and has_volume_deletion and confirm_phrase != "DELETE VOLUMES":
        return JSONResponse(
            {"error": "typed confirmation phrase required for live volume deletion"},
            status_code=400,
        )

    job_id = jobs.create_job(plan_id, mode, user.id)
    jobs.execute_job(job_id, confirm_phrase=(confirm_phrase if mode == "live" else None))
    auditlog.audit(user.id, "job.execute", f"plan#{plan_id}", {"mode": mode})
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

@router.get("/jobs", response_class=HTMLResponse)
def jobs_list(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        job_rows = _rows(
            q(
                conn,
                """
                SELECT j.*, ap.slug AS app_slug, ap.name AS app_name
                FROM jobs j
                LEFT JOIN plans p ON p.id = j.plan_id
                LEFT JOIN applications ap ON ap.id = p.app_id
                ORDER BY j.id DESC
                """,
            )
        )
    finally:
        conn.close()
    for j in job_rows:
        j["duration"] = _duration(j.get("started"), j.get("finished"))
    return _render("jobs.html", request, response, jobs=job_rows)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(
    job_id: int, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        rows = q(
            conn,
            """
            SELECT j.*, ap.slug AS app_slug, ap.name AS app_name
            FROM jobs j
            LEFT JOIN plans p ON p.id = j.plan_id
            LEFT JOIN applications ap ON ap.id = p.app_id
            WHERE j.id = ?
            """,
            (job_id,),
        )
        job = _rows(rows)[0] if rows else {"id": job_id, "status": "unknown"}
        steps = _rows(
            q(conn, "SELECT * FROM job_steps WHERE job_id = ? ORDER BY seq", (job_id,))
        )
    finally:
        conn.close()
    for s in steps:
        s["duration"] = _duration(s.get("started"), s.get("finished"))
    stages: list[dict] = []
    for s in steps:
        st = s.get("stage") or "other"
        if not stages or stages[-1]["stage"] != st:
            stages.append({"stage": st, "steps": []})
        stages[-1]["steps"].append(s)
    return _render(
        "job_detail.html", request, response, job=job, steps=steps, stages=stages
    )


@router.get("/jobs/{job_id}/status")
def job_status(job_id: int, user: User = Depends(auth.require_user)) -> JSONResponse:
    if jobs is None:  # pragma: no cover
        return JSONResponse({"error": "jobs engine unavailable"}, status_code=503)
    status = jobs.job_status(job_id)
    return JSONResponse(status)


# ---------------------------------------------------------------------------
# resources / orphans
# ---------------------------------------------------------------------------

@router.get("/resources", response_class=HTMLResponse)
def resources_index(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    return RedirectResponse(url="/resources/container", status_code=307)


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


@router.get("/orphans", response_class=HTMLResponse)
def orphans_view(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        latest = _latest_scan_id(conn)
        sql = """
            SELECT r.* FROM resources r
            LEFT JOIN associations a ON a.resource_id = r.id
            WHERE a.id IS NULL
        """
        params: tuple = ()
        if latest is not None:
            sql += " AND r.last_seen = ?"
            params = (latest,)
        sql += " ORDER BY r.type, r.display"
        rows = _rows(q(conn, sql, params))
        compose_images = _compose_declared_images(conn)
    finally:
        conn.close()
    for r in rows:
        r["data"] = _json_or(r.get("data_json"), {})
        r["reason"] = _orphan_reason(r["type"], r["data"], compose_images)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["type"], []).append(r)
    grouped = [
        {
            "type": t,
            "label": RESOURCE_TYPE_LABELS.get(t, t),
            "explainer": ORPHAN_REASONS.get(t, "no matching application found"),
            "rows": groups[t],
        }
        for t in ALL_RESOURCE_TYPES
        if t in groups
    ]
    return _render(
        "orphans.html", request, response, grouped=grouped, total=len(rows)
    )


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

@router.post("/scan")
def trigger_scan(
    request: Request, user: User = Depends(auth.require_user), csrf_token: str = Form("")
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    if scanner is None:  # pragma: no cover
        return RedirectResponse(url="/settings?error=Scanner+unavailable", status_code=303)
    scan_id = scanner.run_scan()
    auditlog.audit(user.id, "scan.run", "scanner", {"scan_id": scan_id})
    return RedirectResponse(url=f"/settings?flash=Scan+{scan_id}+started", status_code=303)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
def settings_view(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    settings = get_settings()
    conn = get_db()
    try:
        db_settings = _rows(q(conn, "SELECT * FROM settings"))
        recent_scans = _rows(q(conn, "SELECT * FROM scans ORDER BY id DESC LIMIT 10"))
    finally:
        conn.close()
    return _render(
        "settings.html",
        request,
        response,
        settings=settings.model_dump(),
        db_settings=db_settings,
        recent_scans=recent_scans,
        user=user,
    )


# ---------------------------------------------------------------------------
# manifests
# ---------------------------------------------------------------------------

@router.get("/manifests/{slug}", response_class=HTMLResponse)
def manifest_edit_form(
    slug: str, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    yaml_text = ""
    if manifests is not None:
        all_manifests = manifests.load_all()
        m = all_manifests.get(slug)
        if m is not None:
            data = m.model_dump() if hasattr(m, "model_dump") else dict(m)
            yaml_text = yaml.safe_dump(data, sort_keys=False)
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

    if data is not None and manifests is not None:
        try:
            manifest = manifests.Manifest(**data)
        except Exception as exc:  # pydantic ValidationError or similar
            errors.append(str(exc))
        else:
            manifests.save(manifest)
            auditlog.audit(user.id, "manifest.save", slug, {})
            return RedirectResponse(url=f"/apps/{slug}?flash=Manifest+saved", status_code=303)
    elif data is not None and manifests is None:  # pragma: no cover
        errors.append("Manifests module unavailable")

    return _render(
        "manifest_edit.html",
        request,
        response,
        slug=slug,
        yaml_text=yaml_text,
        validation_errors=errors,
    )


# ---------------------------------------------------------------------------
# static assets (no auth: needed by /login too; CSP 'self', no CDN)
# ---------------------------------------------------------------------------

@router.get("/static/app.css")
def static_css() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.css", media_type="text/css")


@router.get("/static/app.js")
def static_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")


# ---------------------------------------------------------------------------
# Jinja globals (template-side formatting helpers)
# ---------------------------------------------------------------------------
templates.env.globals["human_size"] = _human_size
templates.env.globals["level_of"] = _level
templates.env.globals["duration"] = _duration
templates.env.globals["resource_labels"] = RESOURCE_TYPE_LABELS
