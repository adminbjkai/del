"""DEL web UI routes: dashboard, apps, plans, jobs, resources, orphans,
settings, manifests. All routes here (except /login, /healthz which is owned
by main.py) sit behind auth.require_user.

Other lanes' modules (planner, jobs, scanner, manifests) are imported lazily
/ defensively so this module still imports cleanly (and is testable with
monkeypatched fakes) even before those lanes land.
"""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
from del_app.correlate import _DOCKER_BUILTIN_NETWORKS
from del_app.db import get_db, latest_done_scan_id, q

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

# Per-type default explanation for *actionable* orphan candidates.
ORPHAN_REASONS = {
    "container": "container has no app association (no compose project / path evidence)",
    "image": "image not used by any container and not declared by a known compose file",
    "volume": "named volume attached to no container — possible leftover data",
    "network": "user network not attached to any known app's containers",
    "nginx_site": "enabled site not attributed to any app (or parked/disabled config)",
    "compose_project": "compose project with no running containers / weak directory match",
    "systemd_unit": "custom unit under /etc/systemd/system not correlated to an app",
    "systemd_timer": "custom timer not correlated to any known app unit",
    "cron_entry": "app-related cron line not correlated to a known project path",
    "directory": "project directory under a scan root not claimed by any app",
    "git_repo": "git repo under a scan root not claimed by any app",
    "env_file": ".env under a scan root not claimed by any app",
    "bind_mount": "bind mount not attributed to any known app container",
    "process": "host process under a scan root not matched to an app",
    "port": "listener not matched to a correlated app (may still be a known unit)",
    "tmux_session": "tmux session not matched to any known application",
}

# Docker built-in networks — never "orphans"; they always exist. Imported from
# correlate so the classifier and the correlation engine cannot disagree.

# OS cron.daily/etc. basenames that are not app leftovers.
_SYSTEM_CRON_BASENAMES = frozenset({
    ".placeholder", "0anacron", "apport", "apt-compat", "cracklib-runtime",
    "dpkg", "logrotate", "man-db", "sysstat", "update-notifier-common",
    "bsdmainutils", "popularity-contest", "mlocate", "plocate", "locate",
    "google-chrome", "google-chrome-stable",
})

# Systemd timer basenames that are distro maintenance, not app orphans.
_SYSTEM_TIMER_MARKERS = (
    "anacron", "apport", "apt-", "dpkg-", "e2scrub", "fstrim", "fwupd",
    "logrotate", "man-db", "motd-", "systemd-", "ua-", "update-notifier",
    "phpsessionclean", "certbot", "snap.",
)

# Listener ports that are almost always OS / infra, not abandoned apps.
_SYSTEM_PORTS = frozenset({
    22, 25, 53, 67, 68, 111, 123, 137, 138, 139, 445, 631, 2049, 5353, 5355,
})

# Process name prefixes that are host infrastructure.
_SYSTEM_PROCESS_MARKERS = (
    "sshd", "systemd", "smbd", "nmbd", "dockerd", "containerd", "cron",
    "rsyslog", "dbus", "NetworkManager", "nginx", "tailscaled", "udevd",
    "multipathd", "irqbalance", "polkit", "accounts-daemon", "avahi",
    "postgres", "postmaster", "mysqld", "redis-server", "mongod", "beam.smp",
    "gnome-", "Xorg", "Xvfb", "pipewire", "pulseaudio", "cupsd",
)

# Interactive / IDE noise often has cwd under /apps but is not an abandoned app.
_INTERACTIVE_PROCESS_MARKERS = (
    "bash", "zsh", "fish", "sh", "tmux", "screen", "claude", "code",
    "cursor", "nvim", "vim", "emacs", "less", "tail", "htop", "top",
)


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
    """Backward-compatible reason string (uses classify_orphan_candidate)."""
    return classify_orphan_candidate(res_type, "", "", None, data, compose_images)["reason"]


