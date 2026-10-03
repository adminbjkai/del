"""Top-level discovery+correlation orchestrator: collects from every discovery
source, rejects scans with failed sources, correlates into apps/associations,
and atomically publishes everything into the
scans/applications/resources/associations tables described in
docs/ARCHITECTURE.md. Returns the new scan id.
"""
from __future__ import annotations

import fcntl
import json
import logging
import threading
import time
from pathlib import Path

from del_app import db
from del_app.config import get_settings
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


def scan_state() -> dict:
    """Return the current scan state for the web worker or admin CLI.

    Reports scans started by either the web worker or the admin CLI. The
    in-process lock also covers the brief interval before a scan row exists.
    Returns {"running": False, "scan_id": None, "started": None}
    when no scan is running.
    """
    running = _scan_lock.locked()
    conn = db.get_db()
    try:
        rows = db.q(
            conn,
            "SELECT id, started FROM scans WHERE status = 'running' ORDER BY id DESC LIMIT 1",
        )
    finally:
        conn.close()
    if not rows:
        return {"running": running, "scan_id": None, "started": None}
    if not running:
        try:
            handle = _lock_scan_file()
        except ScanInProgressError:
            pass
        else:
            handle.close()
            # A crashed CLI can leave a running row without an active worker.
            return {"running": False, "scan_id": None, "started": None}
    return {"running": True, "scan_id": rows[0]["id"], "started": rows[0]["started"]}


def _lock_scan_file():
    """Linux advisory lock shared by the web worker and admin CLI."""
    path = Path(get_settings().db_path).with_suffix(".scan.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ScanInProgressError("a scan is already in progress") from None
    except BaseException:
        handle.close()
        raise
    return handle


def _scan_ages() -> tuple[float | None, float | None]:
    """(seconds since the latest completed scan finished, seconds since the
    latest attempt of any status started); None when there is none. Both are
    0 while a removal job runs, because a removal ends with its own rescan."""
    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT "
            "(SELECT CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', finished) AS INTEGER) "
            " FROM scans WHERE status = 'done' ORDER BY id DESC LIMIT 1) AS done_age, "
            "(SELECT CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', started) AS INTEGER) "
            " FROM scans ORDER BY id DESC LIMIT 1) AS attempt_age, "
            "EXISTS (SELECT 1 FROM jobs WHERE status = 'running') AS busy"
        ).fetchone()
    finally:
        conn.close()
    if row["busy"]:
        return 0.0, 0.0
    as_float = lambda v: None if v is None else float(v)  # noqa: E731
    return as_float(row["done_age"]), as_float(row["attempt_age"])


def start_scheduler(interval_hours: float, stop: threading.Event, check_seconds: float = 600) -> threading.Thread | None:
    """Keep the inventory fresh: every check_seconds, scan if the latest
    completed scan is older than interval_hours. After a failed attempt it
    waits min(interval, 1 h) before trying again. Off when interval_hours <= 0."""
    if interval_hours <= 0:
        return None
    limit = interval_hours * 3600
    retry = min(limit, 3600)

    def _loop() -> None:
        while not stop.wait(check_seconds):
            try:
                done_age, attempt_age = _scan_ages()
                stale = done_age is None or done_age >= limit
                cooling = attempt_age is not None and attempt_age < retry
                if stale and not cooling and not scan_state().get("running"):
                    logger.info("scheduler: inventory is %s old; scanning",
                                "unknown" if done_age is None else f"{done_age:.0f}s")
                    run_scan()
            except ScanInProgressError:
                pass
            except Exception:
                logger.exception("scheduler: scheduled scan failed; previous inventory retained")

    thread = threading.Thread(target=_loop, name="del-scan-scheduler", daemon=True)
    thread.start()
    return thread


def abandon_stale_scans(reason: str = "abandoned: process restart or crash mid-scan") -> int:
    """Mark every scan still status='running' as failed.

    Scans insert a 'running' row at start; if the process is restarted (systemd
    restart, deploy, OOM) that row never gets finished. Call this on app
    startup so Settings/dashboard never show ghost in-progress scans.
    Returns the number of rows updated.
    """
    try:
        scan_file = _lock_scan_file()
    except ScanInProgressError:
        return 0  # another process is actively scanning, not abandoned
    conn = None
    try:
        conn = db.get_db()
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
        if conn is not None:
            conn.close()
        scan_file.close()


def _collect_all() -> tuple[list[Resource], dict[str, int]]:
    resources: list[Resource] = []
    per_source_counts: dict[str, int] = {}
    failed: list[str] = []
    for name, collect_fn in SOURCES:
        try:
            found = collect_fn()
        except Exception:
            logger.exception("scanner: source %s failed entirely", name)
            failed.append(name)
            found = []
        per_source_counts[name] = len(found)
        resources.extend(found)
    if failed:
        raise RuntimeError("discovery sources failed: " + ", ".join(failed))
    return resources, per_source_counts


