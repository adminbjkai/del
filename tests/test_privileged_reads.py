"""Discovery fallbacks to the root helper for root-only reads.

del-web runs with NoNewPrivileges, so sudo is unusable; root-only nginx site
files and other users' crontabs must come through the helper's read-only ops.
helper_client.call is monkeypatched: no socket, no root.
"""
import logging

from del_app import helper_client
from del_app.discovery import cron_src, nginx_src


def test_nginx_read_file_direct_read_does_not_call_helper(tmp_path, monkeypatch):
    conf = tmp_path / "ok.conf"
    conf.write_text("server { server_name direct.example; }")
    monkeypatch.setattr(helper_client, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert "direct.example" in nginx_src._read_file(str(conf))


def test_nginx_read_file_permission_error_uses_helper(tmp_path, monkeypatch):
    conf = tmp_path / "root-only.conf"
    conf.write_text("unused")
    calls = []

    def fake_open(*a, **k):
        raise PermissionError(13, "Permission denied")

    def fake_call(op, args, dry_run=True, timeout=300, **kw):
        calls.append((op, args, dry_run))
        return {"ok": True, "output": "server { server_name bjk.ai; listen 443 ssl; }",
                "error": None, "changed": []}

    monkeypatch.setattr(nginx_src, "open", fake_open, raising=False)
    monkeypatch.setattr(helper_client, "call", fake_call)
    res = nginx_src._resource_from_file(str(conf), enabled=True)
    assert calls == [("read_nginx_config", {"path": str(conf)}, False)]
    assert res is not None and res.data["server_names"] == ["bjk.ai"]


def test_nginx_read_file_helper_down_warns_without_traceback(tmp_path, monkeypatch, caplog):
    conf = tmp_path / "root-only.conf"
    conf.write_text("unused")

    def boom(*a, **k):
        raise helper_client.HelperError("helper transport failure: no socket")

    monkeypatch.setattr(nginx_src, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")), raising=False)
    monkeypatch.setattr(helper_client, "call", boom)
    with caplog.at_level(logging.WARNING, logger="del_app.discovery.nginx_src"):
        assert nginx_src._read_file(str(conf)) is None
    recs = [r for r in caplog.records if r.name == "del_app.discovery.nginx_src"]
    assert len(recs) == 1 and recs[0].levelno == logging.WARNING and recs[0].exc_info is None


def test_nginx_read_file_helper_refusal_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(nginx_src, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")), raising=False)
    monkeypatch.setattr(helper_client, "call",
                        lambda *a, **k: {"ok": False, "error": "outside nginx dirs", "output": ""})
    result = nginx_src._read_file(str(tmp_path / "x.conf"))
    assert result is None


def test_user_crontabs_come_from_helper(monkeypatch):
    calls = []

    def fake_call(op, args, dry_run=True, timeout=300, **kw):
        calls.append((op, args, dry_run))
        if args["user"] == "bjkai":
            return {"ok": True, "error": None, "changed": [],
                    "output": "# comment\nPATH=/usr/bin\n*/5 * * * * /apps/foo/run.sh --x\n"}
        return {"ok": True, "output": "", "error": None, "changed": []}

    monkeypatch.setattr(cron_src, "_candidate_users", lambda: ["root", "bjkai"])
    monkeypatch.setattr(helper_client, "call", fake_call)
    resources = []
    cron_src._parse_user_crontabs(resources)
    assert [c[0] for c in calls] == ["read_crontab", "read_crontab"]
    assert all(c[2] is False for c in calls)
    assert len(resources) == 1
    r = resources[0]
    assert r.data["user"] == "bjkai" and r.data["schedule"] == "*/5 * * * *"
    assert r.data["referenced_paths"] == ["/apps/foo/run.sh"]


def test_user_crontabs_helper_down_warns_and_continues(monkeypatch, caplog):
    def boom(*a, **k):
        raise helper_client.HelperError("no socket")

    monkeypatch.setattr(cron_src, "_candidate_users", lambda: ["root", "bjkai"])
    monkeypatch.setattr(helper_client, "call", boom)
    resources = []
    with caplog.at_level(logging.WARNING, logger="del_app.discovery.cron_src"):
        cron_src._parse_user_crontabs(resources)
    assert resources == []
    recs = [r for r in caplog.records if r.name == "del_app.discovery.cron_src"]
    assert len(recs) == 2 and all(r.exc_info is None for r in recs)
