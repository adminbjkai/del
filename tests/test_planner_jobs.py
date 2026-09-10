"""Tests for planner.py (removal plan generation) and jobs.py (job engine).

Uses the real sqlite schema (via del_app.db.run_migrations against a
throwaway tmp_path DB) and a fake helper_client.call so no real docker/
systemd/nginx state is touched. Mirrors the settings_env fixture pattern in
test_core.py.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from del_app import auth, jobs, planner
from del_app.config import get_settings
from del_app.db import get_db, q, run_migrations, x
from del_app.models import Plan, PlanStep


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
helper_socket = "{tmp_path}/nonexistent-helper.sock"
protected_apps = ["del"]
"""
    )
    monkeypatch.setenv("DEL_CONFIG_PATH", str(config_path))
    get_settings.cache_clear()
    monkeypatch.setattr(auth, "SECRET_KEY_PATH", str(tmp_path / "secret.key"))
    settings = get_settings()
    run_migrations()
    yield settings
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _insert_user(conn, username="tester"):
    return x(
        conn,
        "INSERT INTO users (username, password_hash) VALUES (?, 'x')",
        (username,),
    )


def _insert_app(conn, slug, protected=0, kind="compose"):
    return x(
        conn,
        "INSERT INTO applications (slug, name, status, kind, protected) VALUES (?, ?, 'active', ?, ?)",
        (slug, slug, kind, protected),
    )


def _insert_resource(conn, rtype, key, display=None, path=None, data=None):
    return x(
        conn,
        "INSERT INTO resources (type, key, display, path, state, data_json) VALUES (?, ?, ?, ?, 'active', ?)",
        (rtype, key, display or key, path, json.dumps(data or {})),
    )


def _insert_assoc(conn, app_id, resource_id, confidence, source="scan", shared=0,
                   approved=0, excluded=0, removal_eligible="safe"):
    return x(
        conn,
        """
        INSERT INTO associations
          (app_id, resource_id, confidence, ownership, shared, data_loss_risk,
           removal_eligible, recommended_action, evidence_json, source, approved_by_user, excluded)
        VALUES (?, ?, ?, 'owner', ?, 'data', ?, 'remove', '[]', ?, ?, ?)
        """,
        (app_id, resource_id, confidence, shared, removal_eligible, source, approved, excluded),
    )


# ---------------------------------------------------------------------------
# planner.build_plan
# ---------------------------------------------------------------------------

def test_build_plan_refuses_protected_app(settings_env):
    conn = get_db()
    _insert_app(conn, "del", protected=1)
    conn.close()

    with pytest.raises(planner.PlanError):
        planner.build_plan("del", {})


def test_build_plan_refuses_app_in_config_protected_list(settings_env, monkeypatch):
    conn = get_db()
    _insert_app(conn, "myapp", protected=0)
    conn.close()

    settings = get_settings()
    monkeypatch.setattr(settings, "protected_apps", ["myapp"])
    with pytest.raises(planner.PlanError):
        planner.build_plan("myapp", {})


def test_shared_unapproved_volume_is_preserved_not_a_step(settings_env):
    conn = get_db()
    app_id = _insert_app(conn, "shared-app")
    vol_id = _insert_resource(conn, "volume", "shared_vol")
    _insert_assoc(conn, app_id, vol_id, confidence=95, shared=1, approved=0)
    conn.close()

    plan = planner.build_plan("shared-app", {"remove_named_volumes": True})

    assert "shared_vol" in plan.preserved
    assert not any(s.operation == "volume_rm" for s in plan.steps)
    assert any("shared_vol" in w for w in plan.warnings)


def test_shared_approved_volume_becomes_step_when_option_and_approval_present(settings_env):
    conn = get_db()
    app_id = _insert_app(conn, "shared-app2")
    vol_id = _insert_resource(conn, "volume", "shared_vol2", data={"size_bytes": 1000})
    _insert_assoc(conn, app_id, vol_id, confidence=95, shared=1, approved=1)
    conn.close()

    plan = planner.build_plan("shared-app2", {"remove_named_volumes": True})

    volume_steps = [s for s in plan.steps if s.operation == "volume_rm"]
    assert len(volume_steps) == 1
    assert volume_steps[0].args["volume_name"] == "shared_vol2"
    assert volume_steps[0].danger == "data_loss"
    assert "shared_vol2" not in plan.preserved
    assert plan.est_reclaim_bytes >= 1000


