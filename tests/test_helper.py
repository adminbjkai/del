"""Tests for the DEL privileged helper daemon and its validation logic.

No root required. Validation is exercised as pure functions against a policy
rooted in a tmpdir; the socket server is run on a tmp socket in a background
thread and driven end-to-end for ping + a dry-run op.
"""

import json
import os
import socket
import sys
import threading
import time

import pytest

# make helper/ importable regardless of pytest invocation cwd
HELPER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "helper")
sys.path.insert(0, HELPER_DIR)

import validation as V  # noqa: E402
import del_helper as H  # noqa: E402

# The full protected-root list from docs/ARCHITECTURE.md
PROTECTED_ROOTS = [
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt",
    "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var",
    "/apps", "/data", "/apps/del",
]


@pytest.fixture
def tmp_policy(tmp_path):
    """A policy whose approved deletion root is a real tmp dir we can delete into."""
    approved = tmp_path / "approved"
    approved.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    never = approved / "keep"
    never.mkdir()
    return {
        "approved_deletion_roots": [str(approved)],
        "protected_roots": PROTECTED_ROOTS + [str(approved)],
        "never_delete": [str(never)],
        "backup_dir": str(backups),
        "nginx_site_roots": [str(tmp_path / "sites-available"),
                             str(tmp_path / "sites-enabled")],
        "systemd_unit_dir": str(tmp_path / "systemd"),
        "cron_d_dir": str(tmp_path / "cron.d"),
    }


# ---------------------------------------------------------------------------
# validate_path_for_deletion
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("root", PROTECTED_ROOTS)
def test_every_protected_root_rejected(root):
    policy = {
        # deliberately approve "/" so the ONLY thing stopping deletion is the
        # protected-root / mountpoint / shallow guard
        "approved_deletion_roots": ["/"],
        "protected_roots": PROTECTED_ROOTS,
        "never_delete": ["/apps/del"],
        "backup_dir": "/apps/del/backups",
    }
    if not os.path.exists(root):
        pytest.skip(f"{root} absent on this host")
    with pytest.raises(V.ValidationError):
        V.validate_path_for_deletion(root, policy)


def test_nonexistent_path_rejected(tmp_policy):
    missing = os.path.join(tmp_policy["approved_deletion_roots"][0], "nope")
    with pytest.raises(V.ValidationError):
        V.validate_path_for_deletion(missing, tmp_policy)


def test_relative_path_rejected(tmp_policy):
    with pytest.raises(V.ValidationError):
        V.validate_path_for_deletion("relative/path", tmp_policy)


def test_path_outside_approved_root_rejected(tmp_policy, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(V.ValidationError):
        V.validate_path_for_deletion(str(outside), tmp_policy)


def test_never_delete_entry_rejected(tmp_policy):
    keep = tmp_policy["never_delete"][0]
    with pytest.raises(V.ValidationError):
        V.validate_path_for_deletion(keep, tmp_policy)


def test_valid_deletion_path_accepted(tmp_policy):
    target = os.path.join(tmp_policy["approved_deletion_roots"][0], "victim")
    os.mkdir(target)
    got = V.validate_path_for_deletion(target, tmp_policy)
    assert got == os.path.realpath(target)


def test_symlink_escape_rejected(tmp_policy, tmp_path):
    """A symlink inside the approved root pointing outside must be rejected,
    because realpath resolves outside the approved root."""
    approved = tmp_policy["approved_deletion_roots"][0]
    outside = tmp_path / "secret"
    outside.mkdir()
    link = os.path.join(approved, "escape")
    os.symlink(str(outside), link)
    with pytest.raises(V.ValidationError):
        V.validate_path_for_deletion(link, tmp_policy)


# ---------------------------------------------------------------------------
# unit name validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "foo.service; rm -rf /",
    "foo service.service",
    "foo.socket",
    "../etc/passwd.service",
    "foo.service\n",
    "",
])
def test_unit_name_injection_rejected(bad):
    with pytest.raises(V.ValidationError):
        V.validate_unit_name(bad)


@pytest.mark.parametrize("good", ["nginx.service", "my-app@1.timer", "a_b.C.service"])
def test_unit_name_accepted(good):
    assert V.validate_unit_name(good) == good


def test_unit_rm_requires_file(tmp_policy):
    unit_dir = tmp_policy["systemd_unit_dir"]
    os.makedirs(unit_dir)
    # no file yet -> reject
    with pytest.raises(V.ValidationError):
        V.validate_unit_name("ghost.service", tmp_policy, require_unit_file=True)
    # create the file -> accept
    path = os.path.join(unit_dir, "real.service")
    with open(path, "w") as fh:
        fh.write("[Unit]\n")
    got = V.validate_unit_name("real.service", tmp_policy, require_unit_file=True)
    assert got == os.path.realpath(path)


# ---------------------------------------------------------------------------
# docker name validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["$(evil)", "foo bar", "-leading", "a;b", "", "a|b"])
def test_docker_name_rejects_injection(bad):
    with pytest.raises(V.ValidationError):
        V.validate_container_id(bad)
    with pytest.raises(V.ValidationError):
        V.validate_volume_name(bad)
    with pytest.raises(V.ValidationError):
        V.validate_network_name(bad)