def classify_orphan_candidate(
    res_type: str,
    key: str,
    display: str,
    path: str | None,
    data: dict | None,
    compose_images: dict[str, str] | None = None,
) -> dict:
    """Classify an unassociated resource for the Orphans page.

    Returns dict:
      bucket: 'actionable' | 'system' | 'expected'
      reason: precise human explanation
      label: short badge text (Actionable / System / Expected)

    *actionable* — real cleanup / review candidates (default Orphans view).
    *system* — OS/vendor infrastructure that is not an abandoned app.
    *expected* — known non-orphan situations (docker builtins, parked configs,
      images still declared by a compose file that isn't running).
    """
    data = data or {}
    key = key or ""
    display = display or key
    path = path or data.get("fragment_path") or data.get("file") or ""

    # ----- Docker networks -----
    if res_type == "network":
        name = (display or key).strip()
        if name in _DOCKER_BUILTIN_NETWORKS or data.get("driver") in ("null", "host"):
            return {
                "bucket": "system",
                "label": "System",
                "reason": f"Docker built-in network '{name}' — always present, not an app leftover",
            }
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": "user-defined network with no containers from a known app attached",
        }

    # ----- systemd units / timers -----
    if res_type in ("systemd_unit", "systemd_timer"):
        is_custom = bool(data.get("is_custom"))
        frag = (data.get("fragment_path") or path or "").strip()
        under_vendor = frag.startswith(("/lib/systemd/", "/usr/lib/systemd/", "/lib/systemd", "/usr/lib/systemd"))
        under_custom = frag.startswith(("/etc/systemd/system", "/home/", "/usr/local/lib/systemd"))
        name_l = (display or key).lower()
        if res_type == "systemd_timer" and any(m in name_l for m in _SYSTEM_TIMER_MARKERS):
            return {
                "bucket": "system",
                "label": "System",
                "reason": "distro/maintenance timer (apt, logrotate, fstrim, …) — not an app orphan",
            }
        if under_vendor or (not is_custom and not under_custom):
            return {
                "bucket": "system",
                "label": "System",
                "reason": "vendor/OS unit (not under /etc/systemd/system) — host infrastructure, not abandoned app",
            }
        # Custom unit still unassociated → real candidate
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": (
                "custom unit not correlated to an app (WorkingDirectory/ExecStart path mismatch)"
                if res_type == "systemd_unit"
                else "custom timer not correlated to a known app unit"
            ),
        }

    # ----- cron -----
    if res_type == "cron_entry":
        # display often like "[daily] logrotate"
        base = display.split("]")[-1].strip() if "]" in display else display
        base = base.split("/")[-1]
        schedule = (data.get("schedule") or data.get("period") or "").lower()
        path_l = (path or key or "").lower()
        if base in _SYSTEM_CRON_BASENAMES or base.startswith("."):
            return {
                "bucket": "system",
                "label": "System",
                "reason": f"OS package cron job '{base}' — expected host maintenance, not an app leftover",
            }
        if "/etc/cron." in path_l and base in _SYSTEM_CRON_BASENAMES:
            return {
                "bucket": "system",
                "label": "System",
                "reason": "system cron.d/cron.daily entry from a distro package",
            }
        # Heuristic: if command text doesn't mention scan roots, often system
        cmd = (data.get("command") or data.get("cmd") or key or "").lower()
        if any(root in cmd for root in ("/apps/", "/data/apps/", "/srv/", "/var/www/")):
            return {
                "bucket": "actionable",
                "label": "Actionable",
                "reason": "cron command references a project path but no app claimed it",
            }
        if path_l.startswith("/etc/cron") and not any(
            root in cmd for root in ("/apps/", "/home/", "/srv/", "/var/www/")
        ):
            return {
                "bucket": "system",
                "label": "System",
                "reason": "system crontab entry with no app project path — host maintenance",
            }
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": ORPHAN_REASONS["cron_entry"],
        }

    # ----- ports -----
    if res_type == "port":
        try:
            port_num = int(data.get("port") if data.get("port") is not None else -1)
        except (TypeError, ValueError):
            port_num = -1
        proc = (data.get("process") or data.get("comm") or "").lower()
        unit = (data.get("systemd_unit") or "").lower()
        if port_num in _SYSTEM_PORTS or port_num in (5432, 3306, 6379, 27017, 11211):
            return {
                "bucket": "system",
                "label": "System",
                "reason": f"well-known host/infra port {port_num} ({proc or 'unknown process'}) — not an abandoned app",
            }
        if any(proc.startswith(m) or m in proc for m in _SYSTEM_PROCESS_MARKERS):
            return {
                "bucket": "system",
                "label": "System",
                "reason": f"listener owned by system process '{data.get('process') or proc}' — not an app leftover",
            }
        if unit and unit.endswith(".service"):
            # Distro DB / infra units
            if any(
                unit.startswith(p) or p in unit
                for p in (
                    "ssh", "smbd", "nmbd", "cron", "nginx", "docker", "containerd",
                    "systemd", "postgres", "mysql", "mariadb", "redis", "fail2ban",
                    "ufw", "unattended",
                )
            ):
                return {
                    "bucket": "system",
                    "label": "System",
                    "reason": f"port owned by system/infra unit {data.get('systemd_unit')}",
                }
            return {
                "bucket": "actionable",
                "label": "Actionable",
                "reason": (
                    f"listener on port {port_num} via {data.get('systemd_unit') or proc or 'unknown'} "
                    "— unit/process not correlated to any DEL application"
                ),
            }
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": f"listening port {port_num if port_num >= 0 else display} not matched to any known app",
        }

    # ----- processes -----
    if res_type == "process":
        comm = (data.get("comm") or data.get("name") or display or "").lower()
        # Strip " (pid N)" style display for matching
        comm_base = comm.split("(")[0].strip()
        if any(comm_base.startswith(m) or m in comm_base for m in _SYSTEM_PROCESS_MARKERS):
            return {
                "bucket": "system",
                "label": "System",
                "reason": f"system/infra process '{display}' — not an abandoned app",
            }
        if any(comm_base.startswith(m) or m in comm_base for m in _INTERACTIVE_PROCESS_MARKERS):
            return {
                "bucket": "system",
                "label": "System",
                "reason": f"interactive/shell/IDE process '{display}' — not an abandoned app service",
            }
        cwd = (data.get("cwd") or path or "").rstrip("/")
        # Require a *project* path (at least /apps/<name>), not bare /apps.
        projectish = False
        for root in ("/apps/", "/data/apps/", "/srv/", "/var/www/", "/opt/"):
            if cwd.startswith(root) and len(cwd) > len(root):
                projectish = True
                break
        if projectish:
            return {
                "bucket": "actionable",
                "label": "Actionable",
                "reason": f"process under project path {cwd} not correlated to an app",
            }
        return {
            "bucket": "system",
            "label": "System",
            "reason": "host process outside a specific app project path — noise, not an app orphan",
        }

    # ----- nginx -----
    if res_type == "nginx_site":
        if not data.get("enabled", False):
            return {
                "bucket": "expected",
                "label": "Expected",
                "reason": (
                    "sites-available copy not enabled — parked/stale config, not a live orphan site"
                    if data.get("stale_copy")
                    else "config present but not enabled (not serving) — review if leftover"
                ),
            }
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": "enabled nginx site with no correlated application — possible leftover or untracked app",
        }

    # ----- images -----
    if res_type == "image":
        if data.get("dangling"):
            return {
                "bucket": "actionable",
                "label": "Actionable",
                "reason": "dangling (untagged) image unused by any container — reclaim candidate",
            }
        repo_tag = _normalize_image_ref(data.get("repo_tag") or display)
        project = (compose_images or {}).get(repo_tag)
        if project:
            return {
                "bucket": "expected",
                "label": "Expected",
                "reason": (
                    f"unused by running containers, but still declared in compose project "
                    f"'{project}' — keep if you may start that app again"
                ),
            }
        using = data.get("containers_using") or []
        if using:
            return {
                "bucket": "expected",
                "label": "Expected",
                "reason": "image is referenced by container(s) but those containers lack an app association",
            }
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": "image not used by any container and not declared by any known compose file",
        }

    # ----- volumes -----
    if res_type == "volume":
        using = data.get("containers_using") or []
        if using:
            return {
                "bucket": "expected",
                "label": "Expected",
                "reason": "volume still attached to container(s) that lack an app association",
            }
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": "named volume with zero containers — possible leftover data volume",
        }

    # ----- filesystem project artifacts -----
    if res_type in ("directory", "git_repo", "env_file", "compose_project"):
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": ORPHAN_REASONS.get(
                res_type,
                "on-disk project artifact not claimed by any application",
            ),
        }

    if res_type == "bind_mount":
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": ORPHAN_REASONS["bind_mount"],
        }

    if res_type == "container":
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": ORPHAN_REASONS["container"],
        }

    if res_type == "tmux_session":
        return {
            "bucket": "actionable",
            "label": "Actionable",
            "reason": ORPHAN_REASONS["tmux_session"],
        }

    return {
        "bucket": "actionable",
        "label": "Actionable",
        "reason": ORPHAN_REASONS.get(res_type, "no matching application found"),
    }