def test_volume_step_absent_unless_remove_named_volumes_set(settings_env):
    conn = get_db()
    app_id = _insert_app(conn, "app3")
    vol_id = _insert_resource(conn, "volume", "vol3")
    _insert_assoc(conn, app_id, vol_id, confidence=95, shared=0, approved=1)
    conn.close()

    plan = planner.build_plan("app3", {"remove_named_volumes": False})
    assert not any(s.operation == "volume_rm" for s in plan.steps)
    assert "vol3" in plan.preserved


def test_probable_association_is_warning_not_step(settings_env):
    conn = get_db()
    app_id = _insert_app(conn, "app4")
    res_id = _insert_resource(conn, "container", "cont4")
    _insert_assoc(conn, app_id, res_id, confidence=70)  # probable
    conn.close()

    plan = planner.build_plan("app4", {})
    assert not any(s.args.get("container_id") == "cont4" for s in plan.steps)
    assert any("requires per-resource approval" in w for w in plan.warnings)
    assert "cont4" in plan.preserved


def test_possible_association_is_preserved_with_warning(settings_env):
    conn = get_db()
    app_id = _insert_app(conn, "app5")
    res_id = _insert_resource(conn, "container", "cont5")
    _insert_assoc(conn, app_id, res_id, confidence=40)  # possible
    conn.close()

    plan = planner.build_plan("app5", {})
    assert "cont5" in plan.preserved
    assert plan.warnings


def test_confirmed_container_becomes_stop_and_rm_steps(settings_env):
    conn = get_db()
    app_id = _insert_app(conn, "app6", kind="standalone")
    res_id = _insert_resource(conn, "container", "cont6")
    _insert_assoc(conn, app_id, res_id, confidence=95)
    conn.close()

    plan = planner.build_plan("app6", {})
    ops = [(s.stage, s.operation) for s in plan.steps]
    assert ("quiesce", "container_stop") in ops
    assert ("remove_runtime", "container_rm") in ops
    # a validate step is always appended
    assert any(s.stage == "validate" and s.operation == "validate_removal" for s in plan.steps)


def test_confirmed_process_becomes_process_term_step(settings_env):
    """A standalone process resource with a confirmed pid+exe association
    must produce a process_term step, not be silently dropped."""
    conn = get_db()
    app_id = _insert_app(conn, "app_proc", kind="standalone")
    res_id = _insert_resource(
        conn, "process", "pid:1234:myapp",
        data={"pid": 1234, "exe": "/apps/myapp/bin/myapp", "comm": "myapp"},
    )
    _insert_assoc(conn, app_id, res_id, confidence=95)
    conn.close()

    plan = planner.build_plan("app_proc", {})
    term_steps = [s for s in plan.steps if s.operation == "process_term"]
    assert len(term_steps) == 1
    assert term_steps[0].stage == "quiesce"
    assert term_steps[0].args == {"pid": 1234, "expected_exe": "/apps/myapp/bin/myapp"}
    assert plan.preserved == []


def test_foreign_systemd_unit_is_not_planned_for_this_app(settings_env, tmp_path, monkeypatch):
    """A confirmed association to another app's unit (glmflix on a netdata
    plan) must be preserved, not turned into systemd_stop / systemd_rm_unit."""
    conn = get_db()
    app_id = _insert_app(conn, "netdata")
    compose_dir = tmp_path / "netdata"
    compose_dir.mkdir()
    compose_id = _insert_resource(
        conn, "compose_project", str(compose_dir), path=str(compose_dir),
        data={"working_dir": str(compose_dir), "declared_name": "netdata"},
    )
    _insert_assoc(conn, app_id, compose_id, confidence=95)
    other = tmp_path / "glmflix"
    other.mkdir()
    unit_id = _insert_resource(
        conn, "systemd_unit", "glmflix.service",
        path="/etc/systemd/system/glmflix.service",
        data={
            "is_custom": True,
            "working_directory": str(other),
            "exec_start": str(other / "start.sh"),
        },
    )
    _insert_assoc(conn, app_id, unit_id, confidence=95)
    conn.close()

    real_exists = os.path.exists

    def fake_exists(path):
        if path == "/etc/systemd/system/glmflix.service":
            return True
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", fake_exists)
    plan = planner.build_plan("netdata", {})
    unit_ops = [
        s for s in plan.steps
        if s.operation in ("systemd_stop", "systemd_disable", "systemd_rm_unit")
        and s.args.get("unit") == "glmflix.service"
    ]
    assert unit_ops == []
    assert "glmflix.service" in plan.preserved
    assert any("glmflix.service" in w and "not under this app" in w for w in plan.warnings)