@pytest.mark.parametrize("good", ["my_app-1", "web.1", "abc123"])
def test_docker_name_accepts_valid(good):
    assert V.validate_container_id(good) == good


# ---------------------------------------------------------------------------
# nginx / compose / cron path validation
# ---------------------------------------------------------------------------
def test_nginx_paths_outside_sites_rejected(tmp_policy, tmp_path):
    bad = tmp_path / "elsewhere.conf"
    bad.write_text("x")
    with pytest.raises(V.ValidationError):
        V.validate_nginx_site_paths([str(bad)], tmp_policy)


def test_nginx_paths_inside_sites_accepted(tmp_policy):
    site_dir = tmp_policy["nginx_site_roots"][0]
    os.makedirs(site_dir)
    site = os.path.join(site_dir, "app.conf")
    with open(site, "w") as fh:
        fh.write("server {}\n")
    got = V.validate_nginx_site_paths([site], tmp_policy)
    assert got == [os.path.realpath(site)]


def test_compose_files_must_exist_and_be_under_roots(tmp_policy):
    approved = tmp_policy["approved_deletion_roots"][0]
    f = os.path.join(approved, "docker-compose.yml")
    with open(f, "w") as fh:
        fh.write("services: {}\n")
    assert V.validate_compose_files([f], tmp_policy) == [os.path.realpath(f)]
    with pytest.raises(V.ValidationError):
        V.validate_compose_files([os.path.join(approved, "missing.yml")], tmp_policy)


# ---------------------------------------------------------------------------
# operation-level guards (no host mutation)
# ---------------------------------------------------------------------------
def test_volume_rm_requires_confirmed_twice(tmp_policy):
    ops = H.Operations(tmp_policy)
    with pytest.raises(H.OpError):
        ops.volume_rm({"volume_name": "myvol", "confirmed_twice": False},
                      dry_run=True)
    # with confirmation, dry-run returns the command and does not execute
    res = ops.volume_rm({"volume_name": "myvol", "confirmed_twice": True},
                        dry_run=True)
    assert "docker" in res["output"] and "volume" in res["output"]


def test_path_delete_dry_run_does_not_execute(tmp_policy):
    approved = tmp_policy["approved_deletion_roots"][0]
    target = os.path.join(approved, "doomed")
    os.mkdir(target)
    ops = H.Operations(tmp_policy)
    res = ops.path_delete({"path": target}, dry_run=True)
    cmds = json.loads(res["output"])
    assert cmds == [["rm", "-rf", "--one-file-system", "--",
                     os.path.realpath(target)]]
    assert os.path.isdir(target)  # still present -> nothing executed


# ---------------------------------------------------------------------------
# request dispatch: unknown op + malformed JSON
# ---------------------------------------------------------------------------
def _auditor(tmp_path):
    return H.Auditor(str(tmp_path / "audit.log"))


def test_unknown_op(tmp_policy, tmp_path):
    ops = H.Operations(tmp_policy)
    resp = H.handle_request(json.dumps({"op": "nope", "args": {}}).encode(),
                            ops, _auditor(tmp_path))
    assert resp["ok"] is False and "unknown op" in resp["error"]


def test_malformed_json(tmp_policy, tmp_path):
    ops = H.Operations(tmp_policy)
    resp = H.handle_request(b"{not json", ops, _auditor(tmp_path))
    assert resp["ok"] is False and "malformed" in resp["error"]


def test_private_method_not_callable_as_op(tmp_policy, tmp_path):
    ops = H.Operations(tmp_policy)
    resp = H.handle_request(json.dumps({"op": "_container_action"}).encode(),
                            ops, _auditor(tmp_path))
    assert resp["ok"] is False


# ---------------------------------------------------------------------------
# end-to-end socket server: ping + dry-run path_delete
# ---------------------------------------------------------------------------
def _send(sock_path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode())


def test_server_end_to_end(tmp_policy, tmp_path):
    sock_path = str(tmp_path / "helper.sock")
    audit = str(tmp_path / "audit.log")
    server = H.DelHelperServer(sock_path, tmp_policy, audit)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # socket exists and is mode 0660
        deadline = time.time() + 5
        while not os.path.exists(sock_path) and time.time() < deadline:
            time.sleep(0.05)
        assert os.path.exists(sock_path)
        assert (os.stat(sock_path).st_mode & 0o777) == 0o660

        # ping
        resp = _send(sock_path, {"op": "ping"})
        assert resp["ok"] is True and resp["output"] == "pong"

        # dry-run path_delete over the wire does not execute
        approved = tmp_policy["approved_deletion_roots"][0]
        target = os.path.join(approved, "e2e-victim")
        os.mkdir(target)
        resp = _send(sock_path, {"op": "path_delete", "args": {"path": target},
                                 "dry_run": True})
        assert resp["ok"] is True and resp["dry_run"] is True
        assert os.path.isdir(target)

        # unknown op over the wire
        resp = _send(sock_path, {"op": "bogus"})
        assert resp["ok"] is False
    finally:
        server.shutdown()
        server.server_close()

    # audit log captured lines
    with open(audit) as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    ops_logged = {rec["op"] for rec in lines}
    assert "ping" in ops_logged and "path_delete" in ops_logged
