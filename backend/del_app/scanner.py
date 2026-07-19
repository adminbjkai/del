"""Top-level discovery+correlation orchestrator: collects from every discovery
source (each independently try/except'd so one failing source doesn't abort
the scan), correlates into apps/associations, and persists everything into the
scans/applications/resources/associations tables described in
docs/ARCHITECTURE.md. Returns the new scan id.
"""
from __future__ import annotations

import json
import logging
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
    and return the new scan id."""
    started = time.time()
    conn = db.get_db()
    try:
        scan_id = db.x(conn, "INSERT INTO scans (status) VALUES ('running')")

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
        conn.commit()

        stats = {
            "duration_seconds": round(time.time() - started, 1),
            "resources_total": len(resources),
            "resources_by_source": per_source_counts,
            "apps_total": app_count,
            "associations_total": assoc_count,
        }
        conn.execute(
            "UPDATE scans SET finished=datetime('now'), status='done', stats_json=? WHERE id=?",
            (json.dumps(stats), scan_id),
        )
        conn.commit()
        return scan_id
    except Exception:
        logger.exception("scanner: run_scan failed")
        try:
            conn.execute(
                "UPDATE scans SET finished=datetime('now'), status='failed' WHERE id=?", (scan_id,)
            )
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()