def test_process_without_exe_is_preserved_not_dropped(settings_env):
    """If pid/exe is unavailable (e.g. older scan data, or exe unreadable),
    the association must be preserved with a warning rather than silently
    disappearing from the plan."""
    conn = get_db()
    app_id = _insert_app(conn, "app_proc2", kind="standalone")
    res_id = _insert_resource(
        conn, "process", "pid:5678:ghost",
        data={"pid": 5678, "exe": None, "comm": "ghost"},
    )
    _insert_assoc(conn, app_id, res_id, confidence=95)
    conn.close()

    plan = planner.build_plan("app_proc2", {})
    assert not any(s.operation == "process_term" for s in plan.steps)
    assert "pid:5678:ghost" in plan.preserved
    assert any("pid:5678:ghost" in w for w in plan.warnings)


# ---------------------------------------------------------------------------
# HMAC persistence + tamper guard
# ---------------------------------------------------------------------------

def test_persist_and_verify_plan_roundtrip(settings_env):
    conn = get_db()
    app_id = _insert_app(conn, "app7", kind="standalone")
    # A real app has at least one current resource; build_plan now refuses an
    # app with none rather than emitting a validate-only plan that would
    # execute as "success" having removed nothing.
    rid = _insert_resource(conn, "container", "app7_web")
    _insert_assoc(conn, app_id, rid, 100)
    conn.close()

    plan = planner.build_plan("app7", {})
    plan_id = planner.persist_plan(plan)
    assert isinstance(plan_id, int) and plan_id > 0

    verified = planner.verify_plan(plan_id)
    assert verified.app_slug == "app7"


def test_compose_project_name_collision_across_apps_produces_warning(settings_env):
    """Two apps whose compose_project resources resolve to the same project
    NAME (compose_down sweeps by that label, host-wide) must produce a
    warning naming both paths, since the sweep can hit the other app's
    containers."""
    conn = get_db()
    app_a = _insert_app(conn, "appa", kind="compose")
    app_b = _insert_app(conn, "appb", kind="compose")
    res_a = _insert_resource(
        conn, "compose_project", "/apps/appa/docker",
        path="/apps/appa/docker",
        data={"declared_name": "shared-name", "config_files": []},
    )
    res_b = _insert_resource(
        conn, "compose_project", "/apps/appb/docker",
        path="/apps/appb/docker",
        data={"declared_name": "shared-name", "config_files": []},
    )
    _insert_assoc(conn, app_a, res_a, confidence=95)
    _insert_assoc(conn, app_b, res_b, confidence=95)
    conn.close()

    plan = planner.build_plan("appa", {})
    assert any(
        "shared-name" in w and "/apps/appb/docker" in w for w in plan.warnings
    ), plan.warnings


def test_persist_plan_rejects_out_of_order_stages(settings_env):
    _insert_app(get_db(), "orderapp", kind="standalone")
    steps = [
        PlanStep(seq=1, stage="remove_files", operation="path_delete",
                  args={"path": "/apps/orderapp"}, description="delete",
                  reversible=False, danger="data_loss"),
        PlanStep(seq=2, stage="quiesce", operation="container_stop",
                  args={"container_id": "c1"}, description="stop",
                  reversible=True, danger="safe"),
    ]
    plan = Plan(app_slug="orderapp", options={}, steps=steps)
    with pytest.raises(planner.PlanError):
        planner.persist_plan(plan)


def test_load_plan_raises_plan_not_found_error_for_unknown_id(settings_env):
    with pytest.raises(planner.PlanNotFoundError):
        planner.load_plan(999999)
    # PlanNotFoundError is a PlanError, so existing PlanError catches keep working.
    with pytest.raises(planner.PlanError):
        planner.load_plan(999999)


