"""Tests for the DEL web UI layer (del_app.web.routes).

Builds a throwaway FastAPI app around `router`, points settings at a tmp
config/db (mirroring test_core.py's pattern), and monkeypatches the
planner/jobs sibling-lane modules with simple fakes.
"""
from __future__ import annotations

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


def test_dashboard_app_count_excludes_stale_scan_apps(authed_client, settings_env):
    """The dashboard's 'Applications' stat card must match the /apps default
    view: an application only present in an older scan (stale last_seen)
    must not inflate the count."""
    from del_app.db import get_db, x

    conn = get_db()
    try:
        old_scan_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        new_scan_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, last_seen) VALUES (?,?,?,?,?)",
            ("current-app", "Current App", "running", "compose", new_scan_id),
        )
        x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, last_seen) VALUES (?,?,?,?,?)",
            ("stale-removed-app", "Stale Removed App", "running", "compose", old_scan_id),
        )
        conn.commit()
    finally:
        conn.close()

    resp = authed_client.get("/")
    assert resp.status_code == 200
    # exactly one app (current-app) counted, not both
    assert '<div class="stat-value">1</div>' in resp.text


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


def test_view_apps_only_renders_current_enabled_healthy_domains(
    authed_client, settings_env, monkeypatch
):
    """Gallery is an additive live view, not a dump of stale/broken Nginx data."""
    import json

    from del_app.db import get_db, x

    conn = get_db()
    try:
        old_scan = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        scan_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        good_app = x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("good-app", "Good App", "running", "compose", scan_id, scan_id),
        )
        bad_app = x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("bad-app", "Bad App", "running", "compose", scan_id, scan_id),
        )
        stale_app = x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("stale-app", "Stale App", "running", "compose", old_scan, old_scan),
        )

        def add_site(key, domain, enabled, seen):
            return x(
                conn,
                "INSERT INTO resources (type, key, display, state, data_json, first_seen, last_seen) "
                "VALUES ('nginx_site',?,?,?,?,?,?)",
                (key, key, "enabled" if enabled else "available", json.dumps({
                    "enabled": enabled, "server_names": [domain],
                }), seen, seen),
            )

        good_site = add_site("good-site", "good-app.bjk.ai", True, scan_id)
        bad_site = add_site("bad-site", "bad-app.bjk.ai", True, scan_id)
        disabled_site = add_site("disabled-site", "disabled.bjk.ai", False, scan_id)
        stale_site = add_site("stale-site", "stale-app.bjk.ai", True, old_scan)
        for app_id, resource_id in (
            (good_app, good_site), (bad_app, bad_site),
            (good_app, disabled_site), (stale_app, stale_site),
        ):
            x(
                conn,
                "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared) "
                "VALUES (?,?,?,?,0)",
                (app_id, resource_id, 90, "exclusive"),
            )
        conn.commit()
    finally:
        conn.close()

    def fake_probes(domains, force=False):
        assert set(domains) == {"good-app.bjk.ai", "bad-app.bjk.ai"}
        return {
            "good-app.bjk.ai": {
                "healthy": True, "status": 200, "latency_ms": 42,
                "checked_at": "2026-08-03T12:00:00+00:00",
            },
            "bad-app.bjk.ai": {
                "healthy": False, "status": 502, "latency_ms": 15,
                "checked_at": "2026-08-03T12:00:00+00:00",
            },
        }

    monkeypatch.setattr(routes, "_probe_domains", fake_probes)
    resp = authed_client.get("/view-apps")
    assert resp.status_code == 200
    assert "Good App" in resp.text
    assert "good-app.bjk.ai" in resp.text
    assert "HTTP 200" in resp.text
    assert "42 ms" in resp.text
    assert "Bad App" not in resp.text
    assert "disabled.bjk.ai" not in resp.text
    assert "stale-app.bjk.ai" not in resp.text
    assert "1 unavailable hidden" in resp.text
    assert 'id="gallery-layout-toggle"' in resp.text
    assert 'data-gallery-view="grid"' in resp.text
    assert 'href="/view-apps"' in resp.text


