"""Tests for del_app.assistant: provider NDJSON parsing and error mapping,
API key resolution, context builders, prompt library, store, and the ask()
service with a faked provider (no network)."""
from __future__ import annotations

import io
import json
import os
import urllib.error

import pytest

from del_app import auth
from del_app.assistant import (
    RESOURCE_TYPE_TARGETS,
    SCOPES,
    AssistantError,
    list_targets,
    prompt_library,
)
from del_app.assistant import context, provider, service, store
from del_app.assistant.prompts import PROMPT_LIBRARY, SYSTEM_PROMPT
from del_app.config import get_settings
from del_app.db import get_db, q, run_migrations, x
from del_app.web.queries import _rows


@pytest.fixture()
def settings_env(tmp_path, monkeypatch):
    """Throwaway config + db (same shape as test_web.py). The API key env var
    is cleared so the shell environment never leaks into these tests."""
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

[assistant]
api_key_file = "{tmp_path}/ollama-api-key.txt"
context_budget_chars = 24000
"""
    )
    monkeypatch.setenv("DEL_CONFIG_PATH", str(config_path))
    monkeypatch.delenv(provider.API_KEY_ENV, raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(auth, "SECRET_KEY_PATH", str(tmp_path / "secret.key"))
    settings = get_settings()
    run_migrations()
    yield settings
    get_settings.cache_clear()


def _seed(settings_env) -> dict:
    """Two current apps + one removed app; container/image/network/volume
    resources; a shared multi-owner image; a data-risk volume; an excluded
    association; two orphans (dangling image, unattached volume)."""
    conn = get_db()
    try:
        old_scan = x(conn, "INSERT INTO scans (status, finished) VALUES ('done', '2026-09-01 00:00:00')")
        scan = x(conn, "INSERT INTO scans (status, finished) VALUES ('done', '2026-09-10 10:00:00')")
        x(conn, "INSERT INTO scans (status) VALUES ('running')")  # in-flight, must be ignored
        web = x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, protected, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?)",
            ("web-owner", "Web Owner", "running", "compose", 0, old_scan, scan),
        )
        second = x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, protected, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?)",
            ("second-app", "Second App", "stopped", "compose", 1, old_scan, scan),
        )
        x(
            conn,
            "INSERT INTO applications (slug, name, status, kind, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("gone-app", "Gone App", "removed", "systemd", old_scan, old_scan),
        )

        def res(rtype, key, display, state, data, path=None, last_seen=scan):
            return x(
                conn,
                "INSERT INTO resources (type, key, display, path, state, data_json, last_seen) "
                "VALUES (?,?,?,?,?,?,?)",
                (rtype, key, display, path, state, json.dumps(data), last_seen),
            )

        cont = res(
            "container", "abc123def456", "web-owner-app-1", "running",
            {
                "image": "nginx:1.25", "state": "running", "published_ports": [8080],
                "compose_project": "web-owner", "env_var_names": ["DB_HOST"],
                "api_token": "SUPER-SECRET-VALUE", "env": ["PASSWORD=hunter2"],
                "labels": {"com.docker.compose.project": "web-owner"},
            },
        )
        img = res(
            "image", "sha256:1111", "nginx:1.25", "in-use",
            {"repo_tag": "nginx:1.25", "size": 190 * 1024 * 1024, "dangling": False,
             "containers_using": ["web-owner-app-1", "second-app-web-1"]},
        )
        net = res("network", "net001", "web-owner_default", "active",
                  {"driver": "bridge", "containers": ["web-owner-app-1"]})
        vol = res("volume", "web-owner_data", "web-owner_data", "attached",
                  {"driver": "local", "containers_using": ["web-owner-app-1"], "size_bytes": 4096},
                  path="/var/lib/docker/volumes/web-owner_data/_data")
        site = res("nginx_site", "web.example.com", "web.example.com", "enabled",
                   {"enabled": True, "server_names": ["web.example.com"], "upstreams": ["127.0.0.1:8080"]},
                   path="/etc/nginx/sites-enabled/web")
        orphan_img = res("image", "sha256:dead", "sha256:dead", "dangling",
                         {"repo_tag": "<none>:<none>", "dangling": True, "containers_using": [], "size": 12345})
        orphan_vol = res("volume", "leftover_data", "leftover_data", "orphan",
                         {"driver": "local", "containers_using": []})
        # Stale resource from the old scan: must never appear.
        res("container", "stale000", "stale-container", "exited", {}, last_seen=old_scan)

        def assoc(app_id, rid, conf, ownership, shared, risk, eligible, evidence, excluded=0):
            return x(
                conn,
                "INSERT INTO associations (app_id, resource_id, confidence, ownership, shared, "
                "data_loss_risk, removal_eligible, recommended_action, evidence_json, source, excluded) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (app_id, rid, conf, ownership, shared, risk, eligible, "remove",
                 json.dumps([{"source": "docker", "statement": s, "weight": 90} for s in evidence]),
                 "docker", excluded),
            )

        assoc(web, cont, 95, "exclusive", 0, "none", "yes", ["compose project label = web-owner"])
        assoc(web, img, 90, "shared", 1, "none", "uncertain", ["image used by this app's container(s)"])
        assoc(second, img, 85, "shared", 1, "none", "uncertain", ["image used by this app's container(s)"])
        assoc(web, net, 90, "exclusive", 0, "none", "yes", ["network attached to this app's container(s)"])
        assoc(web, vol, 92, "exclusive", 0, "data", "uncertain",
              ["volume attached", "named in compose", "mountpoint under /var/lib/docker", "fourth statement"])
        assoc(web, site, 88, "exclusive", 0, "config", "yes", ["proxy_pass to 127.0.0.1:8080"])
        # excluded association: must not count as ownership anywhere
        assoc(second, net, 70, "possible", 0, "none", "uncertain", ["name similarity"], excluded=1)
        conn.commit()
    finally:
        conn.close()
    return {
        "scan": scan, "web": web, "second": second, "cont": cont, "img": img,
        "net": net, "vol": vol, "orphan_img": orphan_img, "orphan_vol": orphan_vol,
    }


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, lines: list[bytes] | None = None, body: bytes = b""):
        self._lines = lines or []
        self._body = body
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._body

    def close(self):
        self.closed = True


def _ndjson(*objs) -> list[bytes]:
    return [json.dumps(o).encode() + b"\n" for o in objs]


def _http_error(code: int, body: str = "", headers: dict | None = None) -> urllib.error.HTTPError:
    hdrs = headers or {}
    return urllib.error.HTTPError("https://ollama.com/api/chat", code, "err", hdrs, io.BytesIO(body.encode()))


def _client() -> provider.OllamaCloudClient:
    return provider.OllamaCloudClient("https://ollama.com/", "k-test", "glm-5.3-flash", 5)


def test_chat_stream_parses_ndjson_and_yields_thinking_separately(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _FakeResponse(_ndjson(
            {"message": {"role": "assistant", "content": "", "thinking": "let me think"}, "done": False},
            {"message": {"role": "assistant", "content": "Hello "}, "done": False},
            {"message": {"role": "assistant", "content": "world"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True,
             "prompt_eval_count": 12, "eval_count": 3, "total_duration": 2_500_000_000},
        ) + [b"", b"\n"])

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    chunks = list(_client().chat_stream(
        [{"role": "user", "content": "hi"}], think="low", temperature=0.2
    ))
    assert [c["content"] for c in chunks] == ["", "Hello ", "world", ""]
    assert chunks[0]["thinking"] == "let me think"
    assert chunks[-1]["done"] is True
    assert chunks[-1]["usage"] == {"prompt_eval_count": 12, "eval_count": 3, "ms": 2500}
    assert all(c["usage"] is None for c in chunks[:-1])
    assert captured["url"] == "https://ollama.com/api/chat"
    assert captured["headers"]["Authorization"] == "Bearer k-test"
    assert captured["timeout"] == 5
    body = captured["body"]
    assert body["model"] == "glm-5.3-flash"
    assert body["stream"] is True
    assert body["think"] == "low"
    assert body["options"] == {"temperature": 0.2}
    assert body["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize("value,expected", [
    (False, "low"), ("false", "low"), (None, "low"), ("", "low"),
    ("low", "low"), ("high", "high"), (True, True), ("true", True),
])
def test_think_is_never_false(monkeypatch, value, expected):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResponse(_ndjson({"message": {"content": "x"}, "done": True}))

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    list(_client().chat_stream([], think=value, temperature=0))
    assert captured["body"]["think"] == expected
    assert captured["body"]["think"] is not False


@pytest.mark.parametrize("code,kind,status", [
    (401, "auth", 503),
    (404, "model", 503),
    (429, "busy", 429),
    (500, "upstream", 502),
])
def test_http_errors_map_to_assistant_error(monkeypatch, code, kind, status):
    def fake_urlopen(req, timeout=None):
        raise _http_error(code, '{"error": "model \\"x\\" not found"}', {"Retry-After": "7"})

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(AssistantError) as ei:
        list(_client().chat_stream([], think="low", temperature=0))
    assert ei.value.kind == kind
    assert ei.value.status == status
    if code == 429:
        assert ei.value.retry_after == 7
    if code == 404:
        assert "not found" in ei.value.message
    assert "k-test" not in ei.value.message


def test_timeout_and_urlerror_map_to_upstream(monkeypatch):
    def timeout_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(provider.urllib.request, "urlopen", timeout_urlopen)
    with pytest.raises(AssistantError) as ei:
        list(_client().chat_stream([], think="low", temperature=0))
    assert (ei.value.kind, ei.value.status) == ("upstream", 502)

    def url_error(req, timeout=None):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(provider.urllib.request, "urlopen", url_error)
    with pytest.raises(AssistantError) as ei:
        list(_client().chat_stream([], think="low", temperature=0))
    assert (ei.value.kind, ei.value.status) == ("upstream", 502)


def test_mid_stream_timeout_maps_to_upstream(monkeypatch):
    class Boom(_FakeResponse):
        def __iter__(self):
            yield _ndjson({"message": {"content": "part"}, "done": False})[0]
            raise TimeoutError("read timed out")

    monkeypatch.setattr(provider.urllib.request, "urlopen", lambda req, timeout=None: Boom())
    gen = _client().chat_stream([], think="low", temperature=0)
    assert next(gen)["content"] == "part"
    with pytest.raises(AssistantError) as ei:
        next(gen)
    assert ei.value.kind == "upstream"


def test_malformed_line_is_protocol_error(monkeypatch):
    resp = _FakeResponse([b'{"message": {"content": "ok"}, "done": false}\n', b"this is not json\n"])
    monkeypatch.setattr(provider.urllib.request, "urlopen", lambda req, timeout=None: resp)
    gen = _client().chat_stream([], think="low", temperature=0)
    assert next(gen)["content"] == "ok"
    with pytest.raises(AssistantError) as ei:
        next(gen)
    assert (ei.value.kind, ei.value.status) == ("protocol", 502)
    assert resp.closed


def test_in_stream_error_object_is_upstream(monkeypatch):
    resp = _FakeResponse(_ndjson({"error": "Authorization: Bearer k-test rejected"}))
    monkeypatch.setattr(provider.urllib.request, "urlopen", lambda req, timeout=None: resp)
    with pytest.raises(AssistantError) as ei:
        list(_client().chat_stream([], think="low", temperature=0))
    assert ei.value.kind == "upstream"
    assert "k-test" not in ei.value.message


def test_sanitize_error_truncates_and_strips_authorization():
    long = "Authorization: Bearer abcdef " + "x" * 1000
    out = provider.sanitize_error(long)
    assert len(out) <= 300
    assert "abcdef" not in out
    assert out.startswith("Authorization: ***")


def test_ping_non_streaming(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResponse(body=json.dumps(
            {"model": "glm-5.3-flash", "message": {"role": "assistant", "content": "OK"}, "done": True}
        ).encode())

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    out = _client().ping()
    assert out["ok"] is True
    assert out["model"] == "glm-5.3-flash"
    assert out["latency_ms"] >= 1
    assert captured["body"]["stream"] is False
    assert captured["body"]["think"] == "low"
    assert captured["body"]["messages"][0]["content"] == "Reply with OK"


def test_resolve_api_key_precedence_and_mode(tmp_path, monkeypatch):
    key_file = tmp_path / "key.txt"
    monkeypatch.delenv(provider.API_KEY_ENV, raising=False)
    assert provider.resolve_api_key(str(key_file)) == (None, None)

    key_file.write_text("  file-key\n")
    os.chmod(key_file, 0o600)
    assert provider.resolve_api_key(str(key_file)) == ("file-key", "file")

    monkeypatch.setenv(provider.API_KEY_ENV, "env-key")
    assert provider.resolve_api_key(str(key_file)) == ("env-key", "env")

    monkeypatch.delenv(provider.API_KEY_ENV)
    os.chmod(key_file, 0o644)
    with pytest.raises(AssistantError) as ei:
        provider.resolve_api_key(str(key_file))
    assert ei.value.kind == "key"
    assert "chmod 600" in ei.value.message
    assert "file-key" not in ei.value.message

    os.chmod(key_file, 0o600)
    key_file.write_text("\n")
    assert provider.resolve_api_key(str(key_file)) == (None, None)


# ---------------------------------------------------------------------------
# context builders
# ---------------------------------------------------------------------------

def _ctx(scope, target=None, budget=24000):
    conn = get_db()
    try:
        return context.build_context(conn, scope, target, budget)
    finally:
        conn.close()


def test_general_context(settings_env):
    ids = _seed(settings_env)
    b = _ctx("general")
    assert b.scope == "general" and b.target is None and not b.truncated
    assert f"scan: #{ids['scan']} finished 2026-09-10 10:00:00" in b.text
    assert "applications: 2" in b.text
    assert "apps_by_status: running=1, stopped=1" in b.text
    assert "apps_by_kind: compose=2" in b.text
    assert "resources_by_type: container=1, image=2, volume=2, network=1, nginx_site=1" in b.text
    assert "actionable_orphans: 2" in b.text
    assert "shared_or_multi_owner_resources:" in b.text
    assert "## Shared resources (multi-owner)" in b.text
    assert "## Volumes (every current volume + owners)" in b.text
    assert "## Images (every current image + owners)" in b.text
    assert "image sha256:1111" in b.text and "owners=" in b.text
    assert "disk_usage:" in b.text and "4096 bytes" in b.text
    line = next(ln for ln in b.text.splitlines() if ln.startswith("- web-owner"))
    assert "domains=web.example.com" in line
    assert "ports=8080" in line
    assert "resources=5" in line
    assert "protected=false" in line
    assert "gone-app" not in b.text
    assert "stale-container" not in b.text
    assert b.facts["app_count"] == 2 and b.facts["actionable_orphans"] == 2


def test_app_context_lists_associations_with_evidence_and_ordering(settings_env):
    _seed(settings_env)
    b = _ctx("app", "web-owner")
    assert b.target == "web-owner" and "Web Owner" in b.title
    assert "app: web-owner" in b.text
    assert "manifest: none" in b.text
    assert "domains: web.example.com" in b.text
    assert "ports: 8080" in b.text
    assert "associations: 5" in b.text
    bullets = [ln for ln in b.text.splitlines() if ln.startswith("- ")]
    assert len(bullets) == 5
    # shared / data-risk items first
    assert "image sha256:1111" in bullets[0] and "shared=true" in bullets[0]
    assert "also_owners=second-app" in bullets[0]
    assert "all_owners=" in bullets[0] and "web-owner" in bullets[0]
    assert "volume web-owner_data" in bullets[1] and "data_loss_risk=data" in bullets[1]
    assert "confidence=90 (high)" in bullets[0]
    assert "removal_eligible=uncertain" in bullets[0]
    assert "recommended_action=remove" in bullets[0]
    # up to 3 evidence statements
    assert b.text.count("evidence:") == 1 + 1 + 1 + 3 + 1
    assert "fourth statement" not in b.text
    assert b.facts["shared_count"] == 1 and b.facts["data_risk_count"] == 1


def test_app_context_removed_app_uses_its_last_scan(settings_env):
    _seed(settings_env)
    b = _ctx("app", "gone-app")
    assert "removed: true" in b.text
    assert "associations: 0" in b.text


def test_orphans_context(settings_env):
    _seed(settings_env)
    b = _ctx("orphans")
    assert "orphan_candidates: 2" in b.text
    assert "by_bucket: actionable=2, expected=0, system=0" in b.text
    assert "- image sha256:dead | bucket=actionable" in b.text
    assert "size=12.1 KB" in b.text
    assert "- volume leftover_data | bucket=actionable" in b.text
    assert "reason=" in b.text
    # owned resources are not orphans
    assert "web-owner_data" not in b.text
    assert b.facts["counts"]["actionable"] == 2


@pytest.mark.parametrize("rtype", RESOURCE_TYPE_TARGETS)
def test_resource_type_context_every_docker_type(settings_env, rtype):
    _seed(settings_env)
    b = _ctx("resource_type", rtype)
    assert b.target == rtype
    assert f"resource_type: {rtype}" in b.text
    bullets = [ln for ln in b.text.splitlines() if ln.startswith("- ")]
    assert bullets, b.text
    for ln in bullets:
        assert "owners=" in ln and "shared=" in ln and "orphan=" in ln
    if rtype == "image":
        assert "count: 2" in b.text
        assert "owners=web-owner, second-app" in bullets[0]
        assert "shared=true" in bullets[0]
        assert "size=190.0 MB" in bullets[0]
        assert "orphan=no" in bullets[0]
        assert "image sha256:dead" in bullets[1] and "dangling=true" in bullets[1]
        assert "orphan=actionable" in bullets[1]
    if rtype == "volume":
        # neither is shared, so plain display order: leftover_data first
        assert "volume leftover_data" in bullets[0] and "owners=-" in bullets[0]
        assert "orphan=actionable" in bullets[0]
        assert "volume web-owner_data" in bullets[1] and "driver=local" in bullets[1]
        assert "size=4.0 KB" in bullets[1] and "orphan=no" in bullets[1]
    if rtype == "network":
        # the excluded association must not make second-app an owner
        assert "owners=web-owner |" in bullets[0]
        assert "driver=bridge" in bullets[0]
    if rtype == "container":
        assert "image=nginx:1.25" in bullets[0]
        assert "stale-container" not in b.text


def test_resource_context_full_data_owners_and_image_extras(settings_env):
    _seed(settings_env)
    b = _ctx("resource", "image:sha256:1111")
    assert b.target == "image:sha256:1111"
    assert "resource: image sha256:1111" in b.text
    assert "owners: web-owner, second-app" in b.text
    assert "owner_count: 2" in b.text
    assert "shared: true" in b.text
    assert "orphan_candidate: no" in b.text
    assert "containers_using: web-owner-app-1, second-app-web-1" in b.text
    assert "compose_projects_declaring: -" in b.text
    assert "## Data" in b.text and "repo_tag: nginx:1.25" in b.text
    owner_lines = [ln for ln in b.text.splitlines() if ln.startswith("- app ")]
    assert len(owner_lines) == 2
    assert "- app web-owner | confidence=90 (high) | ownership=shared | shared=true" in b.text
    assert "evidence: image used by this app's container(s)" in b.text
    assert b.facts["owners"] == ["web-owner", "second-app"] and b.facts["shared"] is True


def test_resource_context_orphan_and_secret_stripping(settings_env):
    _seed(settings_env)
    b = _ctx("resource", "volume:leftover_data")
    assert "orphan_candidate: actionable" in b.text
    assert "owners: - (none)" in b.text
    assert "- none" in b.text

    b = _ctx("resource", "container:abc123def456")
    assert "SUPER-SECRET-VALUE" not in b.text
    assert "api_token" not in b.text
    assert "hunter2" not in b.text
    assert "labels" not in b.text
    assert "env_var_names: DB_HOST" in b.text
    assert "published_ports: 8080" in b.text


def test_unknown_scope_and_targets(settings_env):
    _seed(settings_env)
    with pytest.raises(AssistantError) as ei:
        _ctx("bogus")
    assert (ei.value.kind, ei.value.status) == ("scope", 400)
    for scope, target in [
        ("app", "nope"), ("app", None), ("resource_type", "nginx_site"),
        ("resource", "image:sha256:missing"), ("resource", "noseparator"),
        ("resource", "port:80"), ("resource", "container:stale000"),
    ]:
        with pytest.raises(AssistantError) as ei:
            _ctx(scope, target)
        assert (ei.value.kind, ei.value.status) == ("target", 404), (scope, target)


def test_budget_truncation_on_line_boundary(settings_env):
    _seed(settings_env)
    full = _ctx("general")
    assert not full.truncated
    budget = 400
    cut = _ctx("general", budget=budget)
    assert cut.truncated
    assert len(cut.text) <= budget
    last = cut.text.splitlines()[-1]
    assert last.startswith("[context truncated: ") and last.endswith(" more lines omitted]")
    kept = cut.text.splitlines()[:-1]
    assert kept == full.text.splitlines()[: len(kept)]
    omitted = int(last.split(":")[1].split()[0])
    assert omitted == len(full.text.splitlines()) - len(kept)


@pytest.mark.parametrize("scope,target", [
    ("general", None), ("app", "web-owner"), ("orphans", None),
    ("resource_type", "image"), ("resource", "image:sha256:1111"),
])
def test_every_scope_respects_small_budget(settings_env, scope, target):
    _seed(settings_env)
    b = _ctx(scope, target, budget=150)
    assert b.truncated and len(b.text) <= 150
    assert "[context truncated:" in b.text


def test_render_budgeted_direct():
    lines = ["a" * 50 for _ in range(10)]
    text, truncated = render = context.render_budgeted(lines, 10_000)
    assert not truncated and text == "\n".join(lines)
    text, truncated = context.render_budgeted(lines, 200)
    assert truncated and len(text) <= 200
    assert text.endswith("more lines omitted]")
    del render


def test_context_without_any_scan(settings_env):
    b = _ctx("general")
    assert "scan: none completed yet" in b.text
    assert "applications: 0" in b.text
    assert _ctx("orphans").facts["total"] == 0
    assert _ctx("resource_type", "volume").facts["count"] == 0


def test_list_targets(settings_env):
    _seed(settings_env)
    conn = get_db()
    try:
        apps = list_targets(conn, "app")
        assert [a["value"] for a in apps] == ["second-app", "web-owner"]
        assert apps[1]["label"] == "Web Owner (web-owner)"
        types = list_targets(conn, "resource_type")
        assert [t["value"] for t in types] == list(RESOURCE_TYPE_TARGETS)
        assert types[1]["label"] == "Images"
        imgs = list_targets(conn, "resource", "image")
        assert {i["value"] for i in imgs} == {"image:sha256:1111", "image:sha256:dead"}
        assert list_targets(conn, "resource") == []
        assert list_targets(conn, "resource", "nginx_site") == []
        assert list_targets(conn, "general") == []
        assert list_targets(conn, "orphans") == []
        with pytest.raises(AssistantError):
            list_targets(conn, "bogus")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

def test_prompt_library_integrity():
    ids = [p["id"] for p in PROMPT_LIBRARY]
    assert len(ids) == len(set(ids))
    for p in PROMPT_LIBRARY:
        assert set(p) == {"id", "scope", "label", "text", "description"}
        assert p["scope"] in SCOPES
        assert p["id"].split(".")[0] in ("general", "app", "orphans", "rtype", "res")
    for scope in SCOPES:
        assert len(prompt_library(scope)) >= 3, scope
    expected = {
        "general.overview", "general.risky", "general.stale", "general.shared",
        "general.owners", "general.protected",
        "app.explain", "app.remove_impact", "app.data", "app.shared", "app.confidence", "app.trace",
        "orphans.review", "orphans.safe", "orphans.suspicious", "orphans.reclaim",
        "rtype.review", "rtype.unused", "rtype.multi_owner",
        "res.safe", "res.owners", "res.contents",
    }
    assert set(ids) == expected
    assert prompt_library("bogus") == []


def test_prompt_type_substitution():
    lib = prompt_library("resource_type", "volume")
    labels = {p["id"]: p["label"] for p in lib}
    assert labels["rtype.review"] == "Review all volumes and flag anything shared"
    assert all("<type>" not in p["label"] and "<type>" not in p["text"] for p in lib)
    # resource scope derives the type from the <type>:<key> target
    assert all("<type>" not in p["text"] for p in prompt_library("resource", "image:sha256:1"))
    # without a target the placeholder falls back to a neutral word
    assert "<type>" not in prompt_library("resource_type")[0]["label"]
    # the library itself is not mutated
    assert any("<type>" in p["label"] for p in PROMPT_LIBRARY)


def test_system_prompt_hard_rules():
    for needle in ("shared", "data_loss_risk", "/orphans", "/apps/<slug>", "removal_eligible", "backup"):
        assert needle in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def test_migration_003_applied(settings_env):
    conn = get_db()
    try:
        names = {r["name"] for r in q(conn, "SELECT name FROM schema_migrations")}
        assert "003_assistant.sql" in names
        tables = {r["name"] for r in q(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"assistant_conversations", "assistant_messages"} <= tables
    finally:
        conn.close()


def test_store_crud_and_owner_scoping(settings_env):
    conn = get_db()
    try:
        cid = store.create_conversation(conn, 1, "app", "web-owner", "x" * 200)
        conv = store.get_conversation(conn, cid, 1)
        assert conv is not None and len(conv["title"]) == 80
        assert conv["scope"] == "app" and conv["target"] == "web-owner"
        assert conv["messages"] == []

        m1 = store.add_message(conn, cid, "user", "hello", prompt_id="app.explain")
        m2 = store.add_message(
            conn, cid, "assistant", "hi", usage={"eval_count": 3, "ms": 10}, context_truncated=True
        )
        assert m2 > m1
        conv = store.get_conversation(conn, cid, 1)
        assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
        assert conv["messages"][0]["prompt_id"] == "app.explain"
        assert conv["messages"][1]["usage"] == {"eval_count": 3, "ms": 10}
        assert conv["messages"][1]["context_truncated"] is True
        assert conv["messages"][0]["context_truncated"] is False

        # other user: invisible everywhere
        assert store.get_conversation(conn, cid, 2) is None
        assert store.list_conversations(conn, 2) == []
        assert store.delete_conversation(conn, cid, 2) is False

        other = store.create_conversation(conn, 2, "general", None, "theirs")
        mine = store.list_conversations(conn, 1)
        assert [c["id"] for c in mine] == [cid]
        assert mine[0]["message_count"] == 2
        assert [c["id"] for c in store.list_conversations(conn, 2)] == [other]

        for i in range(5):
            store.add_message(conn, cid, "user", f"turn {i}")
        recent = store.recent_messages(conn, cid, 3)
        assert [m["content"] for m in recent] == ["turn 2", "turn 3", "turn 4"]
        assert store.recent_messages(conn, cid, 0) == []

        assert store.delete_conversation(conn, cid, 1) is True
        assert store.get_conversation(conn, cid, 1) is None
        assert q(conn, "SELECT COUNT(*) AS n FROM assistant_messages WHERE conversation_id = ?", (cid,))[0]["n"] == 0
        assert store.get_conversation(conn, other, 2) is not None
    finally:
        conn.close()


def test_store_limit_and_ordering(settings_env):
    conn = get_db()
    try:
        ids = [store.create_conversation(conn, 1, "general", None, f"c{i}") for i in range(4)]
        # touching the oldest bumps it to the front
        store.add_message(conn, ids[0], "user", "bump")
        conn.execute(
            "UPDATE assistant_conversations SET updated_at = datetime('now', '+1 minute') WHERE id = ?",
            (ids[0],),
        )
        conn.commit()
        got = [c["id"] for c in store.list_conversations(conn, 1, limit=2)]
        assert got[0] == ids[0] and len(got) == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

class FakeClient:
    """Stands in for provider.OllamaCloudClient; records the request and
    replays scripted chunks (or raises)."""

    calls: list = []
    script: list = []
    raise_after: int | None = None
    error: AssistantError | None = None

    def __init__(self, base_url, api_key, model, timeout_seconds):
        self.api_key = api_key
        self.model = model

    def chat_stream(self, messages, *, think, temperature):
        FakeClient.calls.append({"messages": messages, "think": think, "temperature": temperature,
                                 "api_key": self.api_key, "model": self.model})
        for i, c in enumerate(FakeClient.script):
            if FakeClient.raise_after is not None and i == FakeClient.raise_after:
                raise FakeClient.error
            yield c

    def ping(self):
        return {"ok": True, "model": self.model, "latency_ms": 3}


@pytest.fixture()
def fake_provider(monkeypatch):
    FakeClient.calls = []
    FakeClient.script = [
        {"content": "", "thinking": "hmm", "done": False, "usage": None},
        {"content": "Two ", "thinking": "", "done": False, "usage": None},
        {"content": "owners.", "thinking": "", "done": False, "usage": None},
        {"content": "", "thinking": "", "done": True,
         "usage": {"prompt_eval_count": 40, "eval_count": 4, "ms": 120}},
    ]
    FakeClient.raise_after = None
    FakeClient.error = None
    monkeypatch.setattr(provider, "OllamaCloudClient", FakeClient)
    monkeypatch.setenv(provider.API_KEY_ENV, "env-key-123")
    return FakeClient


def _audit_rows():
    conn = get_db()
    try:
        return _rows(q(conn, "SELECT * FROM audit_log WHERE action = 'assistant.ask' ORDER BY id"))
    finally:
        conn.close()


def test_status_unconfigured_and_configured(settings_env, monkeypatch, tmp_path):
    st = service.status()
    assert st == {
        "enabled": True, "configured": False, "model": "glm-5.3-flash",
        "key_source": None, "reason": st["reason"],
    }
    assert "DEL_OLLAMA_API_KEY" in st["reason"]
    assert "ollama-api-key.txt" in st["reason"]

    key_file = tmp_path / "ollama-api-key.txt"
    key_file.write_text("sekrit-file-key\n")
    os.chmod(key_file, 0o640)
    st = service.status()
    assert st["configured"] is False and "chmod 600" in st["reason"]
    assert "sekrit" not in json.dumps(st)

    os.chmod(key_file, 0o600)
    st = service.status()
    assert st["configured"] is True and st["key_source"] == "file" and st["reason"] is None
    assert "sekrit" not in json.dumps(st)

    monkeypatch.setenv(provider.API_KEY_ENV, "env-key")
    assert service.status()["key_source"] == "env"


def test_status_disabled(settings_env, monkeypatch, tmp_path):
    cfg = get_settings()
    monkeypatch.setattr(cfg.assistant, "enabled", False)
    monkeypatch.setenv(provider.API_KEY_ENV, "env-key")
    st = service.status()
    assert st["enabled"] is False and st["configured"] is False
    assert "disabled" in st["reason"]


def test_ask_disabled_returns_503(settings_env):
    gen = service.ask(user_id=1, scope="general", target=None, message="hi")
    with pytest.raises(AssistantError) as ei:
        next(gen)
    assert (ei.value.kind, ei.value.status) == ("disabled", 503)


def test_ask_validation_errors(settings_env, fake_provider):
    _seed(settings_env)
    cases = [
        (dict(scope="bogus", target=None, message="hi"), "scope", 400),
        (dict(scope="app", target="missing", message="hi"), "target", 404),
        (dict(scope="general", target=None, message="   "), "message", 400),
        (dict(scope="general", target=None, message="hi", conversation_id=999), "target", 404),
    ]
    for kwargs, kind, status in cases:
        with pytest.raises(AssistantError) as ei:
            next(service.ask(user_id=1, **kwargs))
        assert (ei.value.kind, ei.value.status) == (kind, status), kwargs
    assert fake_provider.calls == []
    assert _audit_rows() == []


def test_ask_end_to_end(settings_env, fake_provider):
    _seed(settings_env)
    events = list(service.ask(
        user_id=1, scope="resource", target="image:sha256:1111",
        message="Is it safe to delete this? Is it tied to multiple apps?", prompt_id="res.safe",
    ))
    assert [e["type"] for e in events] == ["meta", "delta", "delta", "done"]
    meta = events[0]
    assert meta["scope"] == "resource" and meta["target"] == "image:sha256:1111"
    assert meta["context_truncated"] is False
    assert isinstance(meta["conversation_id"], int) and isinstance(meta["message_id"], int)
    assert [e["text"] for e in events[1:3]] == ["Two ", "owners."]
    assert events[3]["usage"] == {"prompt_eval_count": 40, "eval_count": 4, "ms": 120}
    # thinking text never reaches the browser
    assert "hmm" not in json.dumps(events)

    # what the model was sent
    call = fake_provider.calls[0]
    assert call["api_key"] == "env-key-123" and call["model"] == "glm-5.3-flash"
    assert call["think"] == "low" and call["temperature"] == 0.2
    msgs = call["messages"]
    assert msgs[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert msgs[1]["role"] == "system"
    assert msgs[1]["content"].startswith("### Inventory context (scope=resource, target=image:sha256:1111)\n")
    assert "owners: web-owner, second-app" in msgs[1]["content"]
    assert msgs[-1] == {"role": "user", "content": "Is it safe to delete this? Is it tied to multiple apps?"}
    assert len(msgs) == 3

    # persistence
    conn = get_db()
    try:
        conv = store.get_conversation(conn, meta["conversation_id"], 1)
    finally:
        conn.close()
    assert conv["scope"] == "resource" and conv["target"] == "image:sha256:1111"
    assert conv["title"] == "Is it safe to delete this? Is it tied to multiple apps?"
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["messages"][0]["id"] == meta["message_id"]
    assert conv["messages"][0]["prompt_id"] == "res.safe"
    assert conv["messages"][1]["content"] == "Two owners."
    assert conv["messages"][1]["usage"] == {"prompt_eval_count": 40, "eval_count": 4, "ms": 120}
    # the context block is not stored
    assert not any("Inventory context" in m["content"] for m in conv["messages"])

    # audit row without message text
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["subject"] == "resource:image:sha256:1111"
    details = json.loads(rows[0]["details_json"])
    assert details == {
        "conversation_id": meta["conversation_id"], "prompt_id": "res.safe",
        "chars_in": len("Is it safe to delete this? Is it tied to multiple apps?"),
        "chars_out": len("Two owners."), "context_truncated": False,
    }
    assert "safe to delete" not in rows[0]["details_json"]
    assert "owners." not in rows[0]["details_json"]
    audit_log = os.path.join(settings_env.logs_dir, "audit.log")
    assert "env-key-123" not in open(audit_log).read()

    # the semaphore was released
    assert service._INFLIGHT.acquire(blocking=False)
    service._INFLIGHT.release()


def test_ask_follow_up_replays_history(settings_env, fake_provider):
    _seed(settings_env)
    first = list(service.ask(user_id=1, scope="app", target="web-owner", message="first question"))
    cid = first[0]["conversation_id"]
    second = list(service.ask(
        user_id=1, scope="app", target="web-owner", message="and then?", conversation_id=cid,
    ))
    assert second[0]["conversation_id"] == cid
    msgs = fake_provider.calls[1]["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "system", "user", "assistant", "user"]
    assert msgs[2]["content"] == "first question"
    assert msgs[3]["content"] == "Two owners."
    assert msgs[4]["content"] == "and then?"
    conn = get_db()
    try:
        conv = store.get_conversation(conn, cid, 1)
        assert len(conv["messages"]) == 4
        # another user cannot continue this conversation
        with pytest.raises(AssistantError) as ei:
            next(service.ask(user_id=2, scope="app", target="web-owner", message="x", conversation_id=cid))
        assert ei.value.status == 404
    finally:
        conn.close()


def test_ask_history_window(settings_env, fake_provider, monkeypatch):
    _seed(settings_env)
    monkeypatch.setattr(get_settings().assistant, "history_messages", 2)
    cid = list(service.ask(user_id=1, scope="general", target="ignored", message="q1"))[0]["conversation_id"]
    list(service.ask(user_id=1, scope="general", target=None, message="q2", conversation_id=cid))
    list(service.ask(user_id=1, scope="general", target=None, message="q3", conversation_id=cid))
    msgs = fake_provider.calls[2]["messages"]
    assert [m["content"] for m in msgs[2:]] == ["q2", "Two owners.", "q3"]
    assert fake_provider.calls[0]["messages"][1]["content"].startswith(
        "### Inventory context (scope=general, target=-)"
    )


def test_ask_provider_error_mid_stream(settings_env, fake_provider):
    _seed(settings_env)
    fake_provider.raise_after = 2
    fake_provider.error = AssistantError("upstream", "stream interrupted: Authorization: ***", 502)
    events = list(service.ask(user_id=1, scope="orphans", target=None, message="review"))
    assert [e["type"] for e in events] == ["meta", "delta", "error"]
    assert events[-1] == {"type": "error", "kind": "upstream", "message": "stream interrupted: Authorization: ***"}
    conn = get_db()
    try:
        conv = store.get_conversation(conn, events[0]["conversation_id"], 1)
    finally:
        conn.close()
    partial = conv["messages"][1]
    assert partial["role"] == "assistant" and partial["content"] == "Two "
    assert partial["usage"] == {"error": "upstream"}
    rows = _audit_rows()
    assert len(rows) == 1 and rows[0]["subject"] == "orphans:-"
    assert json.loads(rows[0]["details_json"])["chars_out"] == 4
    assert service._INFLIGHT.acquire(blocking=False)
    service._INFLIGHT.release()


def test_ask_busy_returns_429_and_releases_on_close(settings_env, fake_provider):
    _seed(settings_env)
    gen = service.ask(user_id=1, scope="general", target=None, message="one")
    assert next(gen)["type"] == "meta"  # holds the semaphore while streaming
    with pytest.raises(AssistantError) as ei:
        next(service.ask(user_id=1, scope="general", target=None, message="two"))
    assert (ei.value.kind, ei.value.status) == ("busy", 429)
    # the rejected ask must not have persisted anything
    conn = get_db()
    try:
        assert q(conn, "SELECT COUNT(*) AS n FROM assistant_conversations")[0]["n"] == 1
    finally:
        conn.close()
    gen.close()  # client aborted: generator finalisers release the slot
    events = list(service.ask(user_id=1, scope="general", target=None, message="three"))
    assert events[-1]["type"] == "done"


def test_ask_truncated_flag_propagates(settings_env, fake_provider, monkeypatch):
    _seed(settings_env)
    monkeypatch.setattr(get_settings().assistant, "context_budget_chars", 300)
    events = list(service.ask(user_id=1, scope="general", target=None, message="summarise"))
    assert events[0]["context_truncated"] is True
    ctx = fake_provider.calls[0]["messages"][1]["content"]
    assert "[context truncated:" in ctx
    assert len(ctx.split("\n", 1)[1]) <= 300
    conn = get_db()
    try:
        conv = store.get_conversation(conn, events[0]["conversation_id"], 1)
    finally:
        conn.close()
    assert conv["messages"][1]["context_truncated"] is True
    assert json.loads(_audit_rows()[0]["details_json"])["context_truncated"] is True


def test_test_connection(settings_env, fake_provider, monkeypatch):
    out = service.test_connection()
    assert out == {"ok": True, "model": "glm-5.3-flash", "latency_ms": 3}
    monkeypatch.delenv(provider.API_KEY_ENV)
    with pytest.raises(AssistantError) as ei:
        service.test_connection()
    assert ei.value.status == 503


def test_assistant_package_does_not_import_action_modules():
    """Layering rule: nothing in assistant/ imports helper_client, planner or
    jobs (checked on the real import graph, not on source text)."""
    import ast
    import importlib

    forbidden = {"del_app.helper_client", "del_app.planner", "del_app.jobs"}
    for name in ("context", "service", "provider", "store", "prompts", "errors"):
        mod = importlib.import_module(f"del_app.assistant.{name}")
        tree = ast.parse(open(mod.__file__).read())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(f"{node.module}.{a.name}" for a in node.names)
        assert not (imported & forbidden), (name, imported & forbidden)