def _is_actionable_orphan(classification: dict) -> bool:
    return classification.get("bucket") == "actionable"


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


# Display timezone for every human-facing datetime in the UI.
# Storage remains UTC (sqlite datetime('now'), Docker Created Z, fs_src ISO-Z).
_DISPLAY_TZ_NAME = "America/New_York"


def _display_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(_DISPLAY_TZ_NAME)


def _parse_dt(value: Any):
    """Parse ISO / sqlite / docker datetime into a timezone-aware UTC datetime.

    Naive values (sqlite `datetime('now')`, space-separated timestamps) are
    treated as UTC — that matches how DEL writes scan/job rows. Aware values
    (Docker `Created`, fs_src `…Z`) are converted to UTC.
    Returns None if unparseable.
    """
    from datetime import datetime, timezone

    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Docker Created often ends with fractional seconds + Z / offset.
    s = s.replace("Z", "+00:00")
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Truncate fractional seconds for "2026-05-13T11:31:00.840175564+00:00"
        # style strings whose fractional part is too long for fromisoformat.
        core = s
        if "." in core:
            head, rest = core.split(".", 1)
            frac = ""
            tz = ""
            for i, ch in enumerate(rest):
                if ch.isdigit():
                    frac += ch
                else:
                    tz = rest[i:]
                    break
            frac = (frac + "000000")[:6]
            try:
                dt = datetime.fromisoformat(f"{head}.{frac}{tz}")
            except ValueError:
                try:
                    dt = datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
        else:
            try:
                dt = datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        # sqlite datetime('now') is UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _to_eastern(value: Any):
    """Timezone-aware America/New_York datetime, or None."""
    dt = _parse_dt(value)
    if dt is None:
        return None
    return dt.astimezone(_display_tz())


