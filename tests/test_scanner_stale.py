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
    monkeypatch.setattr(scanner, "load_all", lambda **kwargs: {})
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
    monkeypatch.setattr(scanner, "load_all", lambda **kwargs: {})
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
    monkeypatch.setattr(scanner, "load_all", lambda **kwargs: {})
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
    monkeypatch.setattr(scanner, "load_all", lambda **kwargs: {})
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


def _inventory_fixture(monkeypatch):
    from del_app.models import AppRecord, Association, Resource

    resources = [Resource(type="directory", key="/apps/example", display="Example", state="found")]
    association = Association(
        resource_type="directory", resource_key="/apps/example", confidence=95,
        level="high", ownership="exclusive", data_loss_risk="data",
        removal_eligible="safe", recommended_action="review",
    )
    apps = [(AppRecord(slug="example", name="Example", status="running", kind="native"), [association])]
    monkeypatch.setattr(scanner, "_collect_all", lambda: (resources, {"fs": len(resources)}))
    monkeypatch.setattr(scanner, "load_all", lambda **kwargs: {})
    monkeypatch.setattr(scanner, "build_apps", lambda resources, manifests: apps)
    return resources, apps


def _inventory_snapshot():
    conn = get_db()
    try:
        return {
            table: [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("resources", "applications", "associations")
        }
    finally:
        conn.close()


@pytest.mark.parametrize("failure", ["collection", "manifest", "correlation", "persistence"])
def test_failed_scan_preserves_entire_previous_inventory(settings_env, monkeypatch, failure):
    resources, apps = _inventory_fixture(monkeypatch)
    prior = scanner.run_scan()
    before = _inventory_snapshot()

    def fail(*args, **kwargs):
        raise RuntimeError("injected scan failure")

    if failure == "collection":
        monkeypatch.setattr(scanner, "_collect_all", fail)
    elif failure == "manifest":
        monkeypatch.setattr(scanner, "load_all", fail)
    elif failure == "correlation":
        monkeypatch.setattr(scanner, "build_apps", fail)
    else:
        # Fails after the resources and applications have already been updated.
        resources[0].display = "Changed"
        apps[0][0].name = "Changed"
        conn = get_db()
        try:
            conn.execute("CREATE TRIGGER reject_association BEFORE INSERT ON associations "
                         "BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END")
            conn.commit()
        finally:
            conn.close()

    with pytest.raises(Exception, match="injected"):
        scanner.run_scan()
    assert _inventory_snapshot() == before
    conn = get_db()
    try:
        assert scanner.db.latest_done_scan_id(conn) == prior
        last = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        assert last["status"] == "failed" and last["finished"]
        assert "injected" in json.loads(last["stats_json"])["error"]
    finally:
        conn.close()
    assert not scanner._scan_lock.locked()


def test_collection_rejects_failed_source_but_checks_remaining_sources(monkeypatch):
    checked = []

    def bad():
        raise RuntimeError("docker unavailable")

    def good():
        checked.append(True)
        return []

    monkeypatch.setattr(scanner, "SOURCES", [("docker", bad), ("fs", good)])
    with pytest.raises(RuntimeError, match="discovery sources failed: docker"):
        scanner._collect_all()
    assert checked == [True]


def test_scan_connection_failure_releases_lock(settings_env, monkeypatch):
    def unavailable():
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as mp:
        mp.setattr(scanner.db, "get_db", unavailable)
        with pytest.raises(RuntimeError, match="database unavailable"):
            scanner.run_scan()
    assert not scanner._scan_lock.locked()
    _inventory_fixture(monkeypatch)
    assert scanner.run_scan() > 0


def test_scan_file_lock_guards_cli_and_startup(settings_env, monkeypatch):
    _inventory_fixture(monkeypatch)
    conn = get_db()
    try:
        active = x(conn, "INSERT INTO scans (status) VALUES ('running')")
    finally:
        conn.close()
    # A separate file descriptor is enough to exercise Linux's flock conflict,
    # just as a separate CLI process would; do not hold the Python thread lock.
    handle = scanner._lock_scan_file()
    try:
        assert scanner.scan_state()["scan_id"] == active
        assert scanner.abandon_stale_scans() == 0
        with pytest.raises(scanner.ScanInProgressError):
            scanner.run_scan()
        assert not scanner._scan_lock.locked()
    finally:
        handle.close()
    assert scanner.run_scan() > active


@pytest.mark.parametrize("review", ["approve", "exclude", "mark-shared"])
def test_scan_preserves_operator_review(settings_env, monkeypatch, review):
    _inventory_fixture(monkeypatch)
    scanner.run_scan()
    conn = get_db()
    try:
        setters = {
            "approve": "approved_by_user=1",
            "exclude": "excluded=1, user_excluded=1",
            "mark-shared": "shared=1, user_shared=1",
        }
        conn.execute(f"UPDATE associations SET {setters[review]}")
        conn.commit()
    finally:
        conn.close()
    scanner.run_scan()
    row = _inventory_snapshot()["associations"][0]
    if review == "approve":
        assert row["approved_by_user"] == 1
    elif review == "exclude":
        assert row["excluded"] == row["user_excluded"] == 1
    else:
        assert row["shared"] == row["user_shared"] == 1


def test_scan_recomputes_inferred_shared_flag(settings_env, monkeypatch):
    _, apps = _inventory_fixture(monkeypatch)
    apps[0][1][0].shared = True
    scanner.run_scan()
    apps[0][1][0].shared = False
    scanner.run_scan()
    assert _inventory_snapshot()["associations"][0]["shared"] == 0


def test_readers_see_previous_inventory_until_publication(settings_env, monkeypatch):
    resources, apps = _inventory_fixture(monkeypatch)
    scanner.run_scan()
    before = _inventory_snapshot()
    resources[0].display = "New display"
    apps[0][0].name = "New name"
    original_dump = apps[0][1][0].model_dump
    # Evidence serialization occurs after writes have begun. Observe from a
    # separate reader while the writer's transaction is still open.
    from del_app.models import Evidence
    evidence = Evidence(source="test", statement="observed", weight=95)
    apps[0][1][0].evidence = [evidence]
    dump = Evidence.model_dump

    def observe(self, *args, **kwargs):
        assert _inventory_snapshot() == before
        return dump(self, *args, **kwargs)

    monkeypatch.setattr(Evidence, "model_dump", observe)
    assert original_dump()  # model remains valid
    scanner.run_scan()
    assert _inventory_snapshot()["applications"][0]["name"] == "New name"


def test_invalid_manifest_cannot_publish_scan(settings_env, monkeypatch):
    _inventory_fixture(monkeypatch)
    scanner.run_scan()
    before = _inventory_snapshot()
    from pathlib import Path
    from del_app.manifests import load_all
    root = Path(settings_env.manifests_dir)
    root.mkdir()
    (root / "example.yaml").write_text("id: ../unsafe\n")
    monkeypatch.setattr(scanner, "load_all", load_all)
    with pytest.raises(ValueError, match="invalid manifest: example.yaml"):
        scanner.run_scan()
    assert _inventory_snapshot() == before


def test_scan_state_does_not_block_new_scan_after_cli_crash(settings_env):
    conn = get_db()
    try:
        x(conn, "INSERT INTO scans (status) VALUES ('running')")
    finally:
        conn.close()
    assert scanner.scan_state()["running"] is False


@pytest.mark.parametrize("changed", ["shared", "confidence", "data_loss_risk", "removal_eligible"])
def test_scan_revokes_approval_if_safety_classification_changes(settings_env, monkeypatch, changed):
    _, apps = _inventory_fixture(monkeypatch)
    scanner.run_scan()
    conn = get_db()
    try:
        conn.execute("UPDATE associations SET approved_by_user=1")
        conn.commit()
    finally:
        conn.close()
    values = {"shared": True, "confidence": 60, "data_loss_risk": "none", "removal_eligible": "blocked"}
    setattr(apps[0][1][0], changed, values[changed])
    scanner.run_scan()
    assert _inventory_snapshot()["associations"][0]["approved_by_user"] == 0


def _scheduler_runs(monkeypatch, interval_hours: float, setup_sql: list[str]) -> int:
    """Run the scheduler loop briefly against a seeded scans/jobs table and
    count how many scans it starts."""
    conn = get_db()
    try:
        for sql in setup_sql:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()
    calls = []
    monkeypatch.setattr(scanner, "run_scan", lambda: calls.append(1) or 0)
    stop = threading.Event()
    thread = scanner.start_scheduler(interval_hours, stop, check_seconds=0.02)
    if thread is None:
        return -1
    stop.wait(0.15)
    stop.set()
    thread.join(2)
    return len(calls)


def test_scheduler_is_off_by_default(settings_env, monkeypatch):
    assert settings_env.scan_interval_hours == 0
    assert _scheduler_runs(monkeypatch, 0, []) == -1


def test_scheduler_scans_a_stale_inventory(settings_env, monkeypatch):
    old = ("INSERT INTO scans (status, started, finished) VALUES "
           "('done', datetime('now','-8 hours'), datetime('now','-8 hours'))")
    assert _scheduler_runs(monkeypatch, 6, [old]) >= 1


def test_scheduler_leaves_a_fresh_inventory_alone(settings_env, monkeypatch):
    fresh = ("INSERT INTO scans (status, started, finished) VALUES "
             "('done', datetime('now','-1 hours'), datetime('now','-1 hours'))")
    assert _scheduler_runs(monkeypatch, 6, [fresh]) == 0


def test_scheduler_waits_after_a_recent_failed_attempt(settings_env, monkeypatch):
    old = ("INSERT INTO scans (status, started, finished) VALUES "
           "('done', datetime('now','-8 hours'), datetime('now','-8 hours'))")
    failed = ("INSERT INTO scans (status, started, finished) VALUES "
              "('failed', datetime('now','-10 minutes'), datetime('now','-9 minutes'))")
    assert _scheduler_runs(monkeypatch, 6, [old, failed]) == 0


def test_scheduler_skips_while_a_removal_job_runs(settings_env, monkeypatch):
    old = ("INSERT INTO scans (status, started, finished) VALUES "
           "('done', datetime('now','-8 hours'), datetime('now','-8 hours'))")
    seed = [
        old,
        "INSERT INTO applications (id, slug, name, status, kind) VALUES (1, 'demo', 'demo', 'running', 'compose')",
        "INSERT INTO plans (id, app_id) VALUES (1, 1)",
        "INSERT INTO jobs (plan_id, mode, status) VALUES (1, 'live', 'running')",
    ]
    assert _scheduler_runs(monkeypatch, 6, seed) == 0