def test_view_apps_domain_validation_and_category_helpers():
    assert routes._valid_gallery_domain("Example.BJK.AI.") == "example.bjk.ai"
    assert routes._valid_gallery_domain("*.bjk.ai") is None
    assert routes._valid_gallery_domain("192.168.1.164") is None
    assert routes._valid_gallery_domain("localhost") is None
    assert routes._gallery_category("jellyfin", "Jellyfin", "jellyfin.bjk.ai") == "Media & Streaming"
    # The .ai public suffix must not classify every app as AI.
    assert routes._gallery_category("plainpad", "Plainpad", "plainpad.bjk.ai") != "AI & Automation"


def test_parse_and_format_dt_helpers():
    """Docker Created, sqlite scan times → Eastern MM-DD-YY H:MM AM/PM.

    Offsets verified against zoneinfo America/New_York (EDT=UTC-4, EST=UTC-5).
    """
    # 2026-05-13 11:31 UTC = 7:31 AM EDT
    assert routes._format_dt("2026-05-13T11:31:00.840175564Z", date_only=True) == "05-13-26"
    assert routes._format_dt("2026-05-13T11:31:00.840175564Z") == "05-13-26 7:31 AM"
    # sqlite-style naive UTC: 2026-07-26 19:04:30 UTC = 3:04 PM EDT
    assert routes._format_dt("2026-07-26 19:04:30") == "07-26-26 3:04 PM"
    # day-boundary: UTC midnight → previous evening in Eastern (EST UTC-5 in Feb)
    assert routes._format_dt("2026-02-01T00:00:00Z") == "01-31-26 7:00 PM"
    assert routes._format_dt(None) == "—"
    assert routes._format_dt("not-a-date") == "—"
    assert "ET" not in routes._format_dt("2026-05-13T11:31:00Z")
    assert routes._iso_sort_key("2026-05-13T11:31:00Z") == "2026-05-13T11:31:00Z"
    # Parse treats naive as UTC and returns aware UTC
    parsed = routes._parse_dt("2026-07-26 19:04:30")
    assert parsed is not None and parsed.tzinfo is not None
    assert parsed.hour == 19
    # earliest of docker + dir signals
    rows = [
        {"type": "container", "data_json": '{"created": "2026-05-13T11:31:00Z"}'},
        {"type": "directory", "data_json": '{"ctime": "2026-01-01T00:00:00Z", "mtime": "2026-06-01T00:00:00Z"}'},
    ]
    # directory contributes ctime only (first of birth/ctime/mtime); earliest overall is dir ctime
    assert routes._installed_at_from_resources(rows).startswith("2026-01-01")
    assert routes._installed_at_from_resources([]) is None


def test_apps_list_shows_installed_column_and_container_date(authed_client, settings_env):
    """Apps table exposes an Installed column populated from container Created."""
    from del_app.db import get_db, x

    conn = get_db()
    try:
        scan_id = x(
            conn,
            "INSERT INTO scans (status, started, finished) VALUES ('done', '2026-07-01 12:00:00', '2026-07-01 12:01:00')",
        )
        app_id = x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("dated-app", "Dated App", "running", "compose", scan_id, scan_id),
        )
        cont_id = x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "container",
                "dated-app-web",
                "dated-app-web",
                "running",
                '{"created": "2026-03-15T08:00:00.123456789Z", "published_ports": [9090]}',
                scan_id,
                scan_id,
            ),
        )
        x(
            conn,
            "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared) "
            "VALUES (?,?,?,?,?)",
            (app_id, cont_id, 100, "exclusive", 0),
        )
        conn.commit()
    finally:
        conn.close()

    resp = authed_client.get("/apps")
    assert resp.status_code == 200
    assert "Installed" in resp.text
    # 2026-03-15T08:00:00Z → 03-15-26 4:00 AM (EDT)
    assert "03-15-26 4:00 AM" in resp.text
    assert "dated-app" in resp.text
    assert 'data-export-table="apps-table"' in resp.text
    assert "Show removed too" in resp.text


