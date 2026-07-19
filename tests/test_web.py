"""Tests for the DEL web UI layer (del_app.web.routes).

Builds a throwaway FastAPI app around `router`, points settings at a tmp
config/db (mirroring test_core.py's pattern), and monkeypatches the
planner/jobs sibling-lane modules with simple fakes.
"""
from __future__ import annotations

import types

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from del_app import auth
from del_app.auth import NeedsLogin, User
from del_app.config import get_settings
from del_app.db import run_migrations
from del_app.web import routes


@pytest.fixture()
def settings_env(tmp_path, monkeypatch):
    """Point DEL settings at a throwaway config + db for this test, and the
    secret key at a throwaway file so no real secrets are touched."""
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


def _build_app(override_auth: bool) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(NeedsLogin)
    async def _needs_login_handler(request: Request, exc: NeedsLogin) -> RedirectResponse:
        return RedirectResponse(url="/login", status_code=303)

    app.include_router(routes.router)

    if override_auth:
        app.dependency_overrides[auth.require_user] = lambda: User(id=1, username="tester")

    return app


@pytest.fixture()
def authed_client(settings_env):
    app = _build_app(override_auth=True)
    with TestClient(app, base_url="http://testserver") as client:
        yield client


@pytest.fixture()
def anon_client(settings_env):
    app = _build_app(override_auth=False)
    with TestClient(app, base_url="http://testserver") as client:
        yield client


def _with_csrf(client: TestClient) -> str:
    """Set a signed session cookie on the client and return the matching
    CSRF token (auth.check_csrf only needs a syntactically valid signed
    cookie value; it does not require a real DB-backed session row)."""
    raw = "test-session-token"
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.sign_token(raw))
    return auth.csrf_token(raw)


# ---------------------------------------------------------------------------
# auth boundary
# ---------------------------------------------------------------------------

def test_unauthenticated_redirects_to_login(anon_client):
    resp = anon_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_page_renders(anon_client):
    resp = anon_client.get("/login")
    assert resp.status_code == 200
    assert "username" in resp.text
    assert 'name="csrf_token"' in resp.text


# ---------------------------------------------------------------------------
# dashboard / apps
# ---------------------------------------------------------------------------

def test_dashboard_200(authed_client):
    resp = authed_client.get("/")
    assert resp.status_code == 200
    assert "Dashboard" in resp.text


def test_apps_list_200(authed_client):
    resp = authed_client.get("/apps")
    assert resp.status_code == 200
    assert "Applications" in resp.text


def test_apps_list_with_search_filter_200(authed_client):
    resp = authed_client.get("/apps", params={"search": "foo", "status": "active"})
    assert resp.status_code == 200


def test_apps_list_has_enhanced_table(authed_client):
    resp = authed_client.get("/apps")
    assert resp.status_code == 200
    assert "data-enhanced" in resp.text


# ---------------------------------------------------------------------------
# resources: tab bar counts, singular/plural handling, owner join
# ---------------------------------------------------------------------------

def _seed_resources(settings_env):
    """Insert an app, a container/volume resource, and associations so the
    resources/apps pages have real content to render."""
    from del_app.db import get_db, x

    conn = get_db()
    try:
        scan_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        app_id = x(
            conn,
            "INSERT INTO applications (slug, name, status, kind) VALUES (?,?,?,?)",
            ("web-owner", "Web Owner", "running", "compose"),
        )
        cont_id = x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            (
                "container",
                "abc123def456",
                "mycontainer",
                "running",
                '{"image": "nginx:latest", "state": "running", "published_ports": [8080]}',
                scan_id,
            ),
        )
        vol_id = x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("volume", "myvol", "myvol", "available", '{"containers_using": []}', scan_id),
        )
        # orphan (no association)
        x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("image", "sha256:deadbeef", "orphan-image", "unused", "{}", scan_id),
        )
        x(
            conn,
            "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared) "
            "VALUES (?,?,?,?,?)",
            (app_id, cont_id, 95, "exclusive", 0),
        )
        x(
            conn,
            "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared) "
            "VALUES (?,?,?,?,?)",
            (app_id, vol_id, 90, "exclusive", 1),
        )
        conn.commit()
    finally:
        conn.close()
    return app_id


