import hashlib
import os

import pytest
from fastapi import Response

from del_app import auth
from del_app.config import get_settings
from del_app.db import get_db, q, run_migrations
from del_app.helper_client import HelperError, call as helper_call


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


class _FakeRequest:
    """Minimal stand-in exposing the .cookies attribute check_csrf needs."""

    def __init__(self, cookies: dict):
        self.cookies = cookies


def test_migrations_apply_on_tmp_db(settings_env):
    conn = get_db()
    try:
        tables = {row["name"] for row in q(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    expected = {
        "users", "sessions", "scans", "applications", "resources",
        "associations", "plans", "jobs", "job_steps", "backups",
        "audit_log", "settings", "schema_migrations",
    }
    assert expected.issubset(tables)


def test_user_create_and_verify_roundtrip(settings_env):
    user_id = auth.create_user("alice", "correct-horse-battery-staple")
    assert isinstance(user_id, int) and user_id > 0
    verified_id = auth.verify("alice", "correct-horse-battery-staple")
    assert verified_id == user_id


def test_wrong_password_fails(settings_env):
    auth.create_user("bob", "hunter2-but-longer")
    assert auth.verify("bob", "wrong-password") is None
    assert auth.verify("nonexistent-user", "whatever") is None


def test_session_cookie_sign_and_verify(settings_env):
    user_id = auth.create_user("carol", "another-strong-password")
    resp = Response()
    token = auth.login_session(resp, user_id, ip="127.0.0.1")

    # Cookie header carries a signed value, not the raw token.
    set_cookie_header = resp.headers.get("set-cookie")
    assert set_cookie_header is not None
    assert token not in set_cookie_header or auth.sign_token(token) in set_cookie_header

    signed = auth.sign_token(token)
    assert auth.unsign_token(signed) == token

    # Tampering with the signed value must be rejected.
    tampered = signed[:-1] + ("a" if signed[-1] != "a" else "b")
    assert auth.unsign_token(tampered) is None

    # require_user resolves a valid signed cookie back to the user.
    request = _FakeRequest({auth.SESSION_COOKIE_NAME: signed})
    user = auth.require_user(request)
    assert user.id == user_id
    assert user.username == "carol"


def test_require_user_raises_needs_login_without_cookie(settings_env):
    request = _FakeRequest({})
    with pytest.raises(auth.NeedsLogin):
        auth.require_user(request)


def test_csrf_token_check(settings_env):
    user_id = auth.create_user("dave", "yet-another-password")
    resp = Response()
    token = auth.login_session(resp, user_id)
    signed = auth.sign_token(token)
    request = _FakeRequest({auth.SESSION_COOKIE_NAME: signed})

    good_token = auth.csrf_token(token)
    assert auth.check_csrf(request, good_token) is True
    assert auth.check_csrf(request, "bogus-token") is False

    no_cookie_request = _FakeRequest({})
    assert auth.check_csrf(no_cookie_request, good_token) is False


def test_helper_client_raises_helper_error_when_socket_absent(settings_env):
    with pytest.raises(HelperError):
        helper_call("ping", {})