def test_tampered_plan_fails_hmac_verification(settings_env):
    conn = get_db()
    app_id = _insert_app(conn, "app8", kind="standalone")
    rid = _insert_resource(conn, "container", "app8_web")
    _insert_assoc(conn, app_id, rid, 100)
    conn.close()

    plan = planner.build_plan("app8", {})
    plan_id = planner.persist_plan(plan)

    conn = get_db()
    row = q(conn, "SELECT steps_json FROM plans WHERE id = ?", (plan_id,))[0]
    tampered = row["steps_json"].replace("validate_removal", "validate_r3moval")
    x(conn, "UPDATE plans SET steps_json = ? WHERE id = ?", (tampered, plan_id))
    conn.close()

    with pytest.raises(planner.PlanError):
        planner.verify_plan(plan_id)

    with pytest.raises(planner.PlanError):
        jobs.create_job(plan_id, "dry_run", user_id=1)


# ---------------------------------------------------------------------------
# jobs.py — job engine
# ---------------------------------------------------------------------------

def _persist_manual_plan(app_slug, steps):
    conn = get_db()
    _insert_app(conn, app_slug, kind="standalone")
    _insert_user(conn, f"user-{app_slug}")
    conn.close()
    plan = Plan(app_slug=app_slug, options={}, steps=steps)
    return planner.persist_plan(plan)


class _FakeHelper:
    def __init__(self, failing_ops=frozenset()):
        self.calls = []
        self.failing_ops = failing_ops

    def call(self, op, args, dry_run=True, timeout=300, plan_id=None, step_id=None):
        self.calls.append({"op": op, "args": args, "dry_run": dry_run})
        if op in self.failing_ops:
            return {"ok": False, "dry_run": dry_run, "output": "", "error": f"{op} failed", "changed": []}
        return {"ok": True, "dry_run": dry_run, "output": f"{op} ok", "error": None, "changed": []}


def _patch_audit(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs.auditlog, "audit", lambda *a, **kw: calls.append((a, kw)))
    return calls


def test_dry_run_job_executes_all_steps_with_dry_run_true(settings_env, monkeypatch):
    _patch_audit(monkeypatch)
    fake = _FakeHelper()
    monkeypatch.setattr(jobs, "helper_client", fake)

    steps = [
        PlanStep(seq=1, stage="quiesce", operation="container_stop",
                  args={"container_id": "c1"}, description="stop", reversible=True, danger="safe"),
        PlanStep(seq=2, stage="remove_runtime", operation="container_rm",
                  args={"container_id": "c1"}, description="rm", reversible=False, danger="warning"),
    ]
    plan_id = _persist_manual_plan("dryapp", steps)
    job_id = jobs.create_job(plan_id, "dry_run", user_id=1)

    jobs._run_job(job_id, None)

    assert len(fake.calls) == 2
    assert all(c["dry_run"] is True for c in fake.calls)

    status = jobs.job_status(job_id)
    assert status["status"] == "success"
    assert all(s["state"] == "done" for s in status["steps"])


def test_job_status_includes_progress_and_current_step(settings_env, monkeypatch):
    _patch_audit(monkeypatch)
    fake = _FakeHelper(failing_ops={"container_rm"})
    monkeypatch.setattr(jobs, "helper_client", fake)

    steps = [
        PlanStep(seq=1, stage="quiesce", operation="container_stop",
                  args={"container_id": "c1"}, description="stop", reversible=True, danger="safe"),
        PlanStep(seq=2, stage="remove_runtime", operation="container_rm",
                  args={"container_id": "c1"}, description="rm", reversible=False, danger="warning"),
        PlanStep(seq=3, stage="remove_runtime", operation="network_rm",
                  args={"network_name": "n1"}, description="net rm", reversible=False, danger="warning"),
    ]
    plan_id = _persist_manual_plan("progressapp", steps)
    job_id = jobs.create_job(plan_id, "live", user_id=1)

    jobs._run_job(job_id, None)

    status = jobs.job_status(job_id)
    assert status["progress"] == {"done": 2, "total": 3, "pct": 67}
    # No step is left "running" after _run_job finishes.
    assert status["current_step"] is None
    for s in status["steps"]:
        assert "duration" in s
    assert set(status["steps"][0].keys()) >= {
        "seq", "stage", "operation", "state", "exit_code", "duration", "output_sanitized",
    }


