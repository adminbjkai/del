"""Process/port discovery source: `ss -lntp` (listening ports + owning process),
`ps` (long-running processes), and `tmux ls` (persistent sessions). Read-only:
no process is signalled, no port is touched, no tmux session is created/killed.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess

from del_app.models import Resource

logger = logging.getLogger("del_app.discovery.proc_src")

TIMEOUT = 20
LONG_RUNNING_ETIMES_THRESHOLD = 300  # 5 min: excludes short-lived/transient procs
# On a long-uptime host almost every process is "long running" by etimes alone
# (uptime dominates). What's actually useful for correlation is processes whose
# cwd sits under a scan root - i.e. directly host-run app processes, not
# containerized workloads or generic system daemons.
SCAN_ROOT_PREFIXES = ("/apps", "/data/apps", "/opt", "/srv", "/var/www")

# A command line can legitimately carry a secret-shaped flag (e.g.
# `--password=...`, `--token=...`); strip the value before this ever reaches
# the DB/UI. Mirrors del_app.jobs.sanitize_output's pattern.
_SECRET_ARG_RE = re.compile(r"(?i)([-]{0,2}(?:password|token|secret|api[_-]?key|key)[=: ])\S+")


def _sanitize_args(args: str) -> str:
    """Redact secret-shaped values from a process's command line before it is
    stored/displayed. Truncated separately by the caller."""
    return _SECRET_ARG_RE.sub(r"\1***", args)


def _run(args: list[str]) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, check=False)
        return proc.stdout if proc.returncode == 0 else ""
    except Exception:
        logger.exception("proc_src command errored: %s", args)
        return ""


_DOCKER_CGROUP_RE = re.compile(r"(?:/docker/|docker-)([0-9a-f]{64})")
_SYSTEMD_CGROUP_RE = re.compile(
    r"/(?:system|user)\.slice/(?:[^/\n]+\.slice/)*([A-Za-z0-9@_.\-]+\.service)"
)


def _docker_container_map() -> dict[str, str]:
    """Full container id (64 hex, --no-trunc) -> container name, so a pid's
    cgroup (which embeds the container id) can be resolved to a name even for
    host-network containers that have no published port mapping to key off."""
    raw = _run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"])
    mapping: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        cid, name = parts
        if cid:
            mapping[cid] = name
    return mapping


def _cgroup_owner(pid: int, docker_map: dict[str, str]) -> tuple[str | None, str | None]:
    """Return (docker_container_name, systemd_unit) owning this pid, resolved
    from /proc/<pid>/cgroup. Either or both may be None. Read-only (only reads
    a /proc file already world-readable for the pid's owner)."""
    try:
        with open(f"/proc/{pid}/cgroup", "r") as f:
            content = f.read()
    except OSError:
        return None, None

    container_name = None
    m = _DOCKER_CGROUP_RE.search(content)
    if m:
        container_name = docker_map.get(m.group(1))

    unit = None
    m2 = _SYSTEMD_CGROUP_RE.search(content)
    if m2:
        unit = m2.group(1)

    return container_name, unit


def _proc_fd_socket_inodes(pid: int) -> set[str]:
    """Socket inodes among pid's open fds, when /proc/<pid>/fd is readable
    (same-uid processes, or root). Most host-network containers run as a
    different, often root, uid and this is *not* readable without sudo -
    that's an honest, documented gap (see `_host_network_container_ports`),
    not silently faked."""
    inodes: set[str] = set()
    try:
        fd_dir = f"/proc/{pid}/fd"
        for entry in os.listdir(fd_dir):
            try:
                target = os.readlink(f"{fd_dir}/{entry}")
            except OSError:
                continue
            m = re.match(r"socket:\[(\d+)\]", target)
            if m:
                inodes.add(m.group(1))
    except OSError:
        pass
    return inodes


def _listen_sockets() -> list[tuple[int, str, int]]:
    """Parse /proc/net/tcp[6] - this is always the calling process's own
    network namespace, which for a normal (non-containerized) host process is
    the host namespace - for LISTEN sockets. Returns (port, inode, uid).
    No privilege needed: these files are world-readable."""
    out: list[tuple[int, str, int]] = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            local, state, uid_s, inode = parts[1], parts[3], parts[7], parts[9]
            if state != "0A":  # TCP_LISTEN
                continue
            try:
                port = int(local.rsplit(":", 1)[1], 16)
                out.append((port, inode, int(uid_s)))
            except ValueError:
                continue
    return out


def _host_network_container_ports() -> dict[int, str]:
    """port -> container name, for host-network containers, resolved WITHOUT
    sudo. `sudo -n ss -lntp` (the ideal path) is blocked in production by
    del-web.service's NoNewPrivileges=yes, so `ss` never learns a pid and the
    cgroup-based match in `_collect_ports` can't fire for these. Instead:
    `docker inspect` gives each host-network container's main pid/uid for
    free (docker group membership, no root needed); that pid's open socket
    fds (readable when the container happens to run as the same uid as
    del-web) or, failing that, a uid unique to exactly one running
    host-network container, are matched against /proc/net/tcp[6]'s listening
    sockets. Containers that both run as a different uid *and* share that uid
    with another host-network container (commonly: several running as root)
    can't be disambiguated this way and are left unresolved rather than
    guessed at."""
    result: dict[int, str] = {}
    raw = _run(["docker", "ps", "--filter", "network=host", "--format", "{{.Names}}"])
    names = [n for n in raw.splitlines() if n.strip()]
    if not names:
        return result

    listen = _listen_sockets()
    inode_to_port = {inode: port for port, inode, _uid in listen}
    uid_ports: dict[int, list[int]] = {}
    for port, _inode, uid in listen:
        uid_ports.setdefault(uid, []).append(port)

    container_uid: dict[str, int] = {}
    for name in names:
        pid_raw = _run(["docker", "inspect", "-f", "{{.State.Pid}}", name]).strip()
        if not pid_raw.isdigit():
            continue
        pid = int(pid_raw)
        try:
            with open(f"/proc/{pid}/status") as f:
                status = f.read()
        except OSError:
            continue
        m = re.search(r"^Uid:\s+(\d+)", status, re.MULTILINE)
        if not m:
            continue
        container_uid[name] = int(m.group(1))

        for inode in _proc_fd_socket_inodes(pid):
            port = inode_to_port.get(inode)
            if port:
                result[port] = name

    uid_owner_count: dict[int, int] = {}
    for uid in container_uid.values():
        uid_owner_count[uid] = uid_owner_count.get(uid, 0) + 1
    for name, uid in container_uid.items():
        if uid_owner_count[uid] != 1:
            continue  # ambiguous: 2+ host-network containers share this uid
        for port in uid_ports.get(uid, []):
            result.setdefault(port, name)

    return result


def _collect_ports() -> list[Resource]:
    resources: list[Resource] = []
    raw = _run(["sudo", "-n", "ss", "-lntp"])
    if not raw:
        # NoNewPrivileges blocks sudo under del-web; ask the root helper for
        # the same read-only listing instead.
        try:
            from del_app import helper_client
            resp = helper_client.call("list_listeners", {}, dry_run=False, timeout=30)
            if resp.get("ok"):
                raw = resp.get("output") or ""
        except Exception:
            logger.warning("helper list_listeners unavailable, falling back to unprivileged ss")
    if not raw:
        raw = _run(["ss", "-lntp"])  # fallback without process owner info
    lines = raw.splitlines()
    docker_map = _docker_container_map()
    host_network_ports = _host_network_container_ports()
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        state = parts[0]
        local = parts[3]
        proc_field = " ".join(parts[5:]) if len(parts) > 5 else ""

        addr, _, port_s = local.rpartition(":")
        if not port_s.isdigit():
            continue
        port = int(port_s)

        pid = None
        process_name = None
        m = re.search(r'\(\("([^"]+)",pid=(\d+)', proc_field)
        if m:
            process_name = m.group(1)
            pid = int(m.group(2))

        container_name = None
        systemd_unit = None
        if pid is not None:
            container_name, systemd_unit = _cgroup_owner(pid, docker_map)
        if container_name is None:
            container_name = host_network_ports.get(port)

        key = f"tcp:{addr}:{port}"
        resources.append(
            Resource(
                type="port",
                key=key,
                display=f"{addr}:{port}",
                path=None,
                state=state.lower(),
                data={
                    "proto": "tcp",
                    "addr": addr,
                    "port": port,
                    "pid": pid,
                    "process": process_name,
                    "container": container_name,
                    "systemd_unit": systemd_unit,
                },
            )
        )
    return resources


def _collect_processes() -> list[Resource]:
    resources: list[Resource] = []
    raw = _run(["ps", "-eo", "pid,ppid,user,etimes,comm,args", "--no-headers"])
    docker_map = _docker_container_map()
    for line in raw.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        pid_s, ppid_s, user, etimes_s, comm = parts[:5]
        args = parts[5] if len(parts) > 5 else comm
        try:
            pid = int(pid_s)
            etimes = int(etimes_s)
        except ValueError:
            continue
        if etimes < LONG_RUNNING_ETIMES_THRESHOLD:
            continue
        if comm.startswith("[") or comm.endswith("]"):
            continue  # kernel thread

        cwd = None
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            pass

        if not cwd or not cwd.startswith(SCAN_ROOT_PREFIXES):
            continue  # not a host-run app process under a scan root; skip noise

        exe = None
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            pass  # e.g. gone by the time we look, or a permission-denied exe link

        container_name, systemd_unit = _cgroup_owner(pid, docker_map)

        resources.append(
            Resource(
                type="process",
                key=f"pid:{pid}:{comm}",
                display=f"{comm} (pid {pid})",
                path=cwd,
                state="running",
                data={
                    "pid": pid,
                    "ppid": int(ppid_s) if ppid_s.isdigit() else None,
                    "user": user,
                    "etimes": etimes,
                    "comm": comm,
                    "exe": exe,
                    "args_redacted": _sanitize_args(args)[:200],
                    "cwd": cwd,
                    "container": container_name,
                    "systemd_unit": systemd_unit,
                },
            )
        )
    return resources


def _collect_tmux() -> list[Resource]:
    resources: list[Resource] = []
    raw = _run(["tmux", "ls"])
    for line in raw.splitlines():
        m = re.match(r"^([^:]+):\s*(\d+)\s+windows?\s*\(created (.+?)\)", line)
        if not m:
            continue
        name, windows, created = m.group(1), m.group(2), m.group(3)
        resources.append(
            Resource(
                type="tmux_session",
                key=f"tmux:{name}",
                display=name,
                path=None,
                state="active",
                data={"session": name, "windows": int(windows), "created": created},
            )
        )
    return resources


def collect() -> list[Resource]:
    """Collect listening ports, long-running processes, and tmux sessions.
    Read-only; tolerates missing sudo/tmux gracefully."""
    resources: list[Resource] = []

    try:
        resources.extend(_collect_ports())
    except Exception:
        logger.exception("proc_src: port collection failed")

    try:
        resources.extend(_collect_processes())
    except Exception:
        logger.exception("proc_src: process collection failed")

    try:
        resources.extend(_collect_tmux())
    except Exception:
        logger.exception("proc_src: tmux collection failed")

    return resources