def test_apps_list_show_removed_toggle(authed_client, settings_env):
    """Default list hides apps not in the latest completed scan; ?show=removed keeps them."""
    from del_app.db import get_db, x

    conn = get_db()
    try:
        old_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        new_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("live-one", "Live One", "running", "compose", new_id, new_id),
        )
        x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("gone-one", "Gone One", "stopped", "compose", old_id, old_id),
        )
        conn.commit()
    finally:
        conn.close()

    live = authed_client.get("/apps")
    assert live.status_code == 200
    assert "Live One" in live.text
    assert "Gone One" not in live.text

    all_apps = authed_client.get("/apps", params={"show": "removed"})
    assert all_apps.status_code == 200
    assert "Live One" in all_apps.text
    assert "Gone One" in all_apps.text
    assert "including removed" in all_apps.text
    assert "removed" in all_apps.text  # badge on removed row


def test_app_detail_shows_human_dates_not_raw_scan_ids(authed_client, settings_env):
    from del_app.db import get_db, x

    conn = get_db()
    try:
        scan_id = x(
            conn,
            "INSERT INTO scans (status, started, finished) VALUES ('done', '2026-06-10 09:30:00', '2026-06-10 09:31:00')",
        )
        app_id = x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("detail-dates", "Detail Dates", "running", "compose", scan_id, scan_id),
        )
        cont_id = x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "container",
                "detail-dates-web",
                "detail-dates-web",
                "running",
                '{"created": "2026-02-01T00:00:00Z"}',
                scan_id,
                scan_id,
            ),
        )
        x(
            conn,
            "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared) "
            "VALUES (?,?,?,?,?)",
            (app_id, cont_id, 100, "exclusive", 0),
        )
        conn.commit()
    finally:
        conn.close()

    resp = authed_client.get("/apps/detail-dates")
    assert resp.status_code == 200
    assert "Installed" in resp.text
    # container Created 2026-02-01T00:00:00Z → 01-31-26 7:00 PM (EST)
    assert "01-31-26 7:00 PM" in resp.text
    # scan started 2026-06-10 09:30:00 UTC → 06-10-26 5:30 AM (EDT)
    assert "06-10-26 5:30 AM" in resp.text
    assert "First seen by DEL" in resp.text
    assert "ET" not in resp.text


def test_latest_scan_id_ignores_running_scans(authed_client, settings_env):
    """During a post-removal rescan the UI must keep showing the last completed scan."""
    from del_app.db import get_db, x

    conn = get_db()
    try:
        done_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, last_seen) VALUES (?,?,?,?,?)",
            ("still-here", "Still Here", "running", "compose", done_id),
        )
        # In-progress scan must not empty the apps list
        x(conn, "INSERT INTO scans (status) VALUES ('running')")
        conn.commit()
    finally:
        conn.close()

    resp = authed_client.get("/apps")
    assert resp.status_code == 200
    assert "Still Here" in resp.text


def test_dashboard_shows_last_scan_strip(authed_client, settings_env):
    from del_app.db import get_db, x

    conn = get_db()
    try:
        x(
            conn,
            "INSERT INTO scans (status, started, finished) VALUES ('done', '2026-07-26 10:00:00', '2026-07-26 10:01:30')",
        )
        conn.commit()
    finally:
        conn.close()

    resp = authed_client.get("/")
    assert resp.status_code == 200
    assert "Last scan" in resp.text


def test_shell_has_glossary_sidebar_and_collapse_controls(authed_client):
    """Authenticated pages include the glossary rail, FAB, and nav collapse UI."""
    resp = authed_client.get("/apps")
    assert resp.status_code == 200
    assert 'data-glossary="apps"' in resp.text
    assert 'id="glossary-rail"' in resp.text
    assert 'id="glossary-fab"' in resp.text
    assert 'id="sidebar-collapse"' in resp.text
    assert 'id="nav-toggle"' in resp.text
    assert "Glossary" in resp.text
    # Apps-context glossary terms
    assert "Warnings" in resp.text
    assert "Protected" in resp.text
    assert "Status" in resp.text


