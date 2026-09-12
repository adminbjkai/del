"""Scanner stale-scan abandonment + single-flight lock."""
from __future__ import annotations

import json
import threading

import pytest

from del_app import scanner
from del_app.config import get_settings
from del_app.db import get_db, q, run_migrations, x

pytestmark = pytest.mark.real_scanner


@pytest.fixture()
def settings_env(tmp_path, monkeypatch):
    db_path = tmp_path / "del.db"
    config_path = tmp_path / "del.toml"
    config_path.write_text(
        f"""
port = 8075
db_path = "{db_path}"
manifests_dir = "{tmp_path}/manifests"
backups_dir = "{tmp_path}/backups"
logs_dir = "{tmp_path}/logs"
scan_roots = ["{tmp_path}"]
helper_socket = "{tmp_path}/helper.sock"
protected_apps = ["del"]
"""
    )
    monkeypatch.setenv("DEL_CONFIG_PATH", str(config_path))
    get_settings.cache_clear()
    run_migrations()
    # Ensure process-wide lock is free (other tests may have used run_scan).
    if scanner._scan_lock.locked():
        try:
            scanner._scan_lock.release()
        except RuntimeError:
            pass
    yield get_settings()
    get_settings.cache_clear()
    if scanner._scan_lock.locked():
        try:
            scanner._scan_lock.release()
        except RuntimeError:
            pass


def test_abandon_stale_scans_marks_running_failed(settings_env):
    conn = get_db()
    try:
        a = x(conn, "INSERT INTO scans (status) VALUES ('running')")
        b = x(conn, "INSERT INTO scans (status) VALUES ('running')")
        done = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        conn.execute("UPDATE scans SET finished=datetime('now') WHERE id=?", (done,))
        conn.commit()
    finally:
        conn.close()

    n = scanner.abandon_stale_scans("test abandon")
    assert n == 2

    conn = get_db()
    try:
        rows = {r["id"]: dict(r) for r in q(conn, "SELECT id, status, finished, stats_json FROM scans")}
    finally:
        conn.close()
    assert rows[a]["status"] == "failed"
    assert rows[b]["status"] == "failed"
    assert rows[a]["finished"]
    assert rows[done]["status"] == "done"
    stats = json.loads(rows[a]["stats_json"])
    assert stats.get("abandoned") is True


def test_run_scan_rejects_concurrent(settings_env, monkeypatch):
    """Second concurrent run_scan must raise ScanInProgressError."""
    entered = threading.Event()
    release = threading.Event()

    def slow_collect():
        entered.set()
        # Hold long enough that the concurrent caller always hits the lock.
        assert release.wait(timeout=10)
        return []

    monkeypatch.setattr(scanner, "_collect_all", lambda: (slow_collect(), {}))
    monkeypatch.setattr(scanner, "load_all", lambda: {})
    monkeypatch.setattr(scanner, "build_apps", lambda resources, manifests: [])

    errors: list[BaseException] = []
    result_ids: list[int] = []

    def runner():
        try:
            result_ids.append(scanner.run_scan())
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=runner)
    t.start()
    assert entered.wait(timeout=5), "slow_collect never started"
    # Hold the lock: second call must fail immediately.
    with pytest.raises(scanner.ScanInProgressError):
        scanner.run_scan()
    release.set()
    t.join(timeout=10)
    assert not errors, f"background scan failed: {errors}"
    assert len(result_ids) == 1


def test_scan_state_reports_running_and_idle(settings_env, monkeypatch):
    assert scanner.scan_state() == {"running": False, "scan_id": None, "started": None}

    entered = threading.Event()
    release = threading.Event()

    def slow_collect():
        entered.set()
        assert release.wait(timeout=10)
        return []

    monkeypatch.setattr(scanner, "_collect_all", lambda: (slow_collect(), {}))
    monkeypatch.setattr(scanner, "load_all", lambda: {})
    monkeypatch.setattr(scanner, "build_apps", lambda resources, manifests: [])

    result_ids: list[int] = []

    def runner():
        result_ids.append(scanner.run_scan())

    t = threading.Thread(target=runner)
    t.start()
    assert entered.wait(timeout=5), "slow_collect never started"

    state = scanner.scan_state()
    assert state["running"] is True
    assert isinstance(state["scan_id"], int)
    assert state["started"]

    release.set()
    t.join(timeout=10)
    assert result_ids

    assert scanner.scan_state() == {"running": False, "scan_id": None, "started": None}


def test_run_scan_abandons_prior_running_before_insert(settings_env, monkeypatch):
    monkeypatch.setattr(scanner, "_collect_all", lambda: ([], {}))
    monkeypatch.setattr(scanner, "load_all", lambda: {})
    monkeypatch.setattr(scanner, "build_apps", lambda resources, manifests: [])

    conn = get_db()
    try:
        stale = x(conn, "INSERT INTO scans (status) VALUES ('running')")
        conn.commit()
    finally:
        conn.close()

    new_id = scanner.run_scan()
    conn = get_db()
    try:
        stale_row = dict(q(conn, "SELECT status FROM scans WHERE id=?", (stale,))[0])
        new_row = dict(q(conn, "SELECT status FROM scans WHERE id=?", (new_id,))[0])
    finally:
        conn.close()
    assert stale_row["status"] == "failed"
    assert new_row["status"] == "done"


def test_run_scan_marks_unseen_apps_removed(settings_env, monkeypatch):
    monkeypatch.setattr(scanner, "_collect_all", lambda: ([], {}))
    monkeypatch.setattr(scanner, "load_all", lambda: {})
    monkeypatch.setattr(scanner, "build_apps", lambda resources, manifests: [])

    conn = get_db()
    try:
        prior = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, protected, first_seen, last_seen) "
            "VALUES ('ghost-app', 'ghost-app', 'running', 'compose', 0, ?, ?)",
            (prior, prior),
        )
        conn.commit()
    finally:
        conn.close()

    scanner.run_scan()
    conn = get_db()
    try:
        row = dict(q(conn, "SELECT status, last_seen FROM applications WHERE slug='ghost-app'")[0])
    finally:
        conn.close()
    assert row["status"] == "removed"
    assert row["last_seen"] == 1
