"""Orphan classification (heuristics for 'unassociated resource' triage) and
the /orphans route."""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from del_app import auth
from del_app.auth import User
from del_app.correlate import _DOCKER_BUILTIN_NETWORKS
from del_app.db import get_db, q
from del_app.web.queries import (
    ALL_RESOURCE_TYPES,
    RESOURCE_TYPE_LABELS,
    _compose_declared_images,
    _json_or,
    _latest_scan_id,
    _orphan_query,
    _rows,
)
from del_app.web.render import _render

router = APIRouter()

_ORPHAN_COUNT_LOCK = threading.Lock()

# The dashboard needs one integer — the actionable-orphan count — but deriving
# it means fetching every unassociated resource and classifying it in Python.
# The inputs only change when a scan completes, so memoise on the scan id.
_ORPHAN_COUNT_CACHE: dict = {"scan": None, "count": 0}

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

# Unit/timer basenames that are distro or vendor infrastructure, not app
# leftovers. Applied to BOTH units and timers: snapd writes its generated
# units into /etc/systemd/system, which is exactly the "custom" location the
# classifier uses to decide something is app-owned, so every snap unit showed
# up as an actionable orphan.
_SYSTEM_UNIT_MARKERS = (
    "anacron", "apport", "apt-", "dpkg-", "e2scrub", "fstrim", "fwupd",
    "logrotate", "man-db", "motd-", "systemd-", "ua-", "update-notifier",
    "phpsessionclean", "certbot", "snap.", "snapd", "avahi", "ollama",
    "pm2-", "docker-", "pmcd", "pmlogger", "nxserver", "cups", "bluetooth",
    "packagekit", "unattended-upgrade", "networkd", "resolvconf",
)

# Listener ports that are almost always OS / infra, not abandoned apps.
_SYSTEM_PORTS = frozenset({
    22, 25, 53, 67, 68, 111, 123, 137, 138, 139, 445, 631, 2049, 5353, 5355,
})

# Process name prefixes that are host infrastructure. MUST be lowercase: the
# classifier lowercases the process name before matching, so mixed-case
# entries here (previously "NetworkManager", "Xorg", "Xvfb") could never match
# and those processes were reported as actionable orphans.
_SYSTEM_PROCESS_MARKERS = (
    "sshd", "systemd", "smbd", "nmbd", "dockerd", "containerd", "cron",
    "rsyslog", "dbus", "networkmanager", "nginx", "tailscaled", "udevd",
    "multipathd", "irqbalance", "polkit", "accounts-daemon", "avahi",
    "postgres", "postmaster", "mysqld", "redis-server", "mongod", "beam.smp",
    "gnome-", "xorg", "xvfb", "pipewire", "pulseaudio", "cupsd",
    "ollama", "pmcd", "pmlogger", "nxserver", "snapd", "packagekit",
)

# Interactive / IDE / agent noise often has cwd under /apps but is not an
# abandoned app service. Also lowercase-only, for the same reason.
_INTERACTIVE_PROCESS_MARKERS = (
    "bash", "zsh", "fish", "sh", "tmux", "screen", "claude", "code",
    "cursor", "nvim", "vim", "emacs", "less", "tail", "htop", "top",
    "node", "npm", "npx", "pnpm", "yarn", "bun", "deno",
    "python", "python3", "uv", "uvx", "ruby", "java",
    "grok", "codex", "gemini", "mainthread", "adb", "qemu",
)


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
        if any(m in name_l for m in _SYSTEM_UNIT_MARKERS):
            kind = "timer" if res_type == "systemd_timer" else "unit"
            return {
                "bucket": "system",
                "label": "System",
                "reason": (
                    f"distro/vendor {kind} (apt, logrotate, snap, ollama, …) — "
                    "host infrastructure, not an app orphan"
                ),
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
                    "ufw", "unattended", "ollama", "pmcd", "pmlogger", "nxserver",
                    "pm2-", "snap.", "avahi", "cups",
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
        # A catch-all / default vhost has no server_name (or only `_`) and no
        # upstream: it exists to reject unknown Host headers, not to serve an
        # app, so it is never a leftover.
        names = [n for n in (data.get("server_names") or []) if n and n != "_"]
        if not names or not (data.get("upstreams") or []):
            return {
                "bucket": "system",
                "label": "System",
                "reason": (
                    "default/catch-all vhost (no server_name or no upstream) — "
                    "rejects unknown hosts, not an app site"
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
        from del_app.web.queries import _normalize_image_ref

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
    # Jump-nav needs every type (including zero-count ones, dimmed) for the
    # current view (actionable-only, or all with ?show=all).
    type_summary = [
        {"type": t, "label": RESOURCE_TYPE_LABELS.get(t, t), "count": len(groups.get(t, []))}
        for t in ALL_RESOURCE_TYPES
    ]
    return _render(
        "orphans.html",
        request,
        response,
        grouped=grouped,
        type_summary=type_summary,
        total=len(classified),
        show_all=show_all,
        counts=counts,
        unfiltered_total=len(rows),
    )
