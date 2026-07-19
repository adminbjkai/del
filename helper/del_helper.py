#!/usr/bin/env python3
"""DEL privileged helper daemon.

A small, dependency-free (stdlib only) root daemon that exposes a fixed,
allow-listed set of privileged operations over a unix socket. del-web never runs
shell as root; it sends typed operation names + structured arguments here, and
this daemon re-validates every argument independently before touching the host.

Protocol
--------
Newline-delimited JSON, one request object per connection, max 1 MiB.
  request:  {op, args, dry_run, plan_id?, step_id?}
  response: {ok, dry_run, output, error, changed:[...]}

Every request (including denials and dry runs) is appended as a single JSON line
to the audit log. Argument *values* here are paths / object names — never secrets
— so they are logged verbatim; no secret values ever pass through this daemon.
"""

from __future__ import annotations

import datetime
import grp
import json
import os
import pwd
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import validation as V  # noqa: E402

DEFAULT_SOCKET = "/run/del/helper.sock"
DEFAULT_POLICY = "/apps/del/config/helper-policy.json"
DEFAULT_AUDIT_LOG = "/apps/del/logs/helper-audit.log"
MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_CMD_TIMEOUT = 300
TERM_GRACE_SECONDS = 10


class OpError(Exception):
    """Raised inside an operation to produce {ok: false, error: <msg>}."""


# ---------------------------------------------------------------------------
# audit logging
# ---------------------------------------------------------------------------
class Auditor:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def log(self, record: dict) -> None:
        record = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  **record}
        line = json.dumps(record, default=str, sort_keys=True)
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                # fall back to stderr (e.g. non-root test runs where /apps/del/logs
                # is not writable) — never crash on audit failure.
                sys.stderr.write("AUDIT " + line + "\n")
                sys.stderr.flush()