def test_resources_singular_type_renders(authed_client, settings_env):
    _seed_resources(settings_env)
    resp = authed_client.get("/resources/container")
    assert resp.status_code == 200
    assert "data-enhanced" in resp.text
    assert "mycontainer" in resp.text
    # tab bar exposes every resource type with counts
    assert 'class="tabbar"' in resp.text
    assert "/resources/systemd_timer" in resp.text
    assert "/resources/nginx_site" in resp.text


def test_resources_plural_type_does_not_empty(authed_client, settings_env):
    """Legacy plural URL must resolve to the singular type, not render empty."""
    _seed_resources(settings_env)
    resp = authed_client.get("/resources/containers")
    assert resp.status_code == 200
    assert "mycontainer" in resp.text


def test_resources_owner_link_present(authed_client, settings_env):
    _seed_resources(settings_env)
    resp = authed_client.get("/resources/container")
    assert "/apps/web-owner" in resp.text  # owner-app cell links to app detail


def test_resources_shared_badge(authed_client, settings_env):
    _seed_resources(settings_env)
    resp = authed_client.get("/resources/volume")
    assert resp.status_code == 200
    assert "shared" in resp.text


def test_resources_tab_counts_reflect_latest_scan(authed_client, settings_env):
    _seed_resources(settings_env)
    resp = authed_client.get("/resources/container")
    # container count pill of 1 present in the tab bar
    assert "count-pill" in resp.text


def test_orphans_grouped_and_review_only(authed_client, settings_env):
    _seed_resources(settings_env)
    resp = authed_client.get("/orphans")
    assert resp.status_code == 200
    assert "orphan-image" in resp.text
    assert "orphan candidate" in resp.text.lower()