def test_failure_mid_job_halts_downstream_steps(settings_env, monkeypatch):
    _patch_audit(monkeypatch)
    fake = _FakeHelper(failing_ops={"container_rm"})
    monkeypatch.setattr(jobs, "helper_client", fake)

    steps = [
        PlanStep(seq=1, stage="quiesce", operation="container_stop",
                  args={"container_id": "c1"}, description="stop", reversible=True, danger="safe"),
        PlanStep(seq=2, stage="remove_runtime", operation="container_rm",
                  args={"container_id": "c1"}, description="rm", reversible=False, danger="warning"),
        PlanStep(seq=3, stage="remove_runtime", operation="network_rm",
                  args={"network_name": "n1"}, description="net rm", reversible=False, danger="warning"),
    ]
    plan_id = _persist_manual_plan("failapp", steps)
    job_id = jobs.create_job(plan_id, "live", user_id=1)

    jobs._run_job(job_id, None)

    ops_called = [c["op"] for c in fake.calls]
    assert "network_rm" not in ops_called  # downstream step never reached

    status = jobs.job_status(job_id)
    assert status["status"] == "failed"
    by_op = {s["operation"]: s["state"] for s in status["steps"]}
    assert by_op["container_stop"] == "done"
    assert by_op["container_rm"] == "failed"
    assert by_op["network_rm"] == "pending"


def test_unexpected_exception_mid_step_fails_job_instead_of_corrupting_state(
    settings_env, monkeypatch
):
    """An unexpected (non-HelperError) exception raised while executing a
    step must be recorded as a normal failed step/job, not left running
    forever or propagated out of the worker thread."""
    _patch_audit(monkeypatch)

    class _BoomHelper:
        def call(self, op, args, dry_run=True, timeout=300, plan_id=None, step_id=None):
            raise RuntimeError("boom: unexpected helper crash")

    monkeypatch.setattr(jobs, "helper_client", _BoomHelper())

    steps = [
        PlanStep(seq=1, stage="quiesce", operation="container_stop",
                  args={"container_id": "c1"}, description="stop", reversible=True, danger="safe"),
    ]
    plan_id = _persist_manual_plan("boomapp", steps)
    job_id = jobs.create_job(plan_id, "live", user_id=1)

    jobs._run_job(job_id, None)  # must not raise

    status = jobs.job_status(job_id)
    assert status["status"] == "failed"
    by_op = {s["operation"]: s["state"] for s in status["steps"]}
    assert by_op["container_stop"] == "failed"


def test_live_volume_removal_without_confirm_phrase_is_refused(settings_env, monkeypatch):
    _patch_audit(monkeypatch)
    fake = _FakeHelper()
    monkeypatch.setattr(jobs, "helper_client", fake)

    steps = [
        PlanStep(seq=1, stage="remove_runtime", operation="volume_rm",
                  args={"volume_name": "v1"}, description="rm vol", reversible=False, danger="data_loss"),
    ]
    plan_id = _persist_manual_plan("volapp", steps)
    job_id = jobs.create_job(plan_id, "live", user_id=1)

    jobs._run_job(job_id, None)

    assert fake.calls == []  # never even attempted
    status = jobs.job_status(job_id)
    assert status["status"] == "refused"
    assert status["steps"][0]["state"] == "pending"


def test_live_volume_removal_with_confirm_phrase_proceeds(settings_env, monkeypatch):
    _patch_audit(monkeypatch)
    fake = _FakeHelper()
    monkeypatch.setattr(jobs, "helper_client", fake)

    steps = [
        PlanStep(seq=1, stage="remove_runtime", operation="volume_rm",
                  args={"volume_name": "v1"}, description="rm vol", reversible=False, danger="data_loss"),
    ]
    plan_id = _persist_manual_plan("volapp2", steps)
    job_id = jobs.create_job(plan_id, "live", user_id=1)

    jobs._run_job(job_id, jobs.CONFIRM_VOLUMES_PHRASE)

    assert len(fake.calls) == 1
    status = jobs.job_status(job_id)
    assert status["status"] == "success"


def test_execute_job_runs_in_background_thread(settings_env, monkeypatch):
    _patch_audit(monkeypatch)
    fake = _FakeHelper()
    monkeypatch.setattr(jobs, "helper_client", fake)

    steps = [
        PlanStep(seq=1, stage="quiesce", operation="tmux_kill",
                  args={"session": "s1"}, description="kill", reversible=False, danger="warning"),
    ]
    plan_id = _persist_manual_plan("threadapp", steps)
    job_id = jobs.create_job(plan_id, "dry_run", user_id=1)

    jobs.execute_job(job_id)

    deadline = time.time() + 5
    status = jobs.job_status(job_id)
    while status["status"] not in ("success", "failed", "refused") and time.time() < deadline:
        time.sleep(0.05)
        status = jobs.job_status(job_id)

    assert status["status"] == "success"