# ---------------------------------------------------------------------------
# subprocess helper
# ---------------------------------------------------------------------------
def _run(cmd: list, timeout: int = DEFAULT_CMD_TIMEOUT) -> tuple[int, str, str]:
    """Run an argument array with shell=False; return (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, shell=False, timeout=timeout,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        raise OpError(f"command timed out after {timeout}s: {cmd}")
    except FileNotFoundError:
        raise OpError(f"command not found: {cmd[0]}")
    return proc.returncode, proc.stdout, proc.stderr


def _fmt_cmds(cmds: list) -> str:
    """Render the exact command list(s) that would run, for dry-run output."""
    return json.dumps(cmds)


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------
class Operations:
    """Each method: (args: dict, dry_run: bool, policy: dict) -> result dict.

    Result dict keys: output(str), changed(list). Missing keys default sensibly.
    On dry_run the method must return the exact command(s) it *would* run and
    must not mutate host state (read-only precondition checks are allowed).
    """

    def __init__(self, policy: dict):
        self.policy = policy

    # -- health --------------------------------------------------------------
    def list_listeners(self, args, dry_run):
        # Read-only: full `ss -lntp` (with pid/process info, which needs root).
        # Exists so del-web (NoNewPrivileges, no sudo) can resolve listener
        # ownership without any privilege escalation of its own.
        rc, out, err = _run(["ss", "-lntp"])
        if rc != 0:
            raise OpError(f"ss failed (rc={rc}): {err.strip()}")
        return {"output": out, "changed": []}

    def ping(self, args, dry_run):
        return {"output": "pong", "changed": []}

    # -- docker: compose -----------------------------------------------------
    def compose_down(self, args, dry_run):
        project = args.get("project")
        if not isinstance(project, str) or not project:
            raise OpError("compose_down requires 'project'")
        V.validate_container_id(project)  # project names share the docker-safe charset
        files = V.validate_compose_files(args.get("config_files"), self.policy)
        cmd = ["docker", "compose", "-p", project]
        for f in files:
            cmd += ["-f", f]
        cmd += ["down"]
        if args.get("remove_volumes"):
            cmd += ["--volumes"]
        mode = args.get("remove_images_mode")
        if mode in ("all", "local"):
            cmd += ["--rmi", mode]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"compose down failed (rc={rc}): {err.strip() or out.strip()}")
        return {"output": out + err, "changed": [f"compose:{project}"]}

    # -- docker: containers --------------------------------------------------
    def container_stop(self, args, dry_run):
        return self._container_action(args, dry_run, "stop")

    def container_rm(self, args, dry_run):
        return self._container_action(args, dry_run, "rm")

    def _container_action(self, args, dry_run, action):
        cid = V.validate_container_id(args.get("container_id", ""))
        # read-only precondition: the container must exist (allowed during dry_run)
        rc, out, err = _run(["docker", "inspect", "--type", "container",
                             "--format", "{{.Id}}", cid])
        if rc != 0:
            # already gone — desired state for stop/rm alike
            return {"output": f"container {cid} already absent", "changed": []}
        cmd = ["docker", action, cid]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"docker {action} failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"container:{cid}"]}

    # -- docker: images ------------------------------------------------------
    def image_rm(self, args, dry_run):
        image_id = V.validate_image_id(args.get("image_id", ""))
        allowed = set(args.get("allowed_container_ids", []) or [])
        for c in allowed:
            V.validate_container_id(c)
        # read-only precondition: which containers reference this image?
        rc, out, err = _run(["docker", "ps", "-a", "--filter",
                             f"ancestor={image_id}", "--format", "{{.ID}} {{.Names}}"])
        if rc != 0:
            raise OpError(f"docker ps failed (rc={rc}): {err.strip()}")
        referencing = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if parts:
                referencing[parts[0]] = parts[1] if len(parts) > 1 else ""
        # callers may know containers by name or (short/full) id
        def _img_allowed(cid, cname):
            return any(a == cid or a == cname or a.startswith(cid) or cid.startswith(a)
                       for a in allowed)
        remainder = [cid for cid, cname in referencing.items()
                     if not _img_allowed(cid, cname)]
        if remainder:
            raise OpError(
                f"image {image_id} still referenced by containers: {sorted(remainder)}")
        cmd = ["docker", "rmi", image_id]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        rc, out, err = _run(cmd)
        if rc != 0:
            if "no such image" in (err or "").lower():
                return {"output": f"image {image_id} already absent", "changed": []}
            raise OpError(f"docker rmi failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"image:{image_id}"]}

    # -- docker: volumes -----------------------------------------------------
    def volume_rm(self, args, dry_run):
        name = V.validate_volume_name(args.get("volume_name", ""))
        if args.get("confirmed_twice") is not True:
            raise OpError("volume_rm requires confirmed_twice=true (double confirmation)")
        cmd = ["docker", "volume", "rm", name]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        rc, out, err = _run(cmd)
        if rc != 0:
            if "no such volume" in (err or "").lower():
                return {"output": f"volume {name} already absent", "changed": []}
            raise OpError(f"docker volume rm failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"volume:{name}"]}

    # -- docker: networks ----------------------------------------------------
    def network_rm(self, args, dry_run):
        name = V.validate_network_name(args.get("network_name", ""))
        if name in ("bridge", "host", "none"):
            raise OpError(f"refusing to remove built-in network: {name}")
        # read-only precondition: inspect for attached containers
        rc, out, err = _run(["docker", "network", "inspect", name])
        if rc != 0:
            # already gone (e.g. compose down removed it) — desired state reached
            return {"output": f"network {name} already absent", "changed": []}
        try:
            info = json.loads(out)
            containers = info[0].get("Containers", {}) if info else {}
        except (ValueError, IndexError, KeyError):
            raise OpError(f"could not parse network inspect for {name}")
        cmd = ["docker", "network", "rm", name]
        if containers:
            # Containers attached. In a dry run, earlier plan steps (container
            # removal) were only simulated, so attached containers belonging to
            # the same plan are expected: accept them if they are all within
            # the caller-approved set. In live mode, any attachment is fatal.
            allowed = set(args.get("allowed_container_ids") or [])
            # containers maps full-id -> {"Name": ..., ...}; the caller may
            # know containers by name or by (possibly short) id.
            attached = set(containers)
            def _is_allowed(cid):
                name = (containers.get(cid) or {}).get("Name", "")
                return (cid in allowed or name in allowed
                        or any(a.startswith(cid) or cid.startswith(a) for a in allowed))
            foreign = {c for c in attached if not _is_allowed(c)}
            if foreign or not dry_run:
                raise OpError(
                    f"network {name} has attached containers: {sorted(attached)}")
            return {"output": "would verify no containers remain attached, then:\n"
                              + _fmt_cmds([cmd]),
                    "changed": []}
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"docker network rm failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"network:{name}"]}

    # -- systemd -------------------------------------------------------------
    def systemd_stop(self, args, dry_run):
        return self._systemctl(args, dry_run, "stop")

    def systemd_disable(self, args, dry_run):
        return self._systemctl(args, dry_run, "disable")

    def _systemctl(self, args, dry_run, action):
        unit = V.validate_unit_name(args.get("unit", ""))
        cmd = ["systemctl", action, unit]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"systemctl {action} failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"unit:{unit}"]}

    def systemd_rm_unit(self, args, dry_run):
        unit_file = V.validate_unit_name(args.get("unit", ""), self.policy,
                                         require_unit_file=True)
        cmds = [["rm", "--", unit_file], ["systemctl", "daemon-reload"]]
        if dry_run:
            return {"output": _fmt_cmds(cmds), "changed": []}
        rc, out, err = _run(cmds[0])
        if rc != 0:
            raise OpError(f"rm unit file failed (rc={rc}): {err.strip()}")
        rc2, out2, err2 = _run(cmds[1])
        if rc2 != 0:
            raise OpError(f"daemon-reload failed (rc={rc2}): {err2.strip()}")
        return {"output": out + out2, "changed": [f"unit_file:{unit_file}"]}

    # -- cron ----------------------------------------------------------------
    def cron_rm(self, args, dry_run):
        path = V.validate_cron_path(args.get("path", ""), self.policy)
        cmd = ["rm", "--", path]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"rm cron file failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"cron:{path}"]}

    # -- nginx ---------------------------------------------------------------
    def nginx_rm_site(self, args, dry_run):
        paths = V.validate_nginx_site_paths(args.get("paths"), self.policy)
        backup_dir = self.policy.get("backup_dir", "/apps/del/backups")
        ts = _timestamp()
        # plan: backup each file, remove each, nginx -t, reload-or-restore
        planned_backups = [
            (p, os.path.join(
                backup_dir,
                f"nginx-{os.path.basename(os.path.dirname(p))}-{os.path.basename(p)}.{ts}.bak"))
            for p in paths
        ]
        if dry_run:
            cmds = []
            for src, dst in planned_backups:
                cmds.append(["cp", "-a", "--", src, dst])
            for src, _ in planned_backups:
                cmds.append(["rm", "--", src])
            cmds.append(["nginx", "-t"])
            cmds.append(["systemctl", "reload", "nginx"])
            return {"output": _fmt_cmds(cmds), "changed": []}

        # live: backup first
        made_backups = []
        for src, dst in planned_backups:
            rc, out, err = _run(["cp", "-a", "--", src, dst])
            if rc != 0:
                raise OpError(f"backup failed for {src}: {err.strip()}")
            made_backups.append((src, dst))
        # remove
        for src, _ in made_backups:
            rc, out, err = _run(["rm", "--", src])
            if rc != 0:
                # restore what we removed so far and abort
                self._restore_all(made_backups)
                raise OpError(f"rm failed for {src}: {err.strip()}")
        # validate config
        rc, out, err = _run(["nginx", "-t"])
        if rc != 0:
            self._restore_all(made_backups)
            raise OpError(
                f"nginx -t failed after removal, restored files, reloaded nothing: "
                f"{err.strip()}")
        # passed: reload
        rc, out, err = _run(["systemctl", "reload", "nginx"])
        if rc != 0:
            raise OpError(f"nginx reload failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [p for p, _ in made_backups]}

    def _restore_all(self, backups):
        for src, dst in backups:
            _run(["cp", "-a", "--", dst, src])

    def nginx_test(self, args, dry_run):
        # Read-only: nginx -t only, never reloads. Safe in dry_run and live.
        rc, out, err = _run(["nginx", "-t"])
        if rc != 0:
            raise OpError(f"nginx -t failed: {err.strip()}")
        return {"output": out + err, "changed": []}

    def nginx_test_reload(self, args, dry_run):
        cmds = [["nginx", "-t"], ["systemctl", "reload", "nginx"]]
        if dry_run:
            return {"output": _fmt_cmds(cmds), "changed": []}
        rc, out, err = _run(cmds[0])
        if rc != 0:
            raise OpError(f"nginx -t failed: {err.strip()}")
        rc2, out2, err2 = _run(cmds[1])
        if rc2 != 0:
            raise OpError(f"nginx reload failed: {err2.strip()}")
        return {"output": out + err + out2 + err2, "changed": ["nginx:reloaded"]}

    # -- filesystem ----------------------------------------------------------
    def path_delete(self, args, dry_run):
        realpath = V.validate_path_for_deletion(args.get("path", ""), self.policy)
        # defence-in-depth guard, matches validation but re-asserted at exec site
        assert realpath.count("/") >= 2, "refusing shallow path"
        cmd = ["rm", "-rf", "--one-file-system", "--", realpath]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"rm -rf failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"path:{realpath}"]}

    # -- tmux ----------------------------------------------------------------
    def tmux_kill(self, args, dry_run):
        session = args.get("session", "")
        if not isinstance(session, str) or not session or "\n" in session:
            raise OpError(f"invalid tmux session name: {session!r}")
        cmd = ["tmux", "kill-session", "-t", session]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"tmux kill-session failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"tmux:{session}"]}

    # -- processes -----------------------------------------------------------
    def process_term(self, args, dry_run):
        pid = args.get("pid")
        expected_exe = args.get("expected_exe", "")
        if not isinstance(pid, int) or pid <= 1:
            raise OpError(f"invalid pid: {pid!r}")
        if not isinstance(expected_exe, str) or not expected_exe:
            raise OpError("process_term requires expected_exe")
        exe_link = f"/proc/{pid}/exe"
        # read-only precondition: verify the pid still maps to the expected exe
        try:
            actual = os.path.realpath(os.readlink(exe_link))
        except OSError:
            raise OpError(f"pid {pid} not running or /proc/{pid}/exe unreadable")
        if actual != os.path.realpath(expected_exe):
            raise OpError(
                f"pid {pid} exe mismatch: {actual!r} != expected {expected_exe!r}")
        if dry_run:
            return {"output": _fmt_cmds([["kill", "-TERM", str(pid)],
                                        ["kill", "-KILL", str(pid)]]),
                    "changed": []}
        # re-verify then TERM
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + TERM_GRACE_SECONDS
        while time.time() < deadline:
            if not os.path.exists(f"/proc/{pid}"):
                return {"output": f"pid {pid} terminated (SIGTERM)",
                        "changed": [f"pid:{pid}"]}
            time.sleep(0.25)
        # still alive: re-verify identity before the more forceful kill
        try:
            still = os.path.realpath(os.readlink(exe_link))
        except OSError:
            return {"output": f"pid {pid} gone before SIGKILL",
                    "changed": [f"pid:{pid}"]}
        if still != os.path.realpath(expected_exe):
            raise OpError(f"pid {pid} recycled before SIGKILL, refusing")
        os.kill(pid, signal.SIGKILL)
        return {"output": f"pid {pid} killed (SIGKILL after {TERM_GRACE_SECONDS}s)",
                "changed": [f"pid:{pid}"]}

    # -- backups -------------------------------------------------------------
    def backup_tar(self, args, dry_run):
        src = args.get("src_path", "")
        if not isinstance(src, str) or not os.path.isabs(src):
            raise OpError(f"src_path must be absolute: {src!r}")
        src_real = os.path.realpath(src)
        if not os.path.exists(src_real):
            raise OpError(f"src_path does not exist: {src!r}")
        dest = V.validate_backup_dest(args.get("dest", ""), self.policy)
        parent = os.path.dirname(src_real) or "/"
        base = os.path.basename(src_real)
        cmd = ["tar", "-czf", dest, "-C", parent, base]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"tar failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"backup:{dest}"]}

    def volume_backup(self, args, dry_run):
        volume = V.validate_volume_name(args.get("volume", ""))
        dest = V.validate_backup_dest(args.get("dest", ""), self.policy)
        dest_dir = os.path.dirname(dest)
        dest_name = os.path.basename(dest)
        cmd = ["docker", "run", "--rm",
               "-v", f"{volume}:/src:ro",
               "-v", f"{dest_dir}:/backup",
               "busybox", "tar", "-czf", f"/backup/{dest_name}", "-C", "/src", "."]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        os.makedirs(dest_dir, exist_ok=True)
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"volume backup failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"backup:{dest}"]}

    def file_backup(self, args, dry_run):
        path = args.get("path", "")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise OpError(f"path must be absolute: {path!r}")
        path_real = os.path.realpath(path)
        if not os.path.isfile(path_real):
            raise OpError(f"path is not a file: {path!r}")
        dest = args.get("dest")
        if not dest:
            backup_dir = self.policy.get("backup_dir", "/apps/del/backups")
            dest = os.path.join(
                backup_dir, f"{os.path.basename(path_real)}.{_timestamp()}.bak")
        dest = V.validate_backup_dest(dest, self.policy)
        cmd = ["cp", "-a", "--", path_real, dest]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        os.makedirs(os.path.dirname(dest) or "/", exist_ok=True)
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"file backup failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"backup:{dest}"]}

    # -- restore -------------------------------------------------------------
    def path_restore(self, args, dry_run):
        backup_path = V.validate_backup_source(args.get("backup_path", ""),
                                               self.policy)
        original = args.get("original_path", "")
        if not isinstance(original, str) or not os.path.isabs(original):
            raise OpError(f"original_path must be absolute: {original!r}")
        cmd = ["cp", "-a", "--", backup_path, original]
        if dry_run:
            return {"output": _fmt_cmds([cmd]), "changed": []}
        rc, out, err = _run(cmd)
        if rc != 0:
            raise OpError(f"restore failed (rc={rc}): {err.strip()}")
        return {"output": out + err, "changed": [f"restored:{original}"]}


# ---------------------------------------------------------------------------
# request dispatch
# ---------------------------------------------------------------------------
def handle_request(raw: bytes, ops: Operations, auditor: Auditor) -> dict:
    """Parse one request, dispatch, and always return a well-formed response."""
    op = None
    args = {}
    dry_run = True
    try:
        req = json.loads(raw.decode("utf-8"))
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
        op = req.get("op")
        args = req.get("args") or {}
        dry_run = bool(req.get("dry_run", True))
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        if not isinstance(op, str):
            raise ValueError("missing 'op'")

        handler = getattr(ops, op, None)
        if handler is None or op.startswith("_") or not callable(handler) \
                or op not in ALLOWED_OPS:
            raise OpError(f"unknown op: {op!r}")

        result = handler(args, dry_run)
        resp = {"ok": True, "dry_run": dry_run,
                "output": result.get("output", ""),
                "error": None,
                "changed": result.get("changed", [])}
    except (V.ValidationError, OpError) as exc:
        resp = {"ok": False, "dry_run": dry_run, "output": "",
                "error": str(exc), "changed": []}
    except ValueError as exc:
        resp = {"ok": False, "dry_run": dry_run, "output": "",
                "error": f"malformed request: {exc}", "changed": []}
    except Exception as exc:  # never crash the connection
        resp = {"ok": False, "dry_run": dry_run, "output": "",
                "error": f"internal error: {exc}", "changed": []}

    auditor.log({"op": op, "args": args, "dry_run": dry_run,
                 "ok": resp["ok"], "error": resp["error"]})
    return resp


ALLOWED_OPS = {
    "ping", "compose_down", "container_stop", "container_rm", "image_rm",
    "volume_rm", "network_rm", "systemd_stop", "systemd_disable", "nginx_test", "list_listeners",
    "systemd_rm_unit", "cron_rm", "nginx_rm_site", "nginx_test_reload",
    "path_delete", "tmux_kill", "process_term", "backup_tar", "volume_backup",
    "file_backup", "path_restore",
}


# ---------------------------------------------------------------------------
# socket server
# ---------------------------------------------------------------------------
class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server: "DelHelperServer" = self.server  # type: ignore[assignment]
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        except OSError:
            return
        if not raw:
            return
        if len(raw) > MAX_REQUEST_BYTES:
            resp = {"ok": False, "dry_run": True, "output": "",
                    "error": "request too large", "changed": []}
        else:
            resp = handle_request(raw, server.ops, server.auditor)
        try:
            self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
            self.wfile.flush()
        except OSError:
            pass


class DelHelperServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, policy: dict, audit_log: str):
        self.socket_path = socket_path
        self.policy = policy
        self.ops = Operations(policy)
        self.auditor = Auditor(audit_log)
        os.makedirs(os.path.dirname(socket_path) or ".", exist_ok=True)
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        super().__init__(socket_path, _Handler)
        self._secure_socket()

    def _secure_socket(self):
        """chmod 0660 and (if root) chown root:bjkai; degrade gracefully otherwise."""
        try:
            os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR |
                     stat.S_IRGRP | stat.S_IWGRP)  # 0660
        except OSError:
            pass
        if os.geteuid() == 0:
            try:
                uid = pwd.getpwnam("root").pw_uid
                gid = grp.getgrnam("bjkai").gr_gid
                os.chown(self.socket_path, uid, gid)
            except (KeyError, OSError):
                pass


def load_policy(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    socket_path = argv[0] if argv else DEFAULT_SOCKET
    policy_path = argv[1] if len(argv) > 1 else DEFAULT_POLICY
    audit_log = os.environ.get("DEL_HELPER_AUDIT_LOG", DEFAULT_AUDIT_LOG)
    policy = load_policy(policy_path)
    server = DelHelperServer(socket_path, policy, audit_log)
    server.auditor.log({"op": "_startup", "args": {"socket": socket_path},
                        "dry_run": False, "ok": True, "error": None})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        try:
            os.unlink(socket_path)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:  # startup failure — log & exit non-zero
        traceback.print_exc()
        sys.exit(1)
