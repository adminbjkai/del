"""Top-level discovery+correlation orchestrator: collects from every discovery
source (each independently try/except'd so one failing source doesn't abort
the scan), correlates into apps/associations, and persists everything into the
scans/applications/resources/associations tables described in
docs/ARCHITECTURE.md. Returns the new scan id.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from del_app import db
from del_app.correlate import build_apps
from del_app.discovery import (
    compose_src, cron_src, docker_src, fs_src, nginx_src, proc_src, systemd_src,
)
from del_app.manifests import load_all
from del_app.models import Resource

logger = logging.getLogger("del_app.scanner")

SOURCES = [
    ("docker", docker_src.collect),
    ("compose", compose_src.collect),
    ("nginx", nginx_src.collect),
    ("systemd", systemd_src.collect),
    ("proc", proc_src.collect),
    ("cron", cron_src.collect),
    ("fs", fs_src.collect),
]

# Only one scan at a time per process. Prevents concurrent run_scan() from
# interleaving resource last_seen updates and leaving orphan 'running' rows.
_scan_lock = threading.Lock()


class ScanInProgressError(RuntimeError):
    """Raised when run_scan is called while another scan is already running."""


def abandon_stale_scans(reason: str = "abandoned: process restart or crash mid-scan") -> int:
    """Mark every scan still status='running' as failed.

    Scans insert a 'running' row at start; if the process is restarted (systemd
    restart, deploy, OOM) that row never gets finished. Call this on app
    startup so Settings/dashboard never show ghost in-progress scans.
    Returns the number of rows updated.
    """
    conn = db.get_db()
    try:
        stats = json.dumps({"abandoned": True, "reason": reason})
        cur = conn.execute(
            "UPDATE scans SET status = 'failed', finished = datetime('now'), "
            "stats_json = ? WHERE status = 'running'",
            (stats,),
        )
        n = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        conn.commit()
        if n:
            logger.warning("scanner: abandoned %s stale running scan(s): %s", n, reason)
        return n
    finally:
        conn.close()


def _collect_all() -> tuple[list[Resource], dict[str, int]]:
    resources: list[Resource] = []
    per_source_counts: dict[str, int] = {}
    for name, collect_fn in SOURCES:
        try:
            found = collect_fn()
        except Exception:
            logger.exception("scanner: source %s failed entirely", name)
            found = []
        per_source_counts[name] = len(found)
        resources.extend(found)
    return resources, per_source_counts


def run_scan() -> int:
    """Collect all sources, correlate, persist apps/resources/associations,
    and return the new scan id.

    Raises ScanInProgressError if another scan is already running in this process.
    """
    if not _scan_lock.acquire(blocking=False):
        raise ScanInProgressError("a scan is already in progress")

    started = time.time()
    scan_id: int | None = None
    conn = db.get_db()
    try:
        # Safety: any leftover 'running' rows from a prior crash block clarity
        # in the UI even though inventory uses status='done' only.
        conn.execute(
            "UPDATE scans SET status = 'failed', finished = datetime('now'), "
            "stats_json = ? WHERE status = 'running'",
            (json.dumps({"abandoned": True, "reason": "superseded by new scan"}),),
        )
        conn.commit()

        cur = conn.execute("INSERT INTO scans (status) VALUES ('running')")
        scan_id = cur.lastrowid
        conn.commit()

        resources, per_source_counts = _collect_all()

        manifests = {}
        try:
            manifests = load_all()
        except Exception:
            logger.exception("scanner: load_all manifests failed")

        try:
            apps = build_apps(resources, manifests)
        except Exception:
            logger.exception("scanner: build_apps failed")
            apps = []

        resource_ids: dict[tuple[str, str], int] = {}
        for r in resources:
            existing = db.q(conn, "SELECT id FROM resources WHERE type=? AND key=?", (r.type, r.key))
            data_json = json.dumps(r.data, default=str)
            if existing:
                rid = existing[0]["id"]
                conn.execute(
                    "UPDATE resources SET display=?, path=?, state=?, data_json=?, last_seen=? WHERE id=?",
                    (r.display, r.path, r.state, data_json, scan_id, rid),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO resources (type, key, display, path, state, data_json, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (r.type, r.key, r.display, r.path, r.state, data_json, scan_id, scan_id),
                )
                rid = cur.lastrowid
            resource_ids[(r.type, r.key)] = rid
        conn.commit()

        app_count = 0
        assoc_count = 0
        for record, associations in apps:
            existing_app = db.q(conn, "SELECT id FROM applications WHERE slug=?", (record.slug,))
            if existing_app:
                app_id = existing_app[0]["id"]
                conn.execute(
                    "UPDATE applications SET name=?, status=?, kind=?, protected=?, last_seen=? WHERE id=?",
                    (record.name, record.status, record.kind, int(record.protected), scan_id, app_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO applications (slug, name, status, kind, protected, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (record.slug, record.name, record.status, record.kind, int(record.protected), scan_id, scan_id),
                )
                app_id = cur.lastrowid
            app_count += 1

            # Replace this app's associations with the freshly correlated set.
            conn.execute("DELETE FROM associations WHERE app_id=?", (app_id,))
            for a in associations:
                rid = resource_ids.get((a.resource_type, a.resource_key))
                if rid is None:
                    continue
                conn.execute(
                    "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared, "
                    "data_loss_risk, removal_eligible, recommended_action, evidence_json, source, excluded) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        app_id, rid, a.confidence, a.ownership, int(a.shared),
                        a.data_loss_risk, a.removal_eligible, a.recommended_action,
                        json.dumps([e.model_dump() for e in a.evidence]),
                        "correlate", int(a.excluded),
                    ),
                )
                assoc_count += 1

        # Drop associations belonging to applications that no longer exist in
        # this scan. Only apps that were re-correlated above had their rows
        # replaced, so an app removed from the host kept its associations
        # forever — and because those rows point at resources that are still
        # live, they went on claiming ownership of them. That both hid real
        # leftovers from the Orphans page and left resources looking "shared"
        # with a ghost, which blocks a clean removal of the surviving app.
        cur = conn.execute(
            "DELETE FROM associations WHERE app_id IN "
            "(SELECT id FROM applications WHERE last_seen < ?)",
            (scan_id,),
        )
        stale_assoc_removed = cur.rowcount or 0
        conn.commit()

        stats = {
            "duration_seconds": round(time.time() - started, 1),
            "resources_total": len(resources),
            "resources_by_source": per_source_counts,
            "apps_total": app_count,
            "associations_total": assoc_count,
            "stale_associations_removed": stale_assoc_removed,
        }
        conn.execute(
            "UPDATE scans SET finished=datetime('now'), status='done', stats_json=? WHERE id=?",
            (json.dumps(stats), scan_id),
        )
        conn.commit()
        return int(scan_id)
    except Exception:
        logger.exception("scanner: run_scan failed")
        if scan_id is not None:
            try:
                conn.execute(
                    "UPDATE scans SET finished=datetime('now'), status='failed', "
                    "stats_json=? WHERE id=?",
                    (json.dumps({"error": "run_scan raised", "duration_seconds": round(time.time() - started, 1)}),
                     scan_id),
                )
                conn.commit()
            except Exception:
                pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
        _scan_lock.release()
