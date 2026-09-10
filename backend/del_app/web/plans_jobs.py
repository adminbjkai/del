"""Plans (build/view/execute) and jobs (list/detail/status)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from del_app import auditlog, auth
from del_app.auth import User
from del_app.db import get_db, q
from del_app.web.formatting import _duration
from del_app.web.queries import _rows
from del_app.web.render import _csrf_response, _render, _require_csrf

# Lazy/defensive imports of sibling lanes' modules. Accessed as
# `<name>.<func>` at call time so tests can monkeypatch these module
# references directly on this module.
try:
    from del_app import planner
except ImportError:  # pragma: no cover - lane not landed yet
    planner = None  # type: ignore[assignment]

try:
    from del_app import jobs
except ImportError:  # pragma: no cover
    jobs = None  # type: ignore[assignment]

router = APIRouter()

# The jobs page rendered every job ever run. Cap it; the table is already
# client-side paginated, and older jobs stay reachable by direct URL.
JOBS_PAGE_LIMIT = 200


@router.get("/apps/{slug}/plan", response_class=HTMLResponse)
def plan_form(
    slug: str, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        rows = _rows(q(conn, "SELECT * FROM applications WHERE slug = ?", (slug,)))
        if not rows:
            raise HTTPException(status_code=404, detail=f"no such application: {slug}")
        app = rows[0]
        volumes = _rows(
            q(
                conn,
                """
                SELECT r.* FROM resources r
                JOIN associations a ON a.resource_id = r.id
                JOIN applications ap ON ap.id = a.app_id
                WHERE ap.slug = ? AND r.type = 'volume'
                  AND (r.last_seen = (SELECT MAX(id) FROM scans WHERE status = 'done')
                       OR NOT EXISTS (SELECT 1 FROM scans WHERE status = 'done'))
                """,
                (slug,),
            )
        )
    finally:
        conn.close()
    return _render(
        "plan.html", request, response, app=app, volumes=volumes, plan=None
    )


@router.post("/apps/{slug}/plan")
def plan_build(
    slug: str,
    request: Request,
    user: User = Depends(auth.require_user),
    csrf_token: str = Form(""),
    remove_named_volumes: str | None = Form(None),
    approved_volume: list[str] | None = Form(None),
    remove_images: str = Form("none"),
    remove_bind_data: str | None = Form(None),
    remove_repo: str | None = Form(None),
    remove_networks: str | None = Form(None),
    backup: str = Form("none"),
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    if planner is None:  # pragma: no cover
        return JSONResponse({"error": "planner unavailable"}, status_code=503)

    options = {
        "remove_named_volumes": bool(remove_named_volumes),
        "approved_volumes": approved_volume or [],
        "remove_images": remove_images,
        "remove_bind_data": bool(remove_bind_data),
        "remove_repo": bool(remove_repo),
        "remove_networks": bool(remove_networks),
        "backup": backup,
    }
    plan = planner.build_plan(slug, options)
    planner.persist_plan(plan)
    plan_id = plan.id if hasattr(plan, "id") else plan.get("id")
    auditlog.audit(user.id, "plan.build", slug, {"options": options, "plan_id": plan_id})
    return RedirectResponse(url=f"/plans/{plan_id}", status_code=303)


@router.post("/apps/{slug}/remove")
def app_remove_now(
    slug: str,
    request: Request,
    user: User = Depends(auth.require_user),
    csrf_token: str = Form(""),
) -> Response:
    """One-click complete removal: build a plan with every removal option
    on (all named volumes approved), then run it live immediately. The
    browser shows a single native confirm before submitting."""
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    if jobs is None or planner is None:  # pragma: no cover
        return JSONResponse({"error": "jobs engine unavailable"}, status_code=503)

    conn = get_db()
    try:
        rows = _rows(q(conn, "SELECT * FROM applications WHERE slug = ?", (slug,)))
        if not rows:
            raise HTTPException(status_code=404, detail=f"no such application: {slug}")
        if rows[0].get("protected"):
            return JSONResponse({"error": "protected apps cannot be removed"}, status_code=403)
        volumes = _rows(
            q(
                conn,
                """
                SELECT r.key AS key FROM resources r
                JOIN associations a ON a.resource_id = r.id
                JOIN applications ap ON ap.id = a.app_id
                WHERE ap.slug = ? AND r.type = 'volume'
                """,
                (slug,),
            )
        )
    finally:
        conn.close()

    options = {
        "remove_named_volumes": True,
        "approved_volumes": [v["key"] for v in volumes],
        "remove_images": "exclusive",
        "remove_bind_data": True,
        "remove_repo": True,
        "remove_networks": True,
        "backup": "none",
    }
    plan = planner.build_plan(slug, options)
    plan_id = planner.persist_plan(plan)
    auditlog.audit(user.id, "plan.build", slug, {"options": options, "plan_id": plan_id, "one_click": True})

    phrase = getattr(jobs, "CONFIRM_VOLUMES_PHRASE", "y")
    job_id = jobs.create_job(plan_id, "live", user.id)
    jobs.execute_job(job_id, confirm_phrase=phrase)
    auditlog.audit(user.id, "job.execute", f"plan#{plan_id}", {"mode": "live", "one_click": True})
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


def _load_plan_dict(plan_id: int) -> dict | None:
    """Load a persisted, HMAC-verified plan for display, via planner's
    load_plan (best-effort verification: falls back to the unverified
    load if the integrity check fails, but flags it in the rendered plan)."""
    if planner is None:  # pragma: no cover
        return None
    try:
        plan, stored_hmac, recomputed_hmac = planner.load_plan(plan_id)
    except Exception:
        return None
    tampered = bool(stored_hmac) and stored_hmac != recomputed_hmac
    row = plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)
    stages: dict[str, list] = {}
    for step in row.get("steps", []):
        stages.setdefault(step.get("stage", "other"), []).append(step)
    row["stages"] = stages
    row["tampered"] = tampered
    row.setdefault("status", "draft")
    row.setdefault("created", None)
    return row


@router.get("/plans/{plan_id}", response_class=HTMLResponse)
def plan_view(
    plan_id: int, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    plan_row = _load_plan_dict(plan_id)
    if plan_row is None:
        raise HTTPException(status_code=404, detail=f"no such plan: {plan_id}")
    conn = get_db()
    try:
        app_rows = _rows(q(conn, "SELECT * FROM applications WHERE slug = ?", (plan_row["app_slug"],)))
    finally:
        conn.close()
    app = app_rows[0] if app_rows else {"slug": plan_row["app_slug"], "name": plan_row["app_slug"]}
    return _render("plan.html", request, response, app=app, volumes=[], plan=plan_row)


@router.post("/plans/{plan_id}/execute")
def plan_execute(
    plan_id: int,
    request: Request,
    user: User = Depends(auth.require_user),
    csrf_token: str = Form(""),
    mode: str = Form("dry_run"),
    confirm_phrase: str = Form(""),
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    if jobs is None or planner is None:  # pragma: no cover
        return JSONResponse({"error": "jobs engine unavailable"}, status_code=503)

    try:
        plan = planner.verify_plan(plan_id)
    except planner.PlanNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except planner.PlanError as exc:
        # The resource exists but cannot be trusted as-is (e.g. tampered
        # steps_json). Anything genuinely unexpected propagates and becomes
        # a 500 rather than being silently reported here.
        return JSONResponse({"error": str(exc)}, status_code=409)

    has_volume_deletion = any(step.operation == "volume_rm" for step in plan.steps)
    required_phrase = getattr(jobs, "CONFIRM_VOLUMES_PHRASE", "y")
    if mode == "live" and has_volume_deletion and confirm_phrase != required_phrase:
        return JSONResponse(
            {"error": "typed confirmation phrase required for live volume deletion"},
            status_code=400,
        )

    job_id = jobs.create_job(plan_id, mode, user.id)
    jobs.execute_job(job_id, confirm_phrase=(confirm_phrase if mode == "live" else None))
    auditlog.audit(user.id, "job.execute", f"plan#{plan_id}", {"mode": mode})
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.get("/jobs", response_class=HTMLResponse)
def jobs_list(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        job_rows = _rows(
            q(
                conn,
                """
                SELECT j.*, ap.slug AS app_slug, ap.name AS app_name
                FROM jobs j
                LEFT JOIN plans p ON p.id = j.plan_id
                LEFT JOIN applications ap ON ap.id = p.app_id
                ORDER BY j.id DESC
                LIMIT ?
                """,
                (JOBS_PAGE_LIMIT,),
            )
        )
    finally:
        conn.close()
    for j in job_rows:
        j["duration"] = _duration(j.get("started"), j.get("finished"))
    return _render("jobs.html", request, response, jobs=job_rows)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(
    job_id: int, request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    conn = get_db()
    try:
        rows = q(
            conn,
            """
            SELECT j.*, ap.slug AS app_slug, ap.name AS app_name
            FROM jobs j
            LEFT JOIN plans p ON p.id = j.plan_id
            LEFT JOIN applications ap ON ap.id = p.app_id
            WHERE j.id = ?
            """,
            (job_id,),
        )
        job = _rows(rows)[0] if rows else {"id": job_id, "status": "unknown"}
        steps = _rows(
            q(conn, "SELECT * FROM job_steps WHERE job_id = ? ORDER BY seq", (job_id,))
        )
    finally:
        conn.close()
    for s in steps:
        s["duration"] = _duration(s.get("started"), s.get("finished"))
    stages: list[dict] = []
    for s in steps:
        st = s.get("stage") or "other"
        if not stages or stages[-1]["stage"] != st:
            stages.append({"stage": st, "steps": []})
        stages[-1]["steps"].append(s)

    # Same shape jobs.job_status() computes, so the initial server render and
    # the first /jobs/{id}/status poll agree on progress/current step.
    terminal_states = {"done", "failed"}
    total = len(steps)
    done_count = sum(1 for s in steps if s.get("state") in terminal_states)
    progress = {
        "done": done_count,
        "total": total,
        "pct": int(round(100 * done_count / total)) if total else 0,
    }
    current_step = next((s for s in steps if s.get("state") == "running"), None)

    return _render(
        "job_detail.html",
        request,
        response,
        job=job,
        steps=steps,
        stages=stages,
        progress=progress,
        current_step=current_step,
    )


@router.get("/jobs/{job_id}/status")
def job_status(job_id: int, user: User = Depends(auth.require_user)) -> JSONResponse:
    if jobs is None:  # pragma: no cover
        return JSONResponse({"error": "jobs engine unavailable"}, status_code=503)
    try:
        status = jobs.job_status(job_id)
    except Exception as exc:
        job_error = getattr(jobs, "JobError", Exception)
        if isinstance(exc, job_error):
            return JSONResponse({"error": str(exc)}, status_code=404)
        raise
    return JSONResponse(status)