def _format_dt(value: Any, *, date_only: bool = False) -> str:
    """Format a datetime for UI display in Eastern (New York).

    Compact style (no timezone suffix — values are always Eastern):
      full:      ``05-13-26 7:31 AM``   (MM-DD-YY H:MM AM/PM)
      date_only: ``05-13-26``
    Returns ``—`` if unparseable. Calendar day follows Eastern, not UTC.
    """
    local = _to_eastern(value)
    if local is None:
        return "—"
    # %-I is glibc (Linux) — hour without leading zero.
    if date_only:
        return local.strftime("%m-%d-%y")
    return local.strftime("%m-%d-%y %-I:%M %p")


def _relative_dt(value: Any) -> str:
    """Short relative age string ('3d ago', '2h ago'), or '' if unparseable."""
    from datetime import datetime, timezone

    dt = _parse_dt(value)
    if dt is None:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 86400 * 45:
        return f"{int(secs // 86400)}d ago"
    if secs < 86400 * 365:
        return f"{int(secs // (86400 * 30))}mo ago"
    return f"{int(secs // (86400 * 365))}y ago"


def _iso_sort_key(value: Any) -> str:
    """UTC ISO string for data-sort-value (stable chronological sort)."""
    dt = _parse_dt(value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _earliest_iso(*values: Any) -> str | None:
    """Return the earliest parseable timestamp as ISO-UTC, or None."""
    best_dt = None
    for v in values:
        dt = _parse_dt(v)
        if dt is None:
            continue
        if best_dt is None or dt < best_dt:
            best_dt = dt
    if best_dt is None:
        return None
    return best_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _installed_at_from_resources(resource_rows: list[dict]) -> str | None:
    """Best-effort install time from associated containers/directories.

    Preference order of *signals* (earliest wins across all of them):
      - container `created` (Docker Created)
      - directory birthtime / ctime / mtime (filesystem)
    Returns ISO-UTC string or None when no signal is available.
    """
    candidates: list[Any] = []
    for r in resource_rows:
        rtype = r.get("type") or r.get("resource_type")
        data = r.get("data") if isinstance(r.get("data"), dict) else None
        if data is None:
            data = _json_or(r.get("data_json") or r.get("resource_data_json"), {})
        if rtype == "container":
            if data.get("created"):
                candidates.append(data["created"])
        elif rtype == "directory":
            for key in ("birthtime", "ctime", "mtime"):
                if data.get(key):
                    candidates.append(data[key])
                    break  # one directory contributes its best single signal
    return _earliest_iso(*candidates)


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


def _duration(started: Any, finished: Any) -> str:
    """Human duration between two ISO/sqlite datetime strings, or '—'."""
    s = _parse_dt(started)
    f = _parse_dt(finished)
    if s is None or f is None:
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


_RECLAIMABLE_CACHE: dict[str, Any] = {"ts": 0.0, "value": 0, "refreshing": False}
_RECLAIMABLE_TTL = 300  # 5 minutes
_RECLAIMABLE_LOCK = threading.Lock()

# A gallery request may need to verify dozens of Nginx endpoints. Keep those
# network checks off the request thread pool and cache the result briefly so
# normal navigation never turns into a thundering herd of HTTPS requests.
_APP_PROBE_CACHE: dict[str, dict[str, Any]] = {}
_APP_PROBE_LOCK = threading.Lock()
_APP_PROBE_TTL = 120
_APP_PROBE_TIMEOUT = 3.0

_CATEGORY_ORDER = [
    "Favorites",
    "AI & Automation",
    "Media & Streaming",
    "Notes & Knowledge",
    "Productivity",
    "Files & Data",
    "Developer Tools",
    "Infrastructure",
    "Security & Identity",
    "Business & Finance",
    "Utilities",
    "Other",
]

_CATEGORY_KEYWORDS = [
    ("Security & Identity", ("auth", "vault", "passbolt", "bitwarden", "identity", "login")),
    ("Media & Streaming", ("flix", "media", "jelly", "stream", "iptv", "tv", "cine", "video", "immich", "photo")),
    ("Notes & Knowledge", ("note", "memo", "docmost", "karakeep", "bookmark", "wiki", "papra", "knowledge", "hearth")),
    ("Business & Finance", ("expense", "wallos", "monize", "invoice", "finance", "nocodb")),
    ("Files & Data", ("file", "share", "stash", "sheet", "database", "libredb", "byte", "openbook")),
    ("AI & Automation", ("ai", "ocr", "agent", "glean", "gongyu", "deep", "poco", "claude", "glm")),
    ("Productivity", ("kan", "board", "task", "plan", "focal", "calendar", "slash", "banban", "lastboard")),
    ("Infrastructure", ("portainer", "dock", "komodo", "netdata", "beszel", "monitor", "speed", "hivedock", "appwrite")),
    ("Developer Tools", ("code", "dev", "git", "query", "api", "draw", "excalidraw", "tldraw", "prettier", "json", "termix")),
    ("Utilities", ("convert", "pdf", "tools", "txt", "b64", "short", "calc", "simple", "tiny")),
]


def _compute_reclaimable_bytes() -> int:
    """Blocking read of docker's reclaimable bytes (images + build cache +
    volumes) via `docker system df`. Called only off the request thread."""
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
    return total


def _refresh_reclaimable_async() -> None:
    def _worker() -> None:
        value = _compute_reclaimable_bytes()
        with _RECLAIMABLE_LOCK:
            _RECLAIMABLE_CACHE["value"] = value
            _RECLAIMABLE_CACHE["ts"] = time.monotonic()
            _RECLAIMABLE_CACHE["refreshing"] = False
    threading.Thread(target=_worker, daemon=True).start()


def _reclaimable_bytes() -> int:
    """Docker's reported reclaimable bytes, cached in-process for 5 minutes.

    Stale-while-revalidate: the dashboard request NEVER blocks on docker. It
    returns the last cached value immediately and, when that value is stale,
    kicks off a single background refresh. Blocking here (a `docker system df`
    that can take seconds, worst right after a removal when docker is busy with
    the post-removal rescan) was the cause of slow / multi-click dashboard
    loads. First load after a restart shows 0 until the async refresh lands."""
    now = time.monotonic()
    with _RECLAIMABLE_LOCK:
        value = _RECLAIMABLE_CACHE["value"]
        stale = (now - _RECLAIMABLE_CACHE["ts"]) >= _RECLAIMABLE_TTL
        trigger = stale and not _RECLAIMABLE_CACHE["refreshing"]
        if trigger:
            _RECLAIMABLE_CACHE["refreshing"] = True
    if trigger:
        _refresh_reclaimable_async()
    return value


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


# The dashboard needs one integer — the actionable-orphan count — but deriving
# it means fetching every unassociated resource and classifying it in Python.
# The inputs only change when a scan completes, so memoise on the scan id.
_ORPHAN_COUNT_CACHE: dict[str, Any] = {"scan": None, "count": 0}
_ORPHAN_COUNT_LOCK = threading.Lock()


def _actionable_orphan_count(conn, latest: int | None) -> int:
    with _ORPHAN_COUNT_LOCK:
        if _ORPHAN_COUNT_CACHE["scan"] == latest:
            return _ORPHAN_COUNT_CACHE["count"]

    sql, params = _orphan_query(latest)
    rows = _rows(q(conn, sql, params))
    compose_images = _compose_declared_images(conn)
    count = 0
    for r in rows:
        cls = classify_orphan_candidate(
            r.get("type") or "",
            r.get("key") or "",
            r.get("display") or "",
            r.get("path"),
            _json_or(r.get("data_json"), {}),
            compose_images,
        )
        if _is_actionable_orphan(cls):
            count += 1

    with _ORPHAN_COUNT_LOCK:
        _ORPHAN_COUNT_CACHE["scan"] = latest
        _ORPHAN_COUNT_CACHE["count"] = count
    return count


def _latest_scan_id(conn) -> int | None:
    """Latest *completed* scan id. Thin wrapper over db.latest_done_scan_id so
    tests can monkeypatch it on this module; both call sites share one
    definition of "latest scan" (see db.latest_done_scan_id for why the
    status filter matters)."""
    return latest_done_scan_id(conn)


def _valid_gallery_domain(value: Any) -> str | None:
    """Return a normalized DNS hostname suitable for the app gallery.

    Nginx may contain wildcard/default server names or literal IPs. The gallery
    is intentionally for human-facing HTTPS domains, so those entries stay out.
    """
    host = str(value or "").strip().lower().rstrip(".")
    if not host or "*" in host or host == "_" or len(host) > 253:
        return None
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        return None
    labels = host.split(".")
    if len(labels) < 2:
        return None
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        return None
    return host


def _gallery_category(slug: str, name: str, domain: str) -> str:
    # Ignore the public suffix (notably `.ai`) so every bjk.ai endpoint does
    # not accidentally land in AI & Automation.
    haystack = " ".join((slug, name, domain.split(".", 1)[0])).lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(
            (bool(re.search(r"(?:^|[-_ ])ai(?:$|[-_ ])", haystack)) if keyword == "ai" else keyword in haystack)
            for keyword in keywords
        ):
            return category
    return "Other"


def _probe_domain(domain: str) -> dict[str, Any]:
    """Verify an HTTPS endpoint without downloading its response body.

    Any HTTP response below 500 proves that DNS, TLS, Nginx routing, and an
    answering upstream are in place. Auth challenges and root-level 404s still
    count as live; 5xx responses do not count as correctly working.
    """
    started = time.monotonic()
    status: int | None = None
    error = ""
    request = urllib.request.Request(
        f"https://{domain}/",
        headers={"User-Agent": "DEL-App-Gallery/1.0", "Accept": "text/html,*/*;q=0.8"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_APP_PROBE_TIMEOUT) as response:
            status = int(response.getcode())
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
    except Exception as exc:
        error = type(exc).__name__
    latency_ms = max(1, round((time.monotonic() - started) * 1000))
    return {
        "healthy": status is not None and 100 <= status < 500,
        "status": status,
        "latency_ms": latency_ms,
        "error": error,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _probe_domains(domains: list[str], *, force: bool = False) -> dict[str, dict[str, Any]]:
    """Return cached/concurrently refreshed health for normalized domains."""
    unique = sorted(set(domains))
    now = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    with _APP_PROBE_LOCK:
        for domain in unique:
            cached = _APP_PROBE_CACHE.get(domain)
            if not force and cached and now - float(cached.get("cached_at", 0)) < _APP_PROBE_TTL:
                results[domain] = dict(cached["result"])
            else:
                pending.append(domain)

    if pending:
        with ThreadPoolExecutor(max_workers=min(16, len(pending))) as pool:
            futures = {pool.submit(_probe_domain, domain): domain for domain in pending}
            for future in as_completed(futures):
                domain = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # defensive: one probe must not break the page
                    result = {
                        "healthy": False,
                        "status": None,
                        "latency_ms": 0,
                        "error": type(exc).__name__,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                results[domain] = result
                with _APP_PROBE_LOCK:
                    _APP_PROBE_CACHE[domain] = {
                        "cached_at": time.monotonic(),
                        "result": dict(result),
                    }
    return results


def _gallery_owner_score(app: dict, domain: str, confidence: Any) -> tuple[int, int, int]:
    """Rank competing correlations for a domain deterministically."""
    slug = str(app.get("slug") or "").lower()
    labels = domain.split(".")
    try:
        base = int(confidence or 0)
    except (TypeError, ValueError):
        base = 0
    exact = int(bool(slug and labels and labels[0] == slug))
    label_hit = int(bool(slug and slug in labels))
    return (exact, label_hit, base)


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
                WHERE a.excluded = 0
                  AND r.type IN ('nginx_site', 'port', 'container', 'directory')
                """,
            )
        )
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


@router.get("/view-apps", response_class=HTMLResponse)
def view_apps(
    request: Request,
    response: Response,
    user: User = Depends(auth.require_user),
) -> HTMLResponse:
    """Homelab-style launcher for verified, currently serving web apps.

    Inventory remains authoritative: candidates must belong to an app and an
    enabled Nginx site in the latest completed scan. A short cached HTTPS probe
    then removes stale, broken, or otherwise unreachable endpoints.
    """
    conn = get_db()
    try:
        latest = _latest_scan_id(conn)
        rows: list[dict] = []
        if latest is not None:
            rows = _rows(
                q(
                    conn,
                    """
                    SELECT ap.id AS app_id, ap.slug, ap.name, ap.status, ap.kind,
                           ap.protected, a.confidence, r.data_json
                    FROM applications ap
                    JOIN associations a ON a.app_id = ap.id AND a.excluded = 0
                    JOIN resources r ON r.id = a.resource_id
                    WHERE ap.last_seen = ? AND r.last_seen = ?
                      AND r.type = 'nginx_site'
                    """,
                    (latest, latest),
                )
            )
    finally:
        conn.close()

    candidates: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        data = _json_or(row.get("data_json"), {})
        if not data.get("enabled", False):
            continue
        for raw_domain in data.get("server_names", []) or []:
            domain = _valid_gallery_domain(raw_domain)
            if not domain:
                continue
            candidate = dict(row)
            candidate["owner_score"] = _gallery_owner_score(candidate, domain, row.get("confidence"))
            candidates.setdefault(domain, []).append(candidate)

    owners: dict[str, dict[str, Any]] = {}
    for domain, possible in candidates.items():
        owners[domain] = max(
            possible,
            key=lambda item: (item["owner_score"], str(item.get("slug") or "")),
        )

    force = request.query_params.get("refresh") == "1"
    probes = _probe_domains(list(owners), force=force)
    apps: list[dict[str, Any]] = []
    for domain, app in owners.items():
        health = probes.get(domain, {})
        if not health.get("healthy"):
            continue
        score = app.get("owner_score") or (0, 0, 0)
        inferred_name = domain.split(".", 1)[0].replace("-", " ").replace("_", " ").title()
        display_name = app.get("name") or app.get("slug") or inferred_name
        if not score[0] and not score[1]:
            display_name = inferred_name
        apps.append(
            {
                "id": f"{app.get('slug')}::{domain}",
                "slug": app.get("slug"),
                "name": display_name,
                "domain": domain,
                "url": f"https://{domain}",
                "icon_url": f"https://{domain}/favicon.ico",
                "initial": str(display_name).strip()[:1].upper() or "?",
                "category": _gallery_category(
                    str(app.get("slug") or ""), str(display_name), domain
                ),
                "status": app.get("status") or "unknown",
                "kind": app.get("kind") or "unknown",
                "protected": bool(app.get("protected")),
                "http_status": health.get("status"),
                "latency_ms": health.get("latency_ms"),
                "checked_at": health.get("checked_at"),
            }
        )

    category_rank = {name: i for i, name in enumerate(_CATEGORY_ORDER)}
    apps.sort(
        key=lambda app: (
            category_rank.get(app["category"], len(category_rank)),
            str(app["name"]).lower(),
            app["domain"],
        )
    )
    categories = [
        category
        for category in _CATEGORY_ORDER
        if category != "Favorites" and any(app["category"] == category for app in apps)
    ]
    checked_at = max((app.get("checked_at") or "" for app in apps), default="")
    return _render(
        "view_apps.html",
        request,
        response,
        apps=apps,
        categories=categories,
        candidate_count=len(owners),
        excluded_count=max(0, len(owners) - len(apps)),
        latest_scan=latest,
        checked_at=checked_at,
        user=user,
    )


@router.get("/apps/{slug}", response_class=HTMLResponse)
def app_detail(
    slug: str, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        rows = q(conn, "SELECT * FROM applications WHERE slug = ?", (slug,))
        latest_scan = _latest_scan_id(conn)
        scan_times = _scan_started_map(conn)
        app = _rows(rows)[0] if rows else {"slug": slug, "name": slug}
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
    required_phrase = getattr(jobs, "CONFIRM_VOLUMES_PHRASE", "y")
    if mode == "live" and has_volume_deletion and confirm_phrase != required_phrase:
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
    """Orphan candidates: unassociated resources, classified for precision.

    Default view = *actionable* only (real review/cleanup candidates).
    Pass ?show=all to include System (OS/infra) and Expected (parked/compose-
    still-declared) rows, each clearly labeled so they are not mistaken for
    abandoned apps.
    """
    show_all = request.query_params.get("show") == "all"
    conn = get_db()
    try:
        latest = _latest_scan_id(conn)
        sql, params = _orphan_query(latest)
        rows = _rows(q(conn, sql, params))
        compose_images = _compose_declared_images(conn)
    finally:
        conn.close()

    classified: list[dict] = []
    counts = {"actionable": 0, "system": 0, "expected": 0}
    for r in rows:
        data = _json_or(r.get("data_json"), {})
        cls = classify_orphan_candidate(
            r.get("type") or "",
            r.get("key") or "",
            r.get("display") or "",
            r.get("path"),
            data,
            compose_images,
        )
        counts[cls["bucket"]] = counts.get(cls["bucket"], 0) + 1
        r = dict(r)
        r["data"] = data
        r["reason"] = cls["reason"]
        r["bucket"] = cls["bucket"]
        r["bucket_label"] = cls["label"]
        if show_all or cls["bucket"] == "actionable":
            classified.append(r)

    groups: dict[str, list] = {}
    for r in classified:
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
        "orphans.html",
        request,
        response,
        grouped=grouped,
        total=len(classified),
        show_all=show_all,
        counts=counts,
        unfiltered_total=len(rows),
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
    try:
        scan_id = scanner.run_scan()
    except Exception as exc:
        # ScanInProgressError or unexpected failure — surface cleanly.
        msg = "Scan+already+in+progress" if "already in progress" in str(exc).lower() else "Scan+failed"
        auditlog.audit(user.id, "scan.error", "scanner", {"error": str(exc)[:200]})
        return RedirectResponse(url=f"/settings?error={msg}", status_code=303)
    auditlog.audit(user.id, "scan.run", "scanner", {"scan_id": scan_id})
    return RedirectResponse(url=f"/settings?flash=Scan+{scan_id}+complete", status_code=303)


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


@router.get("/static/ag-grid-community.min.js")
def static_ag_grid() -> FileResponse:
    """Vendored AG Grid Community (CSP 'self' only — no CDN)."""
    return FileResponse(
        STATIC_DIR / "ag-grid-community.min.js",
        media_type="application/javascript",
    )


# ---------------------------------------------------------------------------
# Jinja globals (template-side formatting helpers)
# ---------------------------------------------------------------------------
templates.env.globals["human_size"] = _human_size
templates.env.globals["level_of"] = _level
templates.env.globals["duration"] = _duration
templates.env.globals["format_dt"] = _format_dt
templates.env.globals["relative_dt"] = _relative_dt
templates.env.globals["iso_sort"] = _iso_sort_key
templates.env.globals["resource_labels"] = RESOURCE_TYPE_LABELS