def test_resources_glossary_context_container(authed_client):
    resp = authed_client.get("/resources/container")
    assert resp.status_code == 200
    assert 'data-glossary="resources-container"' in resp.text
    assert "State / health" in resp.text or "State / health" in resp.text.replace("—", "")
    assert "healthy" in resp.text.lower()
    assert "no healthcheck" in resp.text.lower() or "healthcheck" in resp.text.lower()


def test_resources_glossary_context_volume_and_image(authed_client):
    for path, needle in (
        ("/resources/volume", "Orphan"),
        ("/resources/image", "Dangling"),
        ("/resources/network", "Shared"),
        ("/resources/directory", "Dirty"),
        ("/resources/git_repo", "Dirty"),
    ):
        resp = authed_client.get(path)
        assert resp.status_code == 200, path
        assert needle in resp.text, path


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


def test_app_detail_excludes_associations_from_stale_scan(authed_client, settings_env):
    """A resource whose last_seen predates the latest scan (i.e. it was
    removed/vanished in a later scan) must not show up as still associated
    with the app on its detail page."""
    from del_app.db import get_db, x

    conn = get_db()
    try:
        old_scan_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        new_scan_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        app_id = x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, last_seen) VALUES (?,?,?,?,?)",
            ("stale-app", "Stale App", "running", "compose", new_scan_id),
        )
        stale_res_id = x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("container", "gone-container", "gone-container", "running", "{}", old_scan_id),
        )
        fresh_res_id = x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("container", "still-here", "still-here", "running", "{}", new_scan_id),
        )
        x(
            conn,
            "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared) "
            "VALUES (?,?,?,?,?)",
            (app_id, stale_res_id, 95, "exclusive", 0),
        )
        x(
            conn,
            "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared) "
            "VALUES (?,?,?,?,?)",
            (app_id, fresh_res_id, 95, "exclusive", 0),
        )
        conn.commit()
    finally:
        conn.close()

    resp = authed_client.get("/apps/stale-app")
    assert resp.status_code == 200
    assert "still-here" in resp.text
    assert "gone-container" not in resp.text


def test_orphans_grouped_and_review_only(authed_client, settings_env):
    _seed_resources(settings_env)
    resp = authed_client.get("/orphans")
    assert resp.status_code == 200
    assert "orphan-image" in resp.text
    assert "orphan" in resp.text.lower()
    assert "Actionable" in resp.text
    assert 'data-glossary="orphans"' in resp.text


def test_orphan_classification_filters_system_noise():
    """Vendor systemd, docker builtins, and OS cron are System — not Actionable."""
    from del_app.web.routes import classify_orphan_candidate

    nm = classify_orphan_candidate(
        "systemd_unit",
        "NetworkManager.service",
        "NetworkManager.service",
        "/lib/systemd/system/NetworkManager.service",
        {"is_custom": False, "fragment_path": "/lib/systemd/system/NetworkManager.service"},
    )
    assert nm["bucket"] == "system"

    none_net = classify_orphan_candidate(
        "network", "none", "none", None, {"driver": "null"},
    )
    assert none_net["bucket"] == "system"

    cron = classify_orphan_candidate(
        "cron_entry", "/etc/cron.daily/logrotate", "[daily] logrotate",
        "/etc/cron.daily/logrotate", {"command": "/etc/cron.daily/logrotate"},
    )
    assert cron["bucket"] == "system"

    ssh = classify_orphan_candidate(
        "port", "0.0.0.0:22", "0.0.0.0:22", None,
        {"port": 22, "process": "sshd", "systemd_unit": "ssh.service"},
    )
    assert ssh["bucket"] == "system"

    custom = classify_orphan_candidate(
        "systemd_unit",
        "myapp.service",
        "myapp.service",
        "/etc/systemd/system/myapp.service",
        {"is_custom": True, "fragment_path": "/etc/systemd/system/myapp.service"},
    )
    assert custom["bucket"] == "actionable"

    vol = classify_orphan_candidate(
        "volume", "leftover_data", "leftover_data", None, {"containers_using": []},
    )
    assert vol["bucket"] == "actionable"