def run_scan() -> int:
    """Collect all sources, correlate, persist apps/resources/associations,
    and return the new scan id.

    Raises ScanInProgressError if another scan is already running in this process.
    """
    if not _scan_lock.acquire(blocking=False):
        raise ScanInProgressError("a scan is already in progress")

    started = time.monotonic()
    scan_id: int | None = None
    conn = None
    scan_file = None
    try:
        scan_file = _lock_scan_file()
        conn = db.get_db()
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

        # Fail closed: correlation/manifest failures must never publish an
        # empty inventory and mark real apps removed.
        manifests = load_all(strict=True)
        apps = build_apps(resources, manifests)

        # Acquire the write lock only after host discovery. WAL readers keep
        # seeing the complete previous inventory until this transaction commits.
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO resources (type, key, display, path, state, data_json, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(type, key) DO UPDATE SET display=excluded.display, path=excluded.path, "
            "state=excluded.state, data_json=excluded.data_json, last_seen=excluded.last_seen",
            ((r.type, r.key, r.display, r.path, r.state, json.dumps(r.data, default=str), scan_id, scan_id)
             for r in resources),
        )
        resource_ids = {
            (row["type"], row["key"]): row["id"]
            for row in conn.execute("SELECT id, type, key FROM resources WHERE last_seen=?", (scan_id,))
        }
        app_ids = {
            row["slug"]: row["id"]
            for row in conn.execute("SELECT id, slug FROM applications")
        }
        reviews = {
            (row["app_id"], row["resource_id"]): dict(row)
            for row in conn.execute(
                "SELECT app_id, resource_id, approved_by_user, user_excluded, user_shared, "
                "confidence, ownership, shared, data_loss_risk, removal_eligible "
                "FROM associations WHERE approved_by_user = 1 OR user_excluded = 1 OR user_shared = 1"
            )
        }


        app_count = 0
        assoc_count = 0
        for record, associations in apps:
            manifest = manifests.get(record.slug)
            manifest_path = manifest._source_path if manifest is not None else None
            app_id = app_ids.get(record.slug)
            if app_id is not None:
                conn.execute(
                    "UPDATE applications SET name=?, status=?, kind=?, protected=?, manifest_path=?, "
                    "last_seen=? WHERE id=?",
                    (record.name, record.status, record.kind, int(record.protected), manifest_path,
                     scan_id, app_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO applications (slug, name, status, kind, protected, manifest_path, "
                    "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (record.slug, record.name, record.status, record.kind, int(record.protected),
                     manifest_path, scan_id, scan_id),
                )
                app_id = cur.lastrowid
                app_ids[record.slug] = app_id
            app_count += 1

            # Replace this app's associations with the freshly correlated set.
            conn.execute("DELETE FROM associations WHERE app_id=?", (app_id,))
            for a in associations:
                rid = resource_ids.get((a.resource_type, a.resource_key))
                if rid is None:
                    continue
                review = reviews.get((app_id, rid), {})
                shared = int(a.shared or review.get("user_shared", 0))
                # An approval is not permission to remove a newly shared or
                # riskier resource. Keep it only while the safety classification
                # is unchanged; exclusions/shared protections remain in force.
                safety = (a.confidence, a.ownership, shared, a.data_loss_risk, a.removal_eligible)
                prior_safety = tuple(review.get(field) for field in (
                    "confidence", "ownership", "shared", "data_loss_risk", "removal_eligible",
                ))
                approved = int(bool(review.get("approved_by_user")) and safety == prior_safety)
                conn.execute(
                    "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared, "
                    "data_loss_risk, removal_eligible, recommended_action, evidence_json, source, excluded, "
                    "approved_by_user, user_excluded, user_shared) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        app_id, rid, a.confidence, a.ownership, shared,
                        a.data_loss_risk, a.removal_eligible, a.recommended_action,
                        json.dumps([e.model_dump() for e in a.evidence]),
                        "correlate", int(a.excluded or review.get("user_excluded", 0)),
                        approved, review.get("user_excluded", 0),
                        review.get("user_shared", 0),
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
        # Keep the row for history, but stop advertising a gone app as running.
        conn.execute(
            "UPDATE applications SET status='removed' WHERE last_seen < ? AND status != 'removed'",
            (scan_id,),
        )

        stats = {
            "duration_seconds": round(time.monotonic() - started, 1),
            "resources_total": len(resources),
            "resources_by_source": per_source_counts,
            "apps_total": app_count,
            "associations_total": assoc_count,
            "stale_associations_removed": stale_assoc_removed,
            # Lets the dashboard show status changes between two scans.
            "app_status": {record.slug: record.status for record, _ in apps},
        }
        conn.execute(
            "UPDATE scans SET finished=datetime('now'), status='done', stats_json=? WHERE id=?",
            (json.dumps(stats), scan_id),
        )
        conn.commit()
        return int(scan_id)
    except ScanInProgressError:
        raise
    except Exception as exc:
        logger.exception("scanner: run_scan failed")
        if conn is not None:
            conn.rollback()
        if scan_id is not None:
            try:
                conn.execute(
                    "UPDATE scans SET finished=datetime('now'), status='failed', "
                    "stats_json=? WHERE id=?",
                    (json.dumps({"error": str(exc), "duration_seconds": round(time.monotonic() - started, 1)}),
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
        if scan_file is not None:
            scan_file.close()
        _scan_lock.release()
