"""Tests for the assistant web layer (del_app.web.assistant).

The assistant lane (`del_app.assistant`) is faked with a SimpleNamespace
monkeypatched onto `del_app.web.assistant.assistant`, so these tests do not
depend on the real provider, context builders or store.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from del_app import auth
from del_app.auth import NeedsLogin, User
from del_app.config import get_settings
from del_app.db import get_db, run_migrations, x
from del_app.web import assistant as assistant_web
from del_app.web import routes

SCOPES = ["general", "app", "orphans", "resource_type", "resource"]
RTYPES = ["container", "image", "network", "volume"]


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
    with TestClient(_build_app(True), base_url="http://testserver") as client:
        yield client


@pytest.fixture()
def anon_client(settings_env):
    with TestClient(_build_app(False), base_url="http://testserver") as client:
        yield client


def _with_csrf(client: TestClient) -> str:
    raw = "test-session-token"
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.sign_token(raw))
    return auth.csrf_token(raw)


# ---------------------------------------------------------------------------
# Fake assistant lane
# ---------------------------------------------------------------------------

class FakeAssistantError(Exception):
    def __init__(self, kind, message, status=400):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


def _fake_assistant(*, enabled=True, configured=True, ask=None, calls=None):
    calls = calls if calls is not None else {}
    conversations = {
        7: {
            "id": 7, "user_id": 1, "scope": "app", "target": "testapp",
            "title": "Explain what this app consists of",
            "created_at": "2026-09-10T10:00:00", "updated_at": "2026-09-10T10:01:00",
            "messages": [
                {"id": 1, "role": "user", "content": "Explain what this app consists of"},
                {"id": 2, "role": "assistant", "content": "## Summary\n- one **container**\n- `vol-a`"},
            ],
        },
    }

    def status():
        return {
            "enabled": enabled,
            "configured": configured,
            "model": "glm-5.3-flash" if enabled else None,
            "key_source": "file" if configured else None,
            "reason": None if configured else "no API key configured",
        }

    def default_ask(**kw):
        calls["ask"] = kw
        if not (enabled and configured):
            raise FakeAssistantError("disabled", "Assistant is not configured", 503)
        if kw["scope"] == "app" and kw["target"] == "missing":
            raise FakeAssistantError("target", "no such application: missing", 404)
        yield {"type": "meta", "conversation_id": 11, "message_id": 5, "scope": kw["scope"],
               "target": kw["target"], "context_truncated": True}
        yield {"type": "delta", "text": "Hello "}
        yield {"type": "delta", "text": "**world**"}
        yield {"type": "done", "usage": {"prompt_eval_count": 10, "eval_count": 3, "ms": 120}}

    def prompt_library(scope):
        lib = [
            {"id": "general.overview", "scope": "general", "label": "Summarise", "text": "Summarise this server", "description": "d"},
            {"id": "rtype.review", "scope": "resource_type", "label": "Review all <type>s", "text": "Review all <type>s and flag shared", "description": "d"},
            {"id": "res.safe", "scope": "resource", "label": "Is this <type> safe to delete?", "text": "Is it safe?", "description": "d"},
            {"id": "app.explain", "scope": "app", "label": "Explain", "text": "Explain this app", "description": "d"},
        ]
        return [p for p in lib if p["scope"] == scope]

    def list_targets(conn, scope, resource_type=None):
        calls["list_targets"] = (scope, resource_type)
        if scope == "app":
            return [{"value": "testapp", "label": "Test App"}]
        if scope == "resource":
            return [{"value": f"{resource_type}:abc", "label": f"{resource_type} abc"}]
        return []

    def test_connection():
        calls["test_connection"] = True
        if not configured:
            raise FakeAssistantError("auth", "API key rejected", 503)
        return {"ok": True, "model": "glm-5.3-flash", "latency_ms": 42}

    store = SimpleNamespace(
        list_conversations=lambda conn, user_id, limit=30: [
            {k: v for k, v in c.items() if k != "messages"}
            for c in conversations.values() if c["user_id"] == user_id
        ],
        get_conversation=lambda conn, cid, user_id: (
            conversations.get(cid) if conversations.get(cid, {}).get("user_id") == user_id else None
        ),
        delete_conversation=lambda conn, cid, user_id: (
            conversations.pop(cid, None) is not None if conversations.get(cid, {}).get("user_id") == user_id else False
        ),
    )
    return SimpleNamespace(
        ask=ask or default_ask,
        status=status,
        prompt_library=prompt_library,
        list_targets=list_targets,
        test_connection=test_connection,
        AssistantError=FakeAssistantError,
        SCOPES=SCOPES,
        RESOURCE_TYPE_TARGETS=RTYPES,
        store=store,
    )


@pytest.fixture()
def fake_on(monkeypatch):
    calls: dict = {}
    fake = _fake_assistant(calls=calls)
    monkeypatch.setattr(assistant_web, "assistant", fake)
    fake.calls = calls
    return fake


@pytest.fixture()
def fake_off(monkeypatch):
    fake = _fake_assistant(configured=False)
    monkeypatch.setattr(assistant_web, "assistant", fake)
    return fake


def _insert_scan_and_app(slug="testapp"):
    conn = get_db()
    try:
        scan_id = x(conn, "INSERT INTO scans (status) VALUES ('done')")
        x(conn, "INSERT INTO applications (slug, name, first_seen, last_seen) VALUES (?, ?, ?, ?)",
          (slug, "Test App", scan_id, scan_id))
        x(conn, "INSERT INTO resources (type, key, display, state, data_json, first_seen, last_seen) "
                "VALUES ('volume', 'data vol', 'data vol', 'ok', '{}', ?, ?)", (scan_id, scan_id))
        x(conn, "INSERT INTO resources (type, key, display, state, data_json, first_seen, last_seen) "
                "VALUES ('directory', '/srv/x', '/srv/x', 'ok', '{}', ?, ?)", (scan_id, scan_id))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# auth + page states
# ---------------------------------------------------------------------------

def test_anon_redirects_to_login(anon_client):
    for path in ("/assistant", "/assistant/status", "/assistant/prompts", "/assistant/conversations"):
        resp = anon_client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login"


def test_page_enabled_renders_composer_and_preselects(authed_client, fake_on):
    resp = authed_client.get("/assistant?scope=app&target=testapp")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="assistant-page"' in html
    assert 'data-scope="app"' in html
    assert 'data-target="testapp"' in html
    assert 'id="assistant-form"' in html
    assert '<meta name="csrf-token"' in html
    assert '<link rel="stylesheet" href="/static/assistant.css">' in html
    assert '<script src="/static/assistant.js"></script>' in html
    assert 'data-scope="general" aria-pressed="false"' in html
    assert 'data-scope="app" aria-pressed="true"' in html
    assert "app.explain" in html  # prompts for the preselected scope
    assert "Explain what this app consists of" in html  # recent conversation listed
    assert 'id="assistant-disabled"' not in html
    assert 'data-glossary="assistant"' in html


def test_page_disabled_shows_steps_and_no_composer(authed_client, fake_off):
    resp = authed_client.get("/assistant")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="assistant-disabled"' in html
    assert "ollama-api-key.txt" in html
    assert "systemctl restart del-web" in html
    assert "no API key configured" in html
    assert 'id="assistant-form"' not in html
    assert 'id="assistant-page"' not in html


def test_page_when_lane_missing(authed_client, monkeypatch):
    monkeypatch.setattr(assistant_web, "assistant", None)
    resp = authed_client.get("/assistant")
    assert resp.status_code == 200
    assert 'id="assistant-disabled"' in resp.text
    resp = authed_client.get("/assistant/prompts?scope=general")
    assert resp.status_code == 503
    assert resp.json() == {"error": "Assistant unavailable", "kind": "disabled"}


def test_page_unknown_scope_falls_back_to_general(authed_client, fake_on):
    resp = authed_client.get("/assistant?scope=bogus")
    assert resp.status_code == 200
    assert 'data-scope="general" aria-pressed="true"' in resp.text


def test_page_conversation_preselect_renders_messages(authed_client, fake_on):
    resp = authed_client.get("/assistant?conversation=7")
    assert resp.status_code == 200
    html = resp.text
    assert 'data-conversation="7"' in html
    assert 'data-scope="app"' in html
    assert 'class="assistant-msg assistant-msg-assistant" data-md' in html
    # raw markdown is escaped, not rendered server-side
    assert "## Summary" in html
    assert "<strong>container</strong>" not in html


# ---------------------------------------------------------------------------
# JSON endpoints
# ---------------------------------------------------------------------------

def test_status_json_never_contains_key(authed_client, fake_on, monkeypatch):
    fake_on.status = lambda: {"enabled": True, "configured": True, "model": "m",
                              "key_source": "env", "reason": None, "api_key": "sk-secret"}
    resp = authed_client.get("/assistant/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True
    assert "api_key" not in data
    assert "sk-secret" not in resp.text


def test_prompts_json_substitutes_type(authed_client, fake_on):
    resp = authed_client.get("/assistant/prompts?scope=resource_type&target=image")
    assert resp.status_code == 200
    prompts = resp.json()["prompts"]
    assert prompts[0]["id"] == "rtype.review"
    assert prompts[0]["label"] == "Review all images"
    assert "<type>" not in json.dumps(prompts)

    resp = authed_client.get("/assistant/prompts?scope=resource&target=volume:abc")
    assert resp.json()["prompts"][0]["label"] == "Is this volume safe to delete?"

    resp = authed_client.get("/assistant/prompts?scope=nope")
    assert resp.status_code == 400
    assert resp.json()["kind"] == "scope"


def test_targets_json(authed_client, fake_on):
    resp = authed_client.get("/assistant/targets?scope=app")
    assert resp.status_code == 200
    assert resp.json() == {"targets": [{"value": "testapp", "label": "Test App"}]}

    resp = authed_client.get("/assistant/targets?scope=resource_type")
    values = [t["value"] for t in resp.json()["targets"]]
    assert values == RTYPES

    resp = authed_client.get("/assistant/targets?scope=resource&type=volume")
    assert resp.json()["targets"] == [{"value": "volume:abc", "label": "volume abc"}]
    assert fake_on.calls["list_targets"] == ("resource", "volume")

    resp = authed_client.get("/assistant/targets?scope=resource")
    assert resp.status_code == 400
    resp = authed_client.get("/assistant/targets?scope=resource&type=cron_entry")
    assert resp.status_code == 400
    resp = authed_client.get("/assistant/targets?scope=general")
    assert resp.json() == {"targets": []}


def test_conversations_list_get_404(authed_client, fake_on):
    resp = authed_client.get("/assistant/conversations")
    assert resp.status_code == 200
    convs = resp.json()["conversations"]
    assert [c["id"] for c in convs] == [7]
    assert "messages" not in convs[0]

    resp = authed_client.get("/assistant/conversations/7")
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 2

    resp = authed_client.get("/assistant/conversations/999")
    assert resp.status_code == 404
    assert resp.json() == {"error": "conversation not found", "kind": "not_found"}


def test_conversation_delete_requires_csrf_then_redirects(authed_client, fake_on):
    resp = authed_client.post("/assistant/conversations/7/delete", data={"csrf_token": "bad"})
    assert resp.status_code == 403

    csrf = _with_csrf(authed_client)
    resp = authed_client.post(
        "/assistant/conversations/7/delete", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/assistant?flash=Conversation+deleted"

    resp = authed_client.post(
        "/assistant/conversations/7/delete", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.headers["location"] == "/assistant?error=Conversation+not+found"


# ---------------------------------------------------------------------------
# /assistant/ask
# ---------------------------------------------------------------------------

def _ask(client, csrf, body):
    return client.post("/assistant/ask", json=body, headers={"X-CSRF-Token": csrf})


def test_ask_bad_csrf_header_403(authed_client, fake_on):
    _with_csrf(authed_client)
    resp = authed_client.post("/assistant/ask", json={"scope": "general", "message": "hi"})
    assert resp.status_code == 403
    assert resp.json() == {"error": "invalid csrf token", "kind": "csrf"}
    resp = _ask(authed_client, "nope", {"scope": "general", "message": "hi"})
    assert resp.status_code == 403
    assert "ask" not in fake_on.calls


def test_ask_streams_ndjson(authed_client, fake_on):
    csrf = _with_csrf(authed_client)
    resp = _ask(authed_client, csrf, {
        "scope": "app", "target": "testapp", "message": "Explain", "prompt_id": "app.explain",
    })
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.headers["x-accel-buffering"] == "no"
    assert resp.headers["cache-control"] == "no-store"
    lines = [json.loads(line) for line in resp.text.strip().split("\n")]
    assert [ev["type"] for ev in lines] == ["meta", "delta", "delta", "done"]
    assert lines[0]["conversation_id"] == 11
    assert lines[0]["context_truncated"] is True
    assert lines[-1]["usage"]["eval_count"] == 3
    call = fake_on.calls["ask"]
    assert call["user_id"] == 1
    assert call["scope"] == "app" and call["target"] == "testapp"
    assert call["message"] == "Explain" and call["prompt_id"] == "app.explain"
    assert call["conversation_id"] is None


def test_ask_passes_conversation_id(authed_client, fake_on):
    csrf = _with_csrf(authed_client)
    resp = _ask(authed_client, csrf, {"scope": "general", "message": "more", "conversation_id": "11"})
    assert resp.status_code == 200
    assert fake_on.calls["ask"]["conversation_id"] == 11
    resp = _ask(authed_client, csrf, {"scope": "general", "message": "more", "conversation_id": "x"})
    assert resp.status_code == 400


def test_ask_503_when_not_configured(authed_client, fake_off):
    csrf = _with_csrf(authed_client)
    resp = _ask(authed_client, csrf, {"scope": "general", "message": "hi"})
    assert resp.status_code == 503
    assert resp.json() == {"error": "Assistant is not configured", "kind": "disabled"}
    assert not resp.headers["content-type"].startswith("application/x-ndjson")


def test_ask_400_bad_scope_and_empty_message(authed_client, fake_on):
    csrf = _with_csrf(authed_client)
    resp = _ask(authed_client, csrf, {"scope": "bogus", "message": "hi"})
    assert resp.status_code == 400
    assert resp.json()["kind"] == "scope"
    resp = _ask(authed_client, csrf, {"scope": "general", "message": "   "})
    assert resp.status_code == 400
    assert resp.json()["kind"] == "message"
    assert "ask" not in fake_on.calls


def test_ask_404_unknown_target(authed_client, fake_on):
    csrf = _with_csrf(authed_client)
    resp = _ask(authed_client, csrf, {"scope": "app", "target": "missing", "message": "hi"})
    assert resp.status_code == 404
    assert resp.json() == {"error": "no such application: missing", "kind": "target"}


def test_ask_429_when_busy(authed_client, monkeypatch):
    def busy_ask(**kw):
        raise FakeAssistantError("busy", "another request is in flight", 429)
        yield  # pragma: no cover - makes this a generator like the real ask()

    monkeypatch.setattr(assistant_web, "assistant", _fake_assistant(ask=busy_ask))
    csrf = _with_csrf(authed_client)
    resp = _ask(authed_client, csrf, {"scope": "general", "message": "hi"})
    assert resp.status_code == 429
    assert resp.json() == {"error": "another request is in flight", "kind": "busy"}


def test_ask_mid_stream_error_becomes_event(authed_client, monkeypatch):
    def flaky_ask(**kw):
        yield {"type": "meta", "conversation_id": 1, "message_id": 1, "scope": "general",
               "target": None, "context_truncated": False}
        yield {"type": "delta", "text": "partial"}
        raise FakeAssistantError("upstream", "connection reset", 502)

    monkeypatch.setattr(assistant_web, "assistant", _fake_assistant(ask=flaky_ask))
    csrf = _with_csrf(authed_client)
    resp = _ask(authed_client, csrf, {"scope": "general", "message": "hi"})
    assert resp.status_code == 200
    lines = [json.loads(line) for line in resp.text.strip().split("\n")]
    assert [ev["type"] for ev in lines] == ["meta", "delta", "error"]
    assert lines[-1] == {"type": "error", "kind": "upstream", "message": "connection reset"}


# ---------------------------------------------------------------------------
# Settings panel + test connection
# ---------------------------------------------------------------------------

def test_settings_panel_renders(authed_client, fake_on):
    resp = authed_client.get("/settings")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="assistant-settings"' in html
    assert "glm-5.3-flash" in html
    assert 'action="/assistant/test"' in html
    assert "Key source" in html and "file" in html


def test_settings_test_connection(authed_client, fake_on):
    resp = authed_client.post("/assistant/test", data={"csrf_token": "bad"})
    assert resp.status_code == 403
    csrf = _with_csrf(authed_client)
    resp = authed_client.post("/assistant/test", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/settings?flash=")
    assert "glm-5.3-flash" in resp.headers["location"]
    assert fake_on.calls["test_connection"] is True


def test_settings_test_connection_failure(authed_client, fake_off):
    csrf = _with_csrf(authed_client)
    resp = authed_client.post("/assistant/test", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/settings?error=")
    assert "API+key+rejected" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Deep links + base template + static assets
# ---------------------------------------------------------------------------

def test_deep_links_present_when_enabled(authed_client, fake_on):
    _insert_scan_and_app()
    html = authed_client.get("/apps/testapp").text
    assert 'href="/assistant?scope=app&amp;target=testapp"' in html

    html = authed_client.get("/orphans").text
    assert 'href="/assistant?scope=orphans"' in html

    html = authed_client.get("/resources/volume").text
    assert 'href="/assistant?scope=resource_type&amp;target=volume"' in html
    assert 'href="/assistant?scope=resource&amp;target=volume%3Adata%20vol"' in html

    # non-docker types: no page action, no per-row link
    html = authed_client.get("/resources/directory").text
    assert "/assistant?scope=resource_type" not in html
    assert "/assistant?scope=resource&amp;" not in html


def test_deep_links_absent_when_disabled(authed_client, fake_off):
    _insert_scan_and_app()
    for path in ("/apps/testapp", "/orphans", "/resources/volume"):
        html = authed_client.get(path).text
        assert "/assistant?" not in html, path


def test_nav_and_palette(authed_client, fake_on):
    html = authed_client.get("/").text
    assert 'href="/assistant" title="Assistant"' in html
    pages = authed_client.get("/palette.json").json()["pages"]
    assert {"title": "Assistant", "url": "/assistant"} in pages


def test_base_blocks_present():
    src = (assistant_web.__file__.rsplit("/", 1)[0] + "/templates/base.html")
    text = open(src).read()
    assert "{% block head %}{% endblock %}" in text
    assert "{% block scripts %}{% endblock %}" in text
    assert text.index("{% block head %}") < text.index("</head>")
    assert text.index("/static/app.js") < text.index("{% block scripts %}")


def test_static_assets_served(anon_client):
    js = anon_client.get("/static/assistant.js")
    assert js.status_code == 200
    assert js.headers["content-type"].startswith("application/javascript")
    assert "window.DEL.assistant" in js.text
    css = anon_client.get("/static/assistant.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")


_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I)
_ON_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)


def test_no_inline_scripts_or_handlers(authed_client, fake_on):
    _insert_scan_and_app()
    for path in ("/assistant", "/assistant?conversation=7", "/settings", "/resources/volume"):
        html = authed_client.get(path).text
        assert not _INLINE_SCRIPT.search(html), path
        assert not _ON_HANDLER.search(html), path
    templates_dir = assistant_web.__file__.rsplit("/", 1)[0] + "/templates/"
    for name in ("assistant.html", "settings.html", "base.html"):
        text = open(templates_dir + name).read()
        assert not _INLINE_SCRIPT.search(text), name
        assert not _ON_HANDLER.search(text), name