def test_orphan_image_referenced_by_compose_project_is_expected_not_default(authed_client, settings_env):
    """Compose-declared unused image is Expected — hidden from default Actionable view,
    visible with ?show=all and a precise reason."""
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

    default = authed_client.get("/orphans")
    assert default.status_code == 200
    # Expected images are not in the default Actionable-only list
    assert "myregistry/retiredapp:v2" not in default.text

    all_view = authed_client.get("/orphans", params={"show": "all"})
    assert all_view.status_code == 200
    assert "myregistry/retiredapp:v2" in all_view.text
    assert "declared in compose project" in all_view.text
    assert "Expected" in all_view.text


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


def test_plan_form_has_complete_removal_preset(authed_client, settings_env):
    """The plan-build form must expose a one-click 'complete removal' preset
    that ticks every removal option, with a visible data-loss warning."""
    from del_app.db import get_db
    conn = get_db()
    try:
        _insert_app(conn, "presetapp", "Preset App")
    finally:
        conn.close()

    resp = authed_client.get("/apps/presetapp/plan")
    assert resp.status_code == 200
    assert 'id="preset-complete-removal"' in resp.text
    assert "Complete removal" in resp.text
    assert "permanently deletes all data" in resp.text
    # the individual options the preset must be able to drive are present
    for expected_id in (
        "remove-named-volumes", "remove_images", "remove-bind-data",
        "remove-repo", "remove-networks", "backup",
    ):
        assert f'id="{expected_id}"' in resp.text


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
        data={"csrf_token": csrf, "mode": "live", "confirm_phrase": "y"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/jobs/99"
    assert calls["create_job"] == (7, "live", 1)
    assert calls["execute_job"] == (99, "y")


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


# ---------------------------------------------------------------------------
# App-icon proxy (2026-08-24)
#
# The gallery used to point <img> straight at https://{domain}/favicon.ico.
# Any app behind HTTP basic auth answered 401 + WWW-Authenticate, and the
# browser opened a credential dialog on top of a page the operator was
# already authenticated to. Icons are now fetched server-side.
# ---------------------------------------------------------------------------

def _seed_enabled_site(domain: str) -> None:
    import json

    from del_app.db import get_db, x

    conn = get_db()
    try:
        scan_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, first_seen, last_seen) "
            "VALUES ('nginx_site',?,?,'enabled',?,?,?)",
            (f"site-{domain}", domain,
             json.dumps({"enabled": True, "server_names": [domain]}), scan_id, scan_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_app_icon_absorbs_upstream_basic_auth_challenge(
    authed_client, settings_env, monkeypatch
):
    """A 401 upstream must become an empty 204 — never a challenge the
    browser can turn into a credential prompt."""
    _seed_enabled_site("protected.bjk.ai")

    import urllib.error

    def raise_401(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized",
            {"WWW-Authenticate": 'Basic realm="WebNotepad++"'}, None,
        )

    monkeypatch.setattr(routes.urllib.request, "urlopen", raise_401)
    routes._ICON_CACHE.clear()

    resp = authed_client.get("/app-icon/protected.bjk.ai")
    assert resp.status_code == 204
    assert resp.content == b""
    assert "WWW-Authenticate" not in {k.title() for k in resp.headers}
    assert "www-authenticate" not in {k.lower() for k in resp.headers}


def test_app_icon_refuses_domains_not_in_the_current_inventory(
    authed_client, settings_env, monkeypatch
):
    """The proxy must not become a general-purpose outbound fetcher."""
    _seed_enabled_site("known.bjk.ai")
    called = []
    monkeypatch.setattr(
        routes, "_cached_icon", lambda d: called.append(d) or (b"x", "image/png")
    )

    for hostile in ("evil.example.com", "169.254.169.254", "localhost", "*.bjk.ai"):
        assert authed_client.get(f"/app-icon/{hostile}").status_code == 404
    assert called == [], f"a non-inventory domain was fetched: {called}"


def test_app_icon_serves_a_real_favicon_with_cache_headers(
    authed_client, settings_env, monkeypatch
):
    _seed_enabled_site("good.bjk.ai")
    monkeypatch.setattr(routes, "_cached_icon", lambda d: (b"\x89PNG-body", "image/png"))

    resp = authed_client.get("/app-icon/good.bjk.ai")
    assert resp.status_code == 200
    assert resp.content == b"\x89PNG-body"
    assert resp.headers["content-type"].startswith("image/png")
    assert "max-age" in resp.headers.get("cache-control", "")


def test_app_icon_requires_authentication(anon_client, settings_env):
    """Unauthenticated callers get the login redirect, not an outbound fetch."""
    assert anon_client.get("/app-icon/anything.bjk.ai", follow_redirects=False).status_code == 303


def test_gallery_markup_points_icons_at_the_proxy(
    authed_client, settings_env, monkeypatch
):
    import json

    from del_app.db import get_db, x

    conn = get_db()
    try:
        scan_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        app_id = x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("iconapp", "Icon App", "running", "compose", scan_id, scan_id),
        )
        rid = x(
            conn,
            "INSERT INTO resources (type, key, display, state, data_json, first_seen, last_seen) "
            "VALUES ('nginx_site',?,?,'enabled',?,?,?)",
            ("icon-site", "iconapp.bjk.ai",
             json.dumps({"enabled": True, "server_names": ["iconapp.bjk.ai"]}), scan_id, scan_id),
        )
        x(
            conn,
            "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared) "
            "VALUES (?,?,90,'exclusive',0)",
            (app_id, rid),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(routes, "_probe_domains", lambda domains, force=False: {
        "iconapp.bjk.ai": {"healthy": True, "status": 200, "latency_ms": 12,
                           "checked_at": "2026-08-24T00:00:00+00:00"},
    })
    resp = authed_client.get("/view-apps")
    assert resp.status_code == 200
    assert "/app-icon/iconapp.bjk.ai" in resp.text
    assert "https://iconapp.bjk.ai/favicon.ico" not in resp.text


# ---------------------------------------------------------------------------
# Orphan classifier + gallery categoriser precision (2026-08-24)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("comm", ["Xvfb", "NetworkManager", "Xorg"])
def test_mixed_case_system_processes_are_not_actionable(comm):
    """The classifier lowercases the process name before matching, so the
    marker list must be lowercase too — these three never matched and were
    reported as abandoned apps."""
    result = routes.classify_orphan_candidate("process", comm, comm, None, {"comm": comm})
    assert result["bucket"] == "system"


@pytest.mark.parametrize("unit", [
    "snap.cups.cupsd.service", "ollama.service", "avahi-daemon.service", "pm2-root.service",
])
def test_vendor_units_under_etc_systemd_are_not_actionable(unit):
    """snapd and friends write generated units into /etc/systemd/system, the
    very location the classifier uses to infer 'app-owned'."""
    result = routes.classify_orphan_candidate(
        "systemd_unit", unit, unit, None,
        {"is_custom": True, "fragment_path": f"/etc/systemd/system/{unit}"},
    )
    assert result["bucket"] == "system"


def test_agent_and_runtime_processes_under_a_project_dir_are_not_actionable():
    result = routes.classify_orphan_candidate(
        "process", "node", "node (pid 1)", None, {"comm": "node", "cwd": "/apps/del"},
    )
    assert result["bucket"] == "system"


def test_default_catchall_vhost_is_not_an_actionable_orphan():
    result = routes.classify_orphan_candidate(
        "nginx_site", "00-default", "00-default-reject-unknown.conf", None,
        {"enabled": True, "server_names": [], "upstreams": []},
    )
    assert result["bucket"] == "system"


def test_a_real_enabled_app_vhost_is_still_actionable():
    """The catch-all rule must not swallow genuine leftover sites."""
    result = routes.classify_orphan_candidate(
        "nginx_site", "leftover", "leftover.bjk.ai", None,
        {"enabled": True, "server_names": ["leftover.bjk.ai"],
         "upstreams": [{"port": 9000}]},
    )
    assert result["bucket"] == "actionable"


@pytest.mark.parametrize("slug,domain,expected", [
    # "vault" inside "streamvault" used to win Security & Identity outright
    ("streamvault-iptv", "streamvault-iptv.bjk.ai", "Media & Streaming"),
    ("vaultwarden", "vaultwarden.bjk.ai", "Security & Identity"),
    ("affine", "affine.bjk.ai", "Notes & Knowledge"),
    ("linkwarden", "linkwarden.bjk.ai", "Notes & Knowledge"),
    ("opengist", "opengist.bjk.ai", "Developer Tools"),
    ("portracker", "portracker.bjk.ai", "Infrastructure"),
    ("twenty", "twenty.bjk.ai", "Business & Finance"),
    ("homepage", "homepage.bjk.ai", "Infrastructure"),
])
def test_gallery_categories_for_previously_misfiled_apps(slug, domain, expected):
    assert routes._gallery_category(slug, slug.title(), domain) == expected


def test_gallery_category_ignores_the_public_suffix():
    """Every endpoint here is *.bjk.ai; the TLD must not put them all in AI."""
    assert routes._gallery_category("wallos", "Wallos", "wallos.bjk.ai") != "AI & Automation"
    # but a real AI app still matches on a word-boundary hit
    assert routes._gallery_category("ai-tools", "AI Tools", "ai-tools.bjk.ai") == "AI & Automation"


# ---------------------------------------------------------------------------
# Gallery health probing: stale-while-revalidate (2026-08-24)
# ---------------------------------------------------------------------------

def test_stale_probes_are_served_immediately_and_refreshed_in_background(monkeypatch):
    """Probing ~125 domains takes seconds; with the old 120s hard TTL a
    browsing operator paid that cost every two minutes. A stale entry must be
    served at once and refreshed off the request thread."""
    import time as _time

    routes._APP_PROBE_CACHE.clear()
    routes._PROBE_REFRESHING.clear()

    calls = []

    def slow_probe(domain):
        calls.append(domain)
        _time.sleep(0.25)
        return {"healthy": True, "status": 200, "latency_ms": 5,
                "error": "", "checked_at": "2026-08-24T00:00:00+00:00"}

    monkeypatch.setattr(routes, "_probe_domain", slow_probe)

    # First call has nothing cached: it must block and actually probe.
    started = _time.perf_counter()
    first = routes._probe_domains(["a.example.com", "b.example.com"])
    blocked_for = _time.perf_counter() - started
    assert set(first) == {"a.example.com", "b.example.com"}
    assert blocked_for >= 0.2, "an empty cache must block rather than render an empty gallery"

    # Force both entries stale.
    with routes._APP_PROBE_LOCK:
        for entry in routes._APP_PROBE_CACHE.values():
            entry["cached_at"] = 0.0
    calls.clear()

    started = _time.perf_counter()
    second = routes._probe_domains(["a.example.com", "b.example.com"])
    served_in = _time.perf_counter() - started
    assert set(second) == {"a.example.com", "b.example.com"}, "stale data must still be served"
    assert served_in < 0.15, f"stale read blocked for {served_in:.3f}s; should be immediate"

    # The refresh happens, just not on the request thread.
    deadline = _time.time() + 3
    while _time.time() < deadline and not calls:
        _time.sleep(0.05)
    assert calls, "no background refresh was started for the stale entries"

    routes._APP_PROBE_CACHE.clear()
    routes._PROBE_REFRESHING.clear()


def test_forced_refresh_still_blocks_and_reprobes(monkeypatch):
    """?refresh=1 is an explicit 'check again now' — it must not serve stale."""
    routes._APP_PROBE_CACHE.clear()
    routes._PROBE_REFRESHING.clear()
    calls = []
    monkeypatch.setattr(routes, "_probe_domain", lambda d: calls.append(d) or {
        "healthy": True, "status": 200, "latency_ms": 1, "error": "",
        "checked_at": "2026-08-24T00:00:00+00:00",
    })
    routes._probe_domains(["x.example.com"])
    calls.clear()
    routes._probe_domains(["x.example.com"], force=True)
    assert calls == ["x.example.com"], "forced refresh did not re-probe"
    routes._APP_PROBE_CACHE.clear()