def test_job_skips_foreign_systemd_unit_at_execute_time(settings_env, tmp_path, monkeypatch):
    """A poisoned plan that still lists another app's unit must not call the
    helper — the step is marked done as skipped so retry can continue."""
    _patch_audit(monkeypatch)
    fake = _FakeHelper()
    monkeypatch.setattr(jobs, "helper_client", fake)

    other = tmp_path / "glmflix"
    other.mkdir()
    conn = get_db()
    _insert_resource(
        conn, "systemd_unit", "glmflix.service",
        data={"working_directory": str(other), "exec_start": str(other / "start.sh")},
    )
    conn.close()

    steps = [
        PlanStep(seq=1, stage="quiesce", operation="systemd_stop",
                  args={"unit": "glmflix.service"}, description="stop",
                  reversible=True, danger="safe"),
        PlanStep(seq=2, stage="quiesce", operation="container_stop",
                  args={"container_id": "netdata"}, description="stop c",
                  reversible=True, danger="safe"),
    ]
    plan_id = _persist_manual_plan("netdata", steps)
    job_id = jobs.create_job(plan_id, "live", user_id=1)
    jobs._run_job(job_id, None)

    assert [c["op"] for c in fake.calls] == ["container_stop"]
    status = jobs.job_status(job_id)
    assert status["status"] == "success"
    by_op = {s["operation"]: s for s in status["steps"]}
    assert by_op["systemd_stop"]["state"] == "done"
    assert "skipped" in (by_op["systemd_stop"]["output_sanitized"] or "")
    assert "glmflix.service" in (by_op["systemd_stop"]["output_sanitized"] or "")


def test_retry_job_resumes_from_first_failed_step(settings_env, monkeypatch):
    _patch_audit(monkeypatch)
    fake = _FakeHelper(failing_ops={"container_rm"})
    monkeypatch.setattr(jobs, "helper_client", fake)

    steps = [
        PlanStep(seq=1, stage="quiesce", operation="container_stop",
                  args={"container_id": "c1"}, description="stop", reversible=True, danger="safe"),
        PlanStep(seq=2, stage="remove_runtime", operation="container_rm",
                  args={"container_id": "c1"}, description="rm", reversible=False, danger="warning"),
    ]
    plan_id = _persist_manual_plan("retryapp", steps)
    job_id = jobs.create_job(plan_id, "live", user_id=1)
    jobs._run_job(job_id, None)
    assert jobs.job_status(job_id)["status"] == "failed"

    # fix the failure condition and retry
    fake.failing_ops = frozenset()
    jobs.retry_job(job_id)

    # retry_job clears status to 'pending' then runs in a background thread.
    # Wait until the job is success AND the retried step is done (avoids the
    # race of seeing step=done a tick before job status flips to success).
    deadline = time.time() + 5
    status = jobs.job_status(job_id)
    while time.time() < deadline:
        status = jobs.job_status(job_id)
        by_op = {s["operation"]: s["state"] for s in status["steps"]}
        if status["status"] == "success" and by_op.get("container_rm") == "done":
            break
        time.sleep(0.05)

    assert status["status"] == "success"
    by_op = {s["operation"]: s["state"] for s in status["steps"]}
    assert by_op["container_rm"] == "done"
    # container_stop was not re-run because it was already 'done'
    stop_calls = [c for c in fake.calls if c["op"] == "container_stop"]
    assert len(stop_calls) == 1


def test_sanitize_output_redacts_secrets():
    text = "connected password=hunter2 token=abc123 fine"
    sanitized = jobs.sanitize_output(text)
    assert "hunter2" not in sanitized
    assert "abc123" not in sanitized
    assert "password=***" in sanitized
    assert "token=***" in sanitized


