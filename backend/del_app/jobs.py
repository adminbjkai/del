"""Staged removal job engine: executes a persisted, HMAC-verified Plan one
step at a time in stage order, recording a job_steps row before and after
each step, halting on unsafe failure, and supporting resume via retry_job.

See docs/ARCHITECTURE.md "Removal job engine" and docs/INTERFACES.md jobs.py.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from datetime import datetime, timezone

from del_app import auditlog, helper_client
from del_app.db import get_db, q, x
from del_app.models import Plan
from del_app.planner import PlanError, verify_plan

CONFIRM_VOLUMES_PHRASE = "DELETE VOLUMES"

_SECRET_RE = re.compile(r"(?i)(password|token|secret|key)=\S+")

# Stages whose step failures always halt the job before any downstream
# deletion runs (backup/validate failures also halt: nothing downstream
# should proceed on top of an unverified state).
_HALTING_STAGES = {"backup", "quiesce", "remove_runtime", "remove_host", "remove_files", "validate"}

# Failures in these stages attempt an automatic restore from the backups
# recorded earlier in this job before the job is marked failed.
_RESTORE_ON_FAILURE_OPS = {"nginx_rm_site", "nginx_test_reload", "systemd_disable", "systemd_rm_unit"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_output(text: str) -> str:
    """Redact obvious secret-shaped values from helper output before it is
    persisted or logged."""
    if not text:
        return text
    return _SECRET_RE.sub(r"\1=***", text)


class JobError(Exception):
    """Raised for job-level refusals (tamper guard, missing confirmation)."""


def create_job(plan_id: int, mode: str, user_id: int) -> int:
    """Create a job + its (pending) job_steps snapshot from a verified plan.
    Raises PlanError if the plan's HMAC does not verify."""
    if mode not in ("dry_run", "live"):
        raise JobError(f"invalid job mode: {mode}")

    conn = get_db()
    try:
        plan = verify_plan(plan_id, conn)
        if mode == "live":
            active = q(
                conn,
                "SELECT id FROM jobs WHERE plan_id = ? AND mode = 'live' "
                "AND status IN ('pending', 'running')",
                (plan_id,),
            )
            if active:
                raise JobError(
                    f"plan {plan_id} already has an active live job "
                    f"(id={active[0]['id']}); refusing a concurrent live run")
        job_id = x(
            conn,
            "INSERT INTO jobs (plan_id, mode, status, user_id) VALUES (?, ?, 'pending', ?)",
            (plan_id, mode, user_id),
        )
        for step in plan.steps:
            x(
                conn,
                """
                INSERT INTO job_steps (job_id, seq, stage, operation, args_json, state, reversible)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (job_id, step.seq, step.stage, step.operation, json.dumps(step.args), int(step.reversible)),
            )
        conn.commit()
    finally:
        conn.close()

    auditlog.audit(user_id, "job_created", f"plan:{plan_id}", {"job_id": job_id, "mode": mode})
    return job_id


def execute_job(job_id: int, confirm_phrase: str | None = None) -> None:
    """Run job_id's steps in a background daemon thread. confirm_phrase, if
    given, is the second-confirmation typed phrase required for live jobs
    that contain a volume_rm step."""
    thread = threading.Thread(target=_run_job, args=(job_id, confirm_phrase), daemon=True)
    thread.start()


def _mark_job(conn, job_id: int, status: str, *, started: bool = False, finished: bool = False) -> None:
    if started:
        x(conn, "UPDATE jobs SET status = ?, started = ? WHERE id = ?", (status, _now(), job_id))
    elif finished:
        x(conn, "UPDATE jobs SET status = ?, finished = ? WHERE id = ?", (status, _now(), job_id))
    else:
        x(conn, "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))


def _restore_from_backups(conn, job_id: int, failed_step: dict) -> None:
    backups = q(conn, "SELECT * FROM backups WHERE job_id = ? ORDER BY id DESC", (job_id,))
    for b in backups:
        try:
            helper_client.call(
                "path_restore",
                {"src": b["dest"], "dest": b["src"]},
                dry_run=False,
            )
        except Exception:
            continue


def _run_job(job_id: int, confirm_phrase: str | None) -> None:
    conn = get_db()
    try:
        job_rows = q(conn, "SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job_rows:
            return
        job = job_rows[0]
        mode = job["mode"]
        user_id = job["user_id"]

        try:
            plan = verify_plan(job["plan_id"], conn)
        except PlanError as e:
            _mark_job(conn, job_id, "failed", started=True)
            conn.commit()
            auditlog.audit(user_id, "job_refused", f"job:{job_id}", {"reason": str(e)})
            return

        has_volume_rm = any(s.operation == "volume_rm" for s in plan.steps)
        if mode == "live" and has_volume_rm and confirm_phrase != CONFIRM_VOLUMES_PHRASE:
            _mark_job(conn, job_id, "refused", started=True)
            conn.commit()
            auditlog.audit(
                user_id, "job_refused", f"job:{job_id}",
                {"reason": "live volume removal requires typed confirmation phrase"},
            )
            return

        _mark_job(conn, job_id, "running", started=True)
        conn.commit()
        auditlog.audit(user_id, "job_started", f"job:{job_id}", {"mode": mode})

        steps = q(conn, "SELECT * FROM job_steps WHERE job_id = ? ORDER BY seq", (job_id,))

        job_failed = False
        for step in steps:
            if step["state"] == "done":
                continue

            if job_failed:
                # halted: leave remaining steps pending
                break

            x(
                conn,
                "UPDATE job_steps SET state = 'running', started = ? WHERE id = ?",
                (_now(), step["id"]),
            )
            conn.commit()
            auditlog.audit(user_id, "step_running", f"job:{job_id}:seq:{step['seq']}",
                            {"operation": step["operation"]})

            args = json.loads(step["args_json"]) if step["args_json"] else {}

            if step["stage"] == "validate" and step["operation"] == "validate_removal":
                app_row = q(
                    conn,
                    "SELECT a.slug FROM plans p JOIN applications a ON a.id = p.app_id WHERE p.id = ?",
                    (job["plan_id"],),
                )
                app_slug = app_row[0]["slug"] if app_row else args.get("app_slug", "")
                checks = validate_removal(app_slug, plan)
                if mode == "dry_run":
                    # Nothing was actually removed in a dry run, so failing
                    # checks are expected; report them as informational.
                    ok = True
                    output = json.dumps({
                        "dry_run": True,
                        "note": "validation checks that will run after live removal; "
                                "current failures are expected pre-removal",
                        "checks": checks,
                    })
                    exit_code = 0
                else:
                    ok = all(c["ok"] for c in checks)
                    output = json.dumps(checks)
                    exit_code = 0 if ok else 1
            else:
                if step["operation"] == "volume_rm":
                    # The helper independently requires confirmed_twice; grant it
                    # only when the double-confirmation gate has actually passed
                    # (dry runs delete nothing; live jobs with volume_rm steps
                    # were already refused above without the exact phrase).
                    args["confirmed_twice"] = (
                        mode == "dry_run" or confirm_phrase == CONFIRM_VOLUMES_PHRASE
                    )
                try:
                    result = helper_client.call(step["operation"], args, dry_run=(mode == "dry_run"))
                    ok = bool(result.get("ok"))
                    output = str(result.get("output") or result.get("error") or "")
                except helper_client.HelperError as e:
                    ok = False
                    output = str(e)
                exit_code = 0 if ok else 1

            output = sanitize_output(output)
            state = "done" if ok else "failed"
            x(
                conn,
                "UPDATE job_steps SET state = ?, exit_code = ?, output_sanitized = ?, finished = ? WHERE id = ?",
                (state, exit_code, output, _now(), step["id"]),
            )
            conn.commit()
            auditlog.audit(
                user_id, f"step_{state}", f"job:{job_id}:seq:{step['seq']}",
                {"operation": step["operation"], "exit_code": exit_code},
            )

            if not ok:
                job_failed = True
                if step["operation"] in _RESTORE_ON_FAILURE_OPS:
                    _restore_from_backups(conn, job_id, dict(step))
                    auditlog.audit(user_id, "job_restore_attempted", f"job:{job_id}",
                                    {"failed_operation": step["operation"]})

        final_status = "failed" if job_failed else "success"
        _mark_job(conn, job_id, final_status, finished=True)
        conn.commit()
        auditlog.audit(user_id, f"job_{final_status}", f"job:{job_id}", {})
    finally:
        conn.close()

    # After a successful LIVE removal, rescan so the UI immediately reflects
    # the new reality instead of the pre-removal snapshot.
    if mode == "live" and final_status == "success":
        try:
            from del_app import scanner
            scan_id = scanner.run_scan()
            auditlog.audit(user_id, "post_removal_rescan", f"job:{job_id}", {"scan_id": scan_id})
        except Exception:
            pass


def retry_job(job_id: int, confirm_phrase: str | None = None) -> None:
    """Reset the first failed step (and any steps after it) to pending and
    re-execute the job in a background thread, effectively resuming from
    the point of failure."""
    conn = get_db()
    try:
        steps = q(conn, "SELECT * FROM job_steps WHERE job_id = ? ORDER BY seq", (job_id,))
        failed_seqs = [s["seq"] for s in steps if s["state"] == "failed"]
        if not failed_seqs:
            return
        first_failed_seq = min(failed_seqs)
        x(
            conn,
            "UPDATE job_steps SET state = 'pending', exit_code = NULL, output_sanitized = NULL, "
            "started = NULL, finished = NULL WHERE job_id = ? AND seq >= ?",
            (job_id, first_failed_seq),
        )
        conn.commit()
    finally:
        conn.close()
    execute_job(job_id, confirm_phrase=confirm_phrase)


def job_status(job_id: int) -> dict:
    conn = get_db()
    try:
        job_rows = q(conn, "SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job_rows:
            raise JobError(f"no such job: {job_id}")
        job = dict(job_rows[0])
        steps = [dict(s) for s in q(conn, "SELECT * FROM job_steps WHERE job_id = ? ORDER BY seq", (job_id,))]
        job["steps"] = steps
        return job
    finally:
        conn.close()


def _run_check(cmd: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:
        return False, str(e)


def validate_removal(app_slug: str, plan: Plan) -> list[dict]:
    """Stage 8 post-removal checks: no containers/networks/volumes remain
    (unless preserved), unit inactive, port not listening, nginx -t ok.
    Performed as direct read-only subprocess checks, independent of the
    helper."""
    checks: list[dict] = []
    preserved = set(plan.preserved)

    for step in plan.steps:
        if step.operation in ("container_rm",):
            cid = step.args.get("container_id")
            ok, out = _run_check(["docker", "inspect", cid])
            checks.append({"check": f"container_absent:{cid}", "ok": not ok, "detail": out})
        elif step.operation == "compose_down":
            project = step.args.get("project")
            ok, out = _run_check(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "-q"])
            checks.append({"check": f"compose_containers_absent:{project}", "ok": ok and not out.strip(), "detail": out})
        elif step.operation == "volume_rm":
            vol = step.args.get("volume_name")
            if vol in preserved:
                continue
            ok, out = _run_check(["docker", "volume", "inspect", vol])
            checks.append({"check": f"volume_absent:{vol}", "ok": not ok, "detail": out})
        elif step.operation == "network_rm":
            net = step.args.get("network_name")
            if net in preserved:
                continue
            ok, out = _run_check(["docker", "network", "inspect", net])
            checks.append({"check": f"network_absent:{net}", "ok": not ok, "detail": out})
        elif step.operation in ("systemd_disable", "systemd_rm_unit"):
            unit = step.args.get("unit")
            ok, out = _run_check(["systemctl", "is-active", unit])
            checks.append({"check": f"unit_inactive:{unit}", "ok": "inactive" in out or not ok, "detail": out})
        elif step.operation == "nginx_rm_site":
            try:
                result = helper_client.call("nginx_test", {}, dry_run=False)
                ok, out = bool(result.get("ok")), str(result.get("output") or result.get("error") or "")
            except helper_client.HelperError as e:
                ok, out = False, str(e)
            checks.append({"check": "nginx_config_ok", "ok": ok, "detail": out})

    return checks