def test_orphan_image_referenced_by_compose_project_gets_specific_reason(authed_client, settings_env):
    """An unused image that a discovered (but not-running) compose project
    still declares gets a more specific reason than the generic one."""
    from del_app.db import get_db, x

    conn = get_db()
    try:
        scan_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            (
                "compose_project", "/apps/retiredapp", "retiredapp", "found",
                '{"working_dir": "/apps/retiredapp", "images": ["myregistry/retiredapp:v2"]}',
                scan_id,
            ),
        )
        x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            (
                "image", "sha256:cafefeed", "myregistry/retiredapp:v2", "unused",
                '{"repo_tag": "myregistry/retiredapp:v2", "dangling": false, "containers_using": []}',
                scan_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    resp = authed_client.get("/orphans")
    assert resp.status_code == 200
    assert "unused, but referenced by compose project retiredapp (not currently running)" in resp.text


def test_jobs_list_renders(authed_client, settings_env):
    resp = authed_client.get("/jobs")
    assert resp.status_code == 200
    assert "data-enhanced" in resp.text


# ---------------------------------------------------------------------------
# plan build (fake planner)
# ---------------------------------------------------------------------------

class _FakePlanStep:
    def __init__(self, seq, stage, operation, danger="safe", reversible=True, args=None, description=""):
        self.seq = seq
        self.stage = stage
        self.operation = operation
        self.danger = danger
        self.reversible = reversible
        self.args = args or {}
        self.description = description or operation

    def model_dump(self):
        return {
            "seq": self.seq,
            "stage": self.stage,
            "operation": self.operation,
            "danger": self.danger,
            "reversible": self.reversible,
            "args": self.args,
            "description": self.description,
        }


class _FakePlan:
    def __init__(self, id, app_slug, steps, options=None):
        self.id = id
        self.app_slug = app_slug
        self.steps = steps
        self.options = options or {}
        self.warnings = []
        self.preserved = []
        self.manual_followup = []
        self.est_reclaim_bytes = 0

    def model_dump(self):
        return {
            "id": self.id,
            "app_slug": self.app_slug,
            "steps": [s.model_dump() for s in self.steps],
            "options": self.options,
            "warnings": self.warnings,
            "preserved": self.preserved,
            "manual_followup": self.manual_followup,
            "est_reclaim_bytes": self.est_reclaim_bytes,
        }


def _insert_app(conn, slug="testapp", name="Test App"):
    from del_app.db import x
    return x(conn, "INSERT INTO applications (slug, name) VALUES (?, ?)", (slug, name))


def test_plan_post_builds_plan(authed_client, monkeypatch, settings_env):
    from del_app.db import get_db
    conn = get_db()
    try:
        _insert_app(conn, "testapp", "Test App")
    finally:
        conn.close()

    built = {}

    class _FakePlanner:
        def build_plan(self, slug, options):
            step = _FakePlanStep(1, "remove_runtime", "container_stop")
            plan = _FakePlan(id=None, app_slug=slug, steps=[step], options=options)
            built["plan"] = plan
            return plan

        def persist_plan(self, plan):
            plan.id = 42
            return 42

    monkeypatch.setattr(routes, "planner", _FakePlanner())

    csrf = _with_csrf(authed_client)
    resp = authed_client.post(
        "/apps/testapp/plan",
        data={"csrf_token": csrf, "backup": "none", "remove_images": "none"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/plans/42"
    assert built["plan"].app_slug == "testapp"


# ---------------------------------------------------------------------------
# plan execute: live volume deletion requires typed phrase
# ---------------------------------------------------------------------------

def test_execute_live_without_phrase_returns_400(authed_client, monkeypatch, settings_env):
    volume_step = _FakePlanStep(1, "remove_host", "volume_rm", danger="data_loss", reversible=False)
    fake_plan = _FakePlan(id=7, app_slug="testapp", steps=[volume_step])

    class _FakePlanner:
        def verify_plan(self, plan_id):
            assert plan_id == 7
            return fake_plan

    class _FakeJobs:
        def create_job(self, plan_id, mode, user_id):
            raise AssertionError("create_job must not be called without a valid confirm phrase")

        def execute_job(self, job_id, confirm_phrase=None):
            raise AssertionError("execute_job must not be called without a valid confirm phrase")

    monkeypatch.setattr(routes, "planner", _FakePlanner())
    monkeypatch.setattr(routes, "jobs", _FakeJobs())

    csrf = _with_csrf(authed_client)
    resp = authed_client.post(
        "/plans/7/execute",
        data={"csrf_token": csrf, "mode": "live"},
    )
    assert resp.status_code == 400
    assert "confirmation phrase" in resp.json()["error"]


def test_execute_live_with_correct_phrase_creates_job(authed_client, monkeypatch, settings_env):
    volume_step = _FakePlanStep(1, "remove_host", "volume_rm", danger="data_loss", reversible=False)
    fake_plan = _FakePlan(id=7, app_slug="testapp", steps=[volume_step])
    calls = {}

    class _FakePlanner:
        def verify_plan(self, plan_id):
            return fake_plan

    class _FakeJobs:
        def create_job(self, plan_id, mode, user_id):
            calls["create_job"] = (plan_id, mode, user_id)
            return 99

        def execute_job(self, job_id, confirm_phrase=None):
            calls["execute_job"] = (job_id, confirm_phrase)

    monkeypatch.setattr(routes, "planner", _FakePlanner())
    monkeypatch.setattr(routes, "jobs", _FakeJobs())

    csrf = _with_csrf(authed_client)
    resp = authed_client.post(
        "/plans/7/execute",
        data={"csrf_token": csrf, "mode": "live", "confirm_phrase": "DELETE VOLUMES"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/jobs/99"
    assert calls["create_job"] == (7, "live", 1)
    assert calls["execute_job"] == (99, "DELETE VOLUMES")


# ---------------------------------------------------------------------------
# job status polling endpoint
# ---------------------------------------------------------------------------

def test_job_status_json_shape(authed_client, monkeypatch):
    canned = {
        "id": 5,
        "status": "running",
        "steps": [
            {"seq": 1, "stage": "quiesce", "operation": "container_stop", "state": "done"},
            {"seq": 2, "stage": "remove_runtime", "operation": "container_rm", "state": "running"},
        ],
    }

    class _FakeJobs:
        def job_status(self, job_id):
            assert job_id == 5
            return canned

    monkeypatch.setattr(routes, "jobs", _FakeJobs())

    resp = authed_client.get("/jobs/5/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body == canned
    assert body["status"] == "running"
    assert len(body["steps"]) == 2