def test_validate_removal_uses_preserved_to_skip_checks(monkeypatch):
    monkeypatch.setattr(jobs, "_run_check", lambda cmd: (False, "not found"))
    plan = Plan(
        app_slug="x",
        options={},
        steps=[
            PlanStep(seq=1, stage="remove_runtime", operation="volume_rm",
                      args={"volume_name": "v1"}, description="d", reversible=False, danger="data_loss"),
        ],
        preserved=["v1"],
    )
    checks = jobs.validate_removal("x", plan)
    assert checks == []


# ---------------------------------------------------------------------------
# Backup recording + rollback (2026-08-24)
#
# The `backups` table was created by the initial migration but never written
# to, so `_restore_from_backups` always iterated zero rows while the job still
# audited "job_restore_attempted" — operators were told a rollback happened
# when none had. These lock in the corrected behaviour.
# ---------------------------------------------------------------------------

def _backup_then_fail_steps(tmp_path):
    return [
        PlanStep(
            seq=1, stage="backup", operation="file_backup",
            args={"path": f"{tmp_path}/site.conf", "dest": f"{tmp_path}/backups/site.conf"},
            description="back up nginx site", reversible=True, danger="safe",
        ),
        PlanStep(
            seq=2, stage="remove_host", operation="nginx_rm_site",
            args={"paths": ["/etc/nginx/sites-enabled/site.conf"]},
            description="remove nginx site", reversible=True, danger="warning",
        ),
    ]


def test_live_backup_step_records_a_backups_row(settings_env, tmp_path, monkeypatch):
    _patch_audit(monkeypatch)
    fake = _FakeHelper()
    monkeypatch.setattr(jobs, "helper_client", fake)

    plan_id = _persist_manual_plan("bk1", _backup_then_fail_steps(tmp_path))
    job_id = jobs.create_job(plan_id, "live", user_id=1)
    jobs._run_job(job_id, confirm_phrase=None)

    conn = get_db()
    rows = q(conn, "SELECT * FROM backups WHERE job_id = ?", (job_id,))
    conn.close()
    assert len(rows) == 1, "a successful live backup step must record a backups row"
    assert rows[0]["kind"] == "file_backup"
    assert rows[0]["src"] == f"{tmp_path}/site.conf"
    assert rows[0]["dest"] == f"{tmp_path}/backups/site.conf"


def test_dry_run_backup_step_records_nothing(settings_env, tmp_path, monkeypatch):
    _patch_audit(monkeypatch)
    monkeypatch.setattr(jobs, "helper_client", _FakeHelper())

    plan_id = _persist_manual_plan("bk2", _backup_then_fail_steps(tmp_path))
    job_id = jobs.create_job(plan_id, "dry_run", user_id=1)
    jobs._run_job(job_id, confirm_phrase=None)

    conn = get_db()
    rows = q(conn, "SELECT * FROM backups WHERE job_id = ?", (job_id,))
    conn.close()
    assert rows == [], "a dry run backs nothing up, so it must record nothing"


def test_failed_removal_restores_recorded_backup_with_correct_helper_args(
    settings_env, tmp_path, monkeypatch
):
    """The restore call previously sent {src, dest}; the helper requires
    {backup_path, original_path} and rejected every one of them."""
    audits = _patch_audit(monkeypatch)
    fake = _FakeHelper(failing_ops={"nginx_rm_site"})
    monkeypatch.setattr(jobs, "helper_client", fake)

    plan_id = _persist_manual_plan("bk3", _backup_then_fail_steps(tmp_path))
    job_id = jobs.create_job(plan_id, "live", user_id=1)
    jobs._run_job(job_id, confirm_phrase=None)

    restores = [c for c in fake.calls if c["op"] == "path_restore"]
    assert len(restores) == 1, "the failed removal did not trigger a restore"
    assert restores[0]["args"] == {
        "backup_path": f"{tmp_path}/backups/site.conf",
        "original_path": f"{tmp_path}/site.conf",
    }
    assert restores[0]["dry_run"] is False

    attempted = [kw or a[3] for a, kw in audits if a[1] == "job_restore_attempted"]
    assert attempted, "restore attempt was not audited"
    detail = attempted[0]
    assert detail["backups_found"] == 1
    assert detail["restored"] == 1, "audit must report the real outcome, not just the attempt"


