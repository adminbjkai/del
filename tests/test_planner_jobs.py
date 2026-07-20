"""Tests for planner.py (removal plan generation) and jobs.py (job engine).

Uses the real sqlite schema (via del_app.db.run_migrations against a
throwaway tmp_path DB) and a fake helper_client.call so no real docker/
systemd/nginx state is touched. Mirrors the settings_env fixture pattern in
test_core.py.
"""
from __future__ import annotations

import json
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
    _insert_app(conn, "app7", kind="standalone")
    conn.close()

    plan = planner.build_plan("app7", {})
    plan_id = planner.persist_plan(plan)
    assert isinstance(plan_id, int) and plan_id > 0

    verified = planner.verify_plan(plan_id)
    assert verified.app_slug == "app7"


def test_tampered_plan_fails_hmac_verification(settings_env):
    conn = get_db()
    _insert_app(conn, "app8", kind="standalone")
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

    deadline = time.time() + 5
    status = jobs.job_status(job_id)
    by_op = {s["operation"]: s["state"] for s in status["steps"]}
    while by_op.get("container_rm") != "done" and time.time() < deadline:
        time.sleep(0.05)
        status = jobs.job_status(job_id)
        by_op = {s["operation"]: s["state"] for s in status["steps"]}

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
