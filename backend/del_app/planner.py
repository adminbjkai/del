"""Removal plan generation (dry-run) — see docs/ARCHITECTURE.md and
docs/INTERFACES.md for the authoritative stage order and safety rules.

build_plan() loads an app + its associated resources from the DB and turns
each removal-eligible association into one or more PlanStep entries, grouped
into the six fixed stages: backup, quiesce, remove_runtime, remove_host,
remove_files, validate. Anything not safely removable is recorded as a
warning + preserved resource instead of a step.

Plans are persisted with an HMAC (keyed by the same secret used for session
signing, see del_app.auth.get_secret_key) computed over the canonical JSON of
their steps, so a tampered steps_json row is detected before execution.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import os
import re
import sqlite3

from del_app.auth import get_secret_key
from del_app.config import get_settings
from del_app.db import get_db, latest_done_scan_id, q, x
from del_app.models import Plan, PlanStep

STAGE_ORDER = [
    "backup",
    "quiesce",
    "remove_runtime",
    "remove_host",
    "remove_files",
    "validate",
]

# Never deletable, even if listed in a plan — docs/ARCHITECTURE.md "Protected roots".
PROTECTED_ROOTS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt",
    "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr",
    "/var", "/apps", "/data", "/apps/del",
}

DEFAULT_NETWORKS = {"bridge", "host", "none"}

CONFIG_RESOURCE_TYPES = {"compose_project", "env_file", "nginx_site", "systemd_unit", "cron_entry"}


class PlanError(Exception):
    """Raised when a plan cannot be built or an existing plan fails
    integrity verification (tamper guard)."""


class PlanNotFoundError(PlanError):
    """Raised when a requested plan id does not exist."""


def _level_from_confidence(confidence: int, source: str | None) -> str:
    """Map a numeric confidence + source to a confidence level per
    docs/ARCHITECTURE.md "Confidence scoring"."""
    if source == "manual":
        return "manual"
    if confidence >= 95:
        return "confirmed"
    if confidence >= 80:
        return "high"
    if confidence >= 60:
        return "probable"
    if confidence >= 30:
        return "possible"
    return "unrelated"


def _data(row: sqlite3.Row) -> dict:
    raw = row["data_json"] if "data_json" in row.keys() else None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def _check_stage_order(steps: list[PlanStep]) -> None:
    """Raise PlanError if step stages are not non-decreasing per STAGE_ORDER.

    STAGE_ORDER is documented as authoritative (docs/ARCHITECTURE.md); a plan
    whose steps regress to an earlier stage (e.g. a remove_files step before a
    quiesce step) would execute host mutations out of the safe order.
    """
    rank = {stage: i for i, stage in enumerate(STAGE_ORDER)}
    last = -1
    for step in steps:
        r = rank.get(step.stage)
        if r is None:
            raise PlanError(f"unknown plan stage: {step.stage!r} (seq={step.seq})")
        if r < last:
            raise PlanError(
                f"plan step seq={step.seq} stage={step.stage!r} violates STAGE_ORDER "
                f"(follows a later stage)"
            )
        last = r


def _canonical_steps_json(steps: list[PlanStep]) -> str:
    payload = [s.model_dump() for s in steps]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_hmac(steps: list[PlanStep]) -> str:
    key = get_secret_key()
    canonical = _canonical_steps_json(steps)
    return hmac_mod.new(key, canonical.encode(), hashlib.sha256).hexdigest()


def _is_protected_root(path: str) -> bool:
    real = os.path.realpath(path)
    return real in PROTECTED_ROOTS


# The helper reads its policy from the ROOT-OWNED deployed copy. The planner
# mirrors that policy so it never proposes a path the helper would refuse, so
# it must read the same file — reading the repo copy meant a hand-edit to one
# could silently desynchronise the planner from the allowlist actually being
# enforced. The repo path stays as a fallback for a dev checkout with no
# deployed helper.
_POLICY_PATHS = ("/etc/del/helper-policy.json", "/apps/del/config/helper-policy.json")


def _helper_policy() -> dict:
    for path in _POLICY_PATHS:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _approved_deletion_roots() -> list[str]:
    """Mirror the helper policy's approved deletion roots so the planner never
    proposes a path the helper would (correctly) refuse — e.g. host system
    bind mounts like /var/run/docker.sock, /etc/localtime, /proc."""
    return _helper_policy().get("approved_deletion_roots") or [
        "/apps", "/data", "/srv", "/var/www", "/home/bjkai",
    ]


def _never_delete_roots() -> list[str]:
    return _helper_policy().get("never_delete") or ["/apps/del"]


def _scan_root_app_dir(path: str | None) -> str | None:
    """If path sits under a scan root, return `{root}/{first-component}`.

    Used as a planner safety belt: a systemd unit or process whose
    WorkingDirectory/ExecStart/cwd resolves to `/apps/glmflix` must never be
    included in a plan for a different app (e.g. netdata).
    """
    if not path or not isinstance(path, str):
        return None
    try:
        roots = [r.rstrip("/") for r in get_settings().scan_roots]
    except Exception:
        roots = ["/apps", "/data/apps", "/opt", "/srv", "/var/www"]
    for root in roots:
        prefix = root + "/"
        if path.startswith(prefix):
            name = path[len(prefix):].split("/", 1)[0]
            if name:
                return f"{root}/{name}"
    return None


def _first_abs_path_in(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"(/(?:apps|data|opt|srv|var/www)/[A-Za-z0-9_.-]+(?:/[^\s;]*)?)", text)
    return m.group(1) if m else None


def _owned_app_dirs(app_slug: str, assoc_rows: list) -> set[str]:
    """Project directories this app is allowed to act on."""
    dirs: set[str] = set()
    try:
        roots = [r.rstrip("/") for r in get_settings().scan_roots]
    except Exception:
        roots = ["/apps", "/data/apps", "/opt", "/srv", "/var/www"]
    for root in roots:
        dirs.add(f"{root}/{app_slug}")
    for row in assoc_rows:
        rtype = row["resource_type"]
        path = row["resource_path"]
        if rtype in ("directory", "compose_project", "git_repo") and path:
            owned = _scan_root_app_dir(path)
            if owned:
                dirs.add(owned)
    return dirs


def _resource_foreign_to_app(data: dict, owned_dirs: set[str], *, cwd: str | None = None) -> bool:
    """True only when we can prove this lives under a *different* project.

    Unknown (no scan-root path) is not foreign — correlation already gated
    the association; we only block the glmflix-in-a-netdata-plan case.
    """
    wd = data.get("working_directory")
    exe = data.get("exec_start") or data.get("exe") or ""
    candidate = (
        _scan_root_app_dir(wd)
        or _scan_root_app_dir(cwd)
        or _scan_root_app_dir(exe if isinstance(exe, str) and exe.startswith("/") else None)
        or _scan_root_app_dir(_first_abs_path_in(exe if isinstance(exe, str) else ""))
    )
    if candidate is None:
        return False
    return candidate not in owned_dirs


def _is_safe_delete_path(path: str | None) -> bool:
    """A path is only deletable if absolute, resolves to a real filesystem
    entry, is not a protected root, is not on the never-delete list, and (like
    the helper) resolves strictly under an approved deletion root at least one
    component deep."""
    if not path or not os.path.isabs(path):
        return False
    real = os.path.realpath(path)
    if not os.path.exists(real):
        return False
    if _is_protected_root(real) or _is_protected_root(path):
        return False
    for nd in _never_delete_roots():
        r = os.path.realpath(nd).rstrip("/")
        if real == r or real.startswith(r + "/"):
            return False
    for root in _approved_deletion_roots():
        r = root.rstrip("/")
        if real == r:  # the root itself is never deletable
            return False
        if real.startswith(r + "/"):
            return True
    return False


class _SeqCounter:
    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


def _classify(row: sqlite3.Row) -> tuple[str, bool, str | None]:
    """Return (level, is_step, warning) for an association row.

    Only confirmed/high/manual associations become steps, and only when not
    excluded and not (shared and unapproved). probable is always a warning
    (never a step). possible/unrelated/blocked/shared-unapproved are
    preserved with a warning.
    """
    level = _level_from_confidence(row["confidence"], row["source"])
    shared = bool(row["shared"])
    approved = bool(row["approved_by_user"])
    excluded = bool(row["excluded"])
    removal_eligible = row["removal_eligible"]

    if excluded:
        return level, False, "excluded by user"

    if removal_eligible == "blocked":
        return level, False, "blocked: not safely removable"

    if level == "probable":
        return level, False, "requires per-resource approval"

    if level in ("confirmed", "high", "manual"):
        if shared and not approved:
            return level, False, "shared resource requires per-resource approval"
        return level, True, None

    # possible / unrelated
    return level, False, "confidence too low for automatic removal"


def _compose_project_name(row, data: dict, app_slug: str) -> str:
    """Project name for a compose_down step.

    `compose_down` tears the project down by config file and then sweeps any
    straggler by Docker label (`docker ps -aq --filter
    label=com.docker.compose.project=<project>` followed by `docker rm -f`).
    That sweep is host-wide, so the name must never be a generic directory
    basename: a compose file at /apps/<app>/docker/ used to yield the project
    name "docker", which would force-remove every container on the host
    labelled with that project — other apps included. Prefer an explicitly
    declared name, then the directory basename, and fall back to the app slug
    when the basename is only a layout role. The config-file teardown still
    handles the real project correctly either way.
    """
    from del_app.correlate import _GENERIC_DIR_BASENAMES, _slugify

    declared = data.get("project") or data.get("declared_name")
    if declared:
        return str(declared)
    path = row["resource_path"]
    if path and str(path).startswith("/"):
        base = os.path.basename(str(path).rstrip("/"))
        if base and _slugify(base) not in _GENERIC_DIR_BASENAMES:
            return base
        return app_slug
    return row["resource_key"]


def build_plan(app_slug: str, options: dict) -> Plan:
    """Build a removal plan for app_slug. Raises PlanError for protected
    apps or if the app cannot be found."""
    settings = get_settings()
    conn = get_db()
    try:
        app_rows = q(conn, "SELECT * FROM applications WHERE slug = ?", (app_slug,))
        if not app_rows:
            raise PlanError(f"no such application: {app_slug}")
        app = app_rows[0]
        # Single source of truth for scan scoping (see db.latest_done_scan_id).
        latest_scan = latest_done_scan_id(conn)

        if bool(app["protected"]) or app_slug in settings.protected_apps:
            raise PlanError(f"application '{app_slug}' is protected; refusing to plan removal")

        assoc_rows = q(
            conn,
            """
            SELECT a.*, r.type AS resource_type, r.key AS resource_key,
                   r.display AS resource_display, r.path AS resource_path,
                   r.data_json AS data_json
            FROM associations a
            JOIN resources r ON r.id = a.resource_id
            WHERE a.app_id = ?
              AND (r.last_seen = ? OR ? IS NULL)
            """,
            (app["id"], latest_scan, latest_scan),
        )
        if not assoc_rows:
            # No current resources at all — whether the app has stale
            # associations from an earlier scan or none ever. Either way the
            # only plan we could build is a lone validate step, which executes
            # as "success" having removed nothing. Refuse instead.
            had_any = bool(q(
                conn, "SELECT 1 FROM associations WHERE app_id = ? LIMIT 1", (app["id"],)
            ))
            detail = (
                "its resources were last seen in an earlier scan"
                if had_any
                else "no resources have ever been associated with it"
            )
            raise PlanError(
                f"application '{app_slug}' has no resources in the latest completed "
                f"scan ({detail}); re-scan before planning a removal"
            )
    finally:
        conn.close()

    warnings: list[str] = []
    preserved: list[str] = []
    manual_followup: list[str] = []
    est_reclaim_bytes = 0

    step_rows: list[sqlite3.Row] = []
    for row in assoc_rows:
        level, is_step, warning = _classify(row)
        if is_step:
            step_rows.append(row)
        else:
            preserved.append(row["resource_key"])
            if warning:
                warnings.append(f"{row['resource_key']}: {warning}")

    by_type: dict[str, list[sqlite3.Row]] = {}
    for row in step_rows:
        by_type.setdefault(row["resource_type"], []).append(row)

    seq = _SeqCounter()
    steps: list[PlanStep] = []

    backup_mode = options.get("backup", "none")
    remove_named_volumes = bool(options.get("remove_named_volumes", False))
    remove_images = options.get("remove_images", "none")
    remove_bind_data = bool(options.get("remove_bind_data", False))
    remove_repo = bool(options.get("remove_repo", False))
    remove_networks = bool(options.get("remove_networks", True))

    backups_dir = settings.backups_dir

    # --- Stage: backup ---
    if backup_mode in ("config", "full"):
        backed_up: set[str] = set()
        for row in step_rows:
            if row["resource_type"] not in CONFIG_RESOURCE_TYPES or not row["resource_path"]:
                continue
            # A compose_project's path is its directory; back up its actual
            # config files (and .env) instead — file_backup requires files.
            paths: list[str] = []
            if os.path.isdir(row["resource_path"]):
                data = _data(row)
                for cf in data.get("config_files") or []:
                    if os.path.isfile(cf):
                        paths.append(cf)
                env = os.path.join(row["resource_path"], ".env")
                if os.path.isfile(env):
                    paths.append(env)
            elif os.path.isfile(row["resource_path"]):
                paths.append(row["resource_path"])
            else:
                warnings.append(f"{row['resource_key']}: config path missing, skipping backup")
            for pth in paths:
                if pth in backed_up:
                    continue
                backed_up.add(pth)
                # The destination must be unique per SOURCE, not per basename.
                # Keying only on basename made two different files with the
                # same name (e.g. /apps/x/server/compose.yaml and
                # /apps/x/server/config/compose.yaml, or several
                # docker-compose.yml under one app) share one destination:
                # `cp -a` silently overwrote, both paths were recorded in the
                # backups table pointing at that single file, and a rollback
                # then wrote the survivor's contents back over BOTH originals.
                # Measured across the live inventory: 190 colliding
                # destinations, 21 of them with genuinely different content.
                # The parent directory is flattened into a sub-directory so the
                # basename still matches the original — path_restore requires
                # that, and it is what makes the restore target unambiguous.
                parent = os.path.dirname(pth).strip("/").replace("/", "_") or "root"
                dest = (
                    f"{backups_dir}/{app_slug}/{row['resource_type']}/"
                    f"{parent}/{os.path.basename(pth)}"
                )
                steps.append(PlanStep(
                    seq=seq.next(), stage="backup", operation="file_backup",
                    args={"path": pth, "dest": dest},
                    description=f"Back up config file {pth} before removal",
                    reversible=True, danger="safe",
                ))
        if backup_mode == "full":
            for row in by_type.get("volume", []):
                dest = f"{backups_dir}/{app_slug}/volumes/{row['resource_key']}.tar"
                steps.append(PlanStep(
                    seq=seq.next(), stage="backup", operation="volume_backup",
                    args={"volume": row["resource_key"], "dest": dest},
                    description=f"Back up named volume {row['resource_key']}",
                    reversible=True, danger="safe",
                ))
            for row in by_type.get("bind_mount", []):
                data = _data(row)
                dest = f"{backups_dir}/{app_slug}/bind_mounts/{row['resource_key']}.tar"
                steps.append(PlanStep(
                    seq=seq.next(), stage="backup", operation="backup_tar",
                    args={"src_path": row["resource_path"], "dest": dest},
                    description=f"Back up bind-mount data at {row['resource_path']}",
                    reversible=True, danger="safe",
                ))
                size = data.get("size_bytes")
                if isinstance(size, int):
                    est_reclaim_bytes += size

    # --- Stage: quiesce ---
    # timers first (so a stopped service is not immediately re-triggered),
    # then services; skip units whose files no longer exist on disk.
    owned_dirs = _owned_app_dirs(app_slug, assoc_rows)
    systemd_rows = by_type.get("systemd_timer", []) + by_type.get("systemd_unit", [])
    live_systemd_rows = []
    for row in systemd_rows:
        unit = row["resource_key"]
        if not os.path.exists(os.path.join("/etc/systemd/system", unit)):
            warnings.append(f"{unit}: unit file already absent, skipping systemd steps")
            continue
        data = _data(row)
        if _resource_foreign_to_app(data, owned_dirs):
            preserved.append(unit)
            warnings.append(
                f"{unit}: WorkingDirectory/ExecStart is not under this app, skipping")
            continue
        live_systemd_rows.append(row)
        steps.append(PlanStep(
            seq=seq.next(), stage="quiesce", operation="systemd_stop",
            args={"unit": unit},
            description=f"Stop systemd unit {unit}",
            reversible=True, danger="safe",
        ))
    for row in by_type.get("tmux_session", []):
        steps.append(PlanStep(
            seq=seq.next(), stage="quiesce", operation="tmux_kill",
            args={"session": row["resource_key"]},
            description=f"Kill tmux session {row['resource_key']}",
            reversible=False, danger="warning",
        ))
    for row in by_type.get("process", []):
        data = _data(row)
        pid = data.get("pid")
        exe = data.get("exe")
        if not isinstance(pid, int) or not exe:
            # can't safely terminate without a verified pid+exe pair (matches
            # what the helper's process_term precondition requires); preserve
            # instead of silently dropping the association.
            preserved.append(row["resource_key"])
            warnings.append(
                f"{row['resource_key']}: process pid/exe unavailable, skipping termination")
            continue
        if _resource_foreign_to_app(data, owned_dirs, cwd=data.get("cwd")):
            preserved.append(row["resource_key"])
            warnings.append(
                f"{row['resource_key']}: process cwd/exe is not under this app, skipping")
            continue
        steps.append(PlanStep(
            seq=seq.next(), stage="quiesce", operation="process_term",
            args={"pid": pid, "expected_exe": exe},
            description=f"Terminate process {row['resource_display'] or row['resource_key']}",
            reversible=False, danger="warning",
        ))
    for row in by_type.get("container", []):
        steps.append(PlanStep(
            seq=seq.next(), stage="quiesce", operation="container_stop",
            args={"container_id": row["resource_key"]},
            description=f"Stop container {row['resource_display'] or row['resource_key']}",
            reversible=True, danger="safe",
        ))

    # --- Stage: remove_runtime ---
    compose_rows = by_type.get("compose_project", [])
    if compose_rows:
        # compose_down's label sweep (`docker ps -aq --filter
        # label=com.docker.compose.project=<name>`) is host-wide by project
        # NAME, not by directory. If another app's compose project resolves
        # to the same name under a different path, warn the operator before
        # execution so they know that sweep may also hit the other app's
        # containers.
        conn_collision = get_db()
        try:
            other_compose_rows = q(
                conn_collision,
                """
                SELECT ap.slug AS app_slug, r.path AS resource_path,
                       r.data_json AS data_json
                FROM associations a
                JOIN resources r ON r.id = a.resource_id
                JOIN applications ap ON ap.id = a.app_id
                WHERE r.type = 'compose_project' AND ap.slug != ?
                """,
                (app_slug,),
            )
        finally:
            conn_collision.close()
        for row in compose_rows:
            this_name = _compose_project_name(row, _data(row), app_slug)
            this_path = row["resource_path"]
            for orow in other_compose_rows:
                other_data = _data(orow)
                other_name = _compose_project_name(orow, other_data, orow["app_slug"])
                if other_name == this_name and orow["resource_path"] != this_path:
                    warnings.append(
                        f"compose project name '{this_name}' collision: {this_path} "
                        f"(this app) and {orow['resource_path']} (app '{orow['app_slug']}') "
                        f"share the same docker-compose project label; compose_down's "
                        f"host-wide label sweep may remove the other app's containers too"
                    )
        for row in compose_rows:
            data = _data(row)
            steps.append(PlanStep(
                seq=seq.next(), stage="remove_runtime", operation="compose_down",
                args={
                    "project": _compose_project_name(row, data, app_slug),
                    "config_files": data.get("config_files", []),
                    "remove_volumes": False,
                    "remove_images_mode": remove_images if remove_images == "exclusive" else "none",
                },
                description=f"docker compose down for project {row['resource_key']}",
                reversible=False, danger="warning",
            ))
    else:
        for row in by_type.get("container", []):
            steps.append(PlanStep(
                seq=seq.next(), stage="remove_runtime", operation="container_rm",
                args={"container_id": row["resource_key"]},
                description=f"Remove container {row['resource_display'] or row['resource_key']}",
                reversible=False, danger="warning",
            ))

    if remove_networks:
        app_container_ids = [r["resource_key"] for r in by_type.get("container", [])]
        for row in by_type.get("network", []):
            if row["resource_key"] in DEFAULT_NETWORKS:
                continue
            if bool(row["shared"]):
                continue
            steps.append(PlanStep(
                seq=seq.next(), stage="remove_runtime", operation="network_rm",
                args={"network_name": row["resource_key"],
                      "allowed_container_ids": app_container_ids},
                description=f"Remove app-exclusive network {row['resource_key']}",
                reversible=False, danger="warning",
            ))

    if remove_named_volumes:
        # Approval comes from EITHER the per-volume checkboxes on the plan
        # form (options.approved_volumes) or the association's approve button.
        form_approved = set(options.get("approved_volumes") or [])
        for row in by_type.get("volume", []):
            if not (bool(row["approved_by_user"]) or row["resource_key"] in form_approved):
                preserved.append(row["resource_key"])
                warnings.append(f"{row['resource_key']}: volume removal not approved, preserving")
                continue
            data = _data(row)
            steps.append(PlanStep(
                seq=seq.next(), stage="remove_runtime", operation="volume_rm",
                args={"volume_name": row["resource_key"]},
                description=f"Delete named volume {row['resource_key']} (DATA LOSS)",
                reversible=False, danger="data_loss",
            ))
            size = data.get("size_bytes")
            if isinstance(size, int):
                est_reclaim_bytes += size
    else:
        for row in by_type.get("volume", []):
            preserved.append(row["resource_key"])
            if backup_mode != "full":
                warnings.append(f"{row['resource_key']}: volume preserved (remove_named_volumes not set)")

    if remove_images == "exclusive":
        for row in by_type.get("image", []):
            if bool(row["shared"]):
                preserved.append(row["resource_key"])
                warnings.append(f"{row['resource_key']}: image shared with other apps, preserving")
                continue
            data = _data(row)
            steps.append(PlanStep(
                seq=seq.next(), stage="remove_runtime", operation="image_rm",
                args={"image_id": row["resource_key"],
                      "allowed_container_ids": [r["resource_key"] for r in by_type.get("container", [])]},
                description=f"Remove image {row['resource_display'] or row['resource_key']}",
                reversible=False, danger="warning",
            ))
            size = data.get("size_bytes")
            if isinstance(size, int):
                est_reclaim_bytes += size

    # --- Stage: remove_host ---
    for row in live_systemd_rows:
        steps.append(PlanStep(
            seq=seq.next(), stage="remove_host", operation="systemd_disable",
            args={"unit": row["resource_key"]},
            description=f"Disable systemd unit {row['resource_key']}",
            reversible=False, danger="warning",
        ))
        steps.append(PlanStep(
            seq=seq.next(), stage="remove_host", operation="systemd_rm_unit",
            args={"unit": row["resource_key"], "daemon_reload": True},
            description=f"Remove systemd unit file for {row['resource_key']}",
            reversible=False, danger="warning",
        ))

    for row in by_type.get("cron_entry", []):
        data = _data(row)
        steps.append(PlanStep(
            seq=seq.next(), stage="remove_host", operation="cron_rm",
            args={"path": row["resource_path"] or data.get("line", row["resource_key"])},
            description=f"Remove cron entry {row['resource_key']}",
            reversible=False, danger="warning",
        ))

    # Collect every nginx path for this app into ONE removal step, ordered
    # sites-enabled first: removing a sites-available target before its
    # enabled symlink leaves a dangling symlink and fails nginx -t.
    nginx_paths: list[str] = []
    nginx_names: list[str] = []
    for row in by_type.get("nginx_site", []):
        data = _data(row)
        paths = data.get("paths") or ([row["resource_path"]] if row["resource_path"] else [])
        for p in paths:
            if p and os.path.lexists(p) and p not in nginx_paths:
                nginx_paths.append(p)
            elif p and not os.path.lexists(p):
                warnings.append(f"{p}: nginx config already absent, skipping")
        nginx_names.append(row["resource_key"])
    if nginx_paths:
        nginx_paths.sort(key=lambda p: 0 if "/sites-enabled/" in p else 1)
        steps.append(PlanStep(
            seq=seq.next(), stage="remove_host", operation="nginx_rm_site",
            args={"paths": nginx_paths},
            description=f"Remove nginx site(s) {', '.join(sorted(set(nginx_names)))}",
            reversible=True, danger="warning",
        ))
        steps.append(PlanStep(
            seq=seq.next(), stage="remove_host", operation="nginx_test_reload",
            args={},
            description="Test nginx config and reload",
            reversible=True, danger="safe",
        ))

    # --- Stage: remove_files ---
    # Collect candidate paths first so we can drop duplicates and paths nested
    # inside another path already being deleted (deleting the parent first
    # would make the nested child step fail with "path does not exist").
    delete_candidates: list[tuple[str, str, int]] = []  # (path, label, size)
    if remove_repo:
        for row in by_type.get("directory", []) + by_type.get("git_repo", []):
            path = row["resource_path"]
            if not _is_safe_delete_path(path):
                warnings.append(f"{row['resource_key']}: path not safely deletable, skipping")
                preserved.append(row["resource_key"])
                continue
            size = _data(row).get("size_bytes")
            delete_candidates.append((path, "project directory", size if isinstance(size, int) else 0))
    if remove_bind_data:
        for row in by_type.get("bind_mount", []):
            path = row["resource_path"]
            if not _is_safe_delete_path(path):
                warnings.append(f"{row['resource_key']}: bind mount path not safely deletable, skipping")
                preserved.append(row["resource_key"])
                continue
            size = _data(row).get("size_bytes")
            delete_candidates.append((path, "bind-mount data", size if isinstance(size, int) else 0))

    seen_paths: set[str] = set()
    all_paths = {c[0].rstrip("/") for c in delete_candidates if c[0]}
    for path, label, size in delete_candidates:
        norm = (path or "").rstrip("/")
        if not norm or norm in seen_paths:
            continue
        parent = any(other != norm and norm.startswith(other + "/") for other in all_paths)
        if parent:
            # covered by an ancestor's deletion; no separate step needed
            continue
        seen_paths.add(norm)
        steps.append(PlanStep(
            seq=seq.next(), stage="remove_files", operation="path_delete",
            args={"path": path},
            description=f"Delete {label} {path} (DATA LOSS)",
            reversible=False, danger="data_loss",
        ))
        est_reclaim_bytes += size

    # --- Stage: validate ---
    steps.append(PlanStep(
        seq=seq.next(), stage="validate", operation="validate_removal",
        args={"app_slug": app_slug},
        description="Run post-removal validation checks",
        reversible=True, danger="safe",
    ))

    for row in by_type.get("nginx_site", []):
        data = _data(row)
        for d in data.get("domains", []):
            manual_followup.append(f"Remove DNS record for {d}")

    _check_stage_order(steps)

    plan = Plan(
        app_slug=app_slug,
        options=options,
        steps=steps,
        warnings=warnings,
        preserved=sorted(set(preserved)),
        manual_followup=manual_followup,
        est_reclaim_bytes=est_reclaim_bytes,
    )
    return plan


def persist_plan(plan: Plan, conn: sqlite3.Connection | None = None) -> int:
    """Persist a Plan to the plans table with an HMAC over its canonical
    steps JSON. Returns the new plan id and sets plan.id."""
    own_conn = conn is None
    conn = conn or get_db()
    try:
        app_rows = q(conn, "SELECT id FROM applications WHERE slug = ?", (plan.app_slug,))
        if not app_rows:
            raise PlanError(f"no such application: {plan.app_slug}")
        app_id = app_rows[0]["id"]

        _check_stage_order(plan.steps)

        steps_json = _canonical_steps_json(plan.steps)
        digest = compute_hmac(plan.steps)
        options_with_meta = dict(plan.options)
        options_with_meta["_meta"] = {
            "warnings": plan.warnings,
            "preserved": plan.preserved,
            "manual_followup": plan.manual_followup,
            "est_reclaim_bytes": plan.est_reclaim_bytes,
        }
        plan_id = x(
            conn,
            """
            INSERT INTO plans (app_id, options_json, steps_json, status, hmac)
            VALUES (?, ?, ?, 'draft', ?)
            """,
            (app_id, json.dumps(options_with_meta), steps_json, digest),
        )
        plan.id = plan_id
        return plan_id
    finally:
        if own_conn:
            conn.close()


def load_plan(plan_id: int, conn: sqlite3.Connection | None = None) -> tuple[Plan, str, str]:
    """Load a persisted plan. Returns (Plan, stored_hmac, recomputed_hmac).
    Callers must compare the two hmac values (or call verify_plan) before
    treating the plan as trustworthy."""
    own_conn = conn is None
    conn = conn or get_db()
    try:
        rows = q(
            conn,
            """
            SELECT p.*, a.slug AS app_slug
            FROM plans p JOIN applications a ON a.id = p.app_id
            WHERE p.id = ?
            """,
            (plan_id,),
        )
        if not rows:
            raise PlanNotFoundError(f"no such plan: {plan_id}")
        row = rows[0]
        steps_data = json.loads(row["steps_json"])
        steps = [PlanStep(**s) for s in steps_data]
        options = json.loads(row["options_json"]) if row["options_json"] else {}
        meta = options.pop("_meta", {}) or {}
        plan = Plan(
            id=row["id"],
            app_slug=row["app_slug"],
            options=options,
            steps=steps,
            warnings=meta.get("warnings", []),
            preserved=meta.get("preserved", []),
            manual_followup=meta.get("manual_followup", []),
            est_reclaim_bytes=meta.get("est_reclaim_bytes", 0),
        )
        recomputed = compute_hmac(steps)
        return plan, row["hmac"], recomputed
    finally:
        if own_conn:
            conn.close()


def verify_plan(plan_id: int, conn: sqlite3.Connection | None = None) -> Plan:
    """Load a plan and raise PlanError if its stored HMAC does not match a
    fresh HMAC recomputed over its steps (tamper guard)."""
    plan, stored, recomputed = load_plan(plan_id, conn)
    if not stored or not hmac_mod.compare_digest(stored, recomputed):
        raise PlanError(f"plan {plan_id} failed integrity check (tampered steps_json)")
    return plan