def test_restore_reports_failure_instead_of_claiming_success(
    settings_env, tmp_path, monkeypatch
):
    audits = _patch_audit(monkeypatch)
    fake = _FakeHelper(failing_ops={"nginx_rm_site", "path_restore"})
    monkeypatch.setattr(jobs, "helper_client", fake)

    plan_id = _persist_manual_plan("bk4", _backup_then_fail_steps(tmp_path))
    job_id = jobs.create_job(plan_id, "live", user_id=1)
    jobs._run_job(job_id, confirm_phrase=None)

    detail = next(kw or a[3] for a, kw in audits if a[1] == "job_restore_attempted")
    assert detail["backups_found"] == 1
    assert detail["restored"] == 0, "a failed restore must not be recorded as restored"


# ---------------------------------------------------------------------------
# Backup destination uniqueness (2026-08-24, found by independent verification)
#
# Making rollback real also made it capable of restoring the WRONG file:
# destinations were keyed on basename only, so two different config files with
# the same name collided, `cp -a` silently overwrote, both source paths were
# recorded against that one destination, and a rollback wrote the survivor's
# contents back over BOTH originals. 21 real collisions with differing content
# existed in the live inventory.
# ---------------------------------------------------------------------------

def test_same_basename_in_different_directories_gets_distinct_backup_dests(
    settings_env, tmp_path
):
    # Real files: the backup stage only emits a step for a path that exists.
    a_dir = tmp_path / "server"
    b_dir = tmp_path / "server" / "config"
    b_dir.mkdir(parents=True)
    a_file = a_dir / "compose.yaml"
    b_file = b_dir / "compose.yaml"
    a_file.write_text("services: {a: {}}\n")
    b_file.write_text("services: {b: {}}\n")

    conn = get_db()
    app_id = _insert_app(conn, "collide", kind="compose")
    for key, cfg in (("compose-a", a_file), ("compose-b", b_file)):
        rid = _insert_resource(conn, "compose_project", key, path=str(cfg),
                               data={"config_files": [str(cfg)]})
        _insert_assoc(conn, app_id, rid, 95)
    conn.close()

    plan = planner.build_plan("collide", {"backup": "config"})
    backups = [s for s in plan.steps if s.operation == "file_backup"]
    dests = [s.args["dest"] for s in backups]
    assert len(dests) == 2, f"expected two backup steps, got {dests}"
    assert len(set(dests)) == 2, (
        f"backup destinations collide, so a rollback would restore one file "
        f"over both originals: {dests}"
    )
    # path_restore requires the basename to match the original it replaces.
    for step in backups:
        assert os.path.basename(step.args["dest"]) == os.path.basename(step.args["path"])


def test_compose_down_never_targets_a_generic_project_label(settings_env):
    """compose_down sweeps stragglers by Docker label across the WHOLE host, so
    a project name of "docker" would force-remove every container on the box
    labelled com.docker.compose.project=docker — other apps included."""
    conn = get_db()
    app_id = _insert_app(conn, "myapp", kind="compose")
    rid = _insert_resource(
        conn, "compose_project", "myapp-compose",
        path="/apps/myapp/docker",
        data={"config_files": ["/apps/myapp/docker/docker-compose.yml"]},
    )
    _insert_assoc(conn, app_id, rid, 95)
    conn.close()

    plan = planner.build_plan("myapp", {})
    projects = [s.args["project"] for s in plan.steps if s.operation == "compose_down"]
    assert projects, "no compose_down step was generated"
    assert "docker" not in projects, f"generic label sweep target: {projects}"
    assert projects == ["myapp"], projects


def test_declared_compose_project_name_is_still_preferred(settings_env):
    conn = get_db()
    app_id = _insert_app(conn, "declared", kind="compose")
    rid = _insert_resource(
        conn, "compose_project", "declared-compose",
        path="/apps/declared/docker",
        data={"declared_name": "realproject",
              "config_files": ["/apps/declared/docker/compose.yml"]},
    )
    _insert_assoc(conn, app_id, rid, 95)
    conn.close()
    plan = planner.build_plan("declared", {})
    projects = [s.args["project"] for s in plan.steps if s.operation == "compose_down"]
    assert projects == ["realproject"], projects


def test_build_plan_refuses_an_app_with_no_associations_at_all(settings_env):
    """Previously produced a lone validate step that executed as 'success'."""
    conn = get_db()
    _insert_app(conn, "empty-app", kind="standalone")
    conn.close()
    with pytest.raises(planner.PlanError, match="no resources in the latest completed scan"):
        planner.build_plan("empty-app", {})
