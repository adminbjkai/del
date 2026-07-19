"""Cron discovery source: /etc/crontab, /etc/cron.d/*, the periodic run-parts
directories (cron.hourly/daily/weekly/monthly), and per-user crontabs (via
`sudo crontab -l -u <user>`, read-only). Never edits any cron file or crontab.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess

from del_app.models import Resource

logger = logging.getLogger("del_app.discovery.cron_src")

TIMEOUT = 10
CRONTAB_LINE_RE = re.compile(
    r"^\s*(@\w+|\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(\S+)\s+(.*)$"
)
PERIODIC_DIRS = {
    "/etc/cron.hourly": "hourly",
    "/etc/cron.daily": "daily",
    "/etc/cron.weekly": "weekly",
    "/etc/cron.monthly": "monthly",
}


def _read(path: str) -> str | None:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def _referenced_paths(command: str) -> list[str]:
    return re.findall(r"(/[\w./\-]+)", command)


def _parse_system_crontab(path: str, resources: list[Resource]) -> None:
    text = _read(path)
    if text is None:
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped.split()[0] and " " not in stripped.split("=", 1)[0]:
            continue  # env var assignment line e.g. SHELL=/bin/sh, PATH=...
        m = CRONTAB_LINE_RE.match(stripped)
        if not m:
            continue
        schedule, user, command = m.group(1), m.group(2), m.group(3)
        key = f"{path}:{schedule}:{user}:{command}"
        resources.append(
            Resource(
                type="cron_entry",
                key=key,
                display=f"[{user}] {command[:80]}",
                path=path,
                state="active",
                data={
                    "source": path,
                    "user": user,
                    "schedule": schedule,
                    "command": command,
                    "referenced_paths": _referenced_paths(command),
                },
            )
        )


def _parse_periodic_dirs(resources: list[Resource]) -> None:
    for dir_path, freq in PERIODIC_DIRS.items():
        try:
            if not os.path.isdir(dir_path):
                continue
            for name in sorted(os.listdir(dir_path)):
                full = os.path.join(dir_path, name)
                resources.append(
                    Resource(
                        type="cron_entry",
                        key=full,
                        display=f"[{freq}] {name}",
                        path=full,
                        state="active",
                        data={"source": dir_path, "frequency": freq, "script": name},
                    )
                )
        except Exception:
            logger.exception("cron_src: failed to list %s", dir_path)


def _candidate_users() -> list[str]:
    users = []
    try:
        with open("/etc/passwd", "r") as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) < 7:
                    continue
                name, _, uid, _, _, home, shell = parts[:7]
                if shell.endswith("nologin") or shell.endswith("false") or shell == "":
                    continue
                if not os.path.isdir(home):
                    continue
                users.append(name)
    except Exception:
        logger.exception("cron_src: failed to read /etc/passwd")
    return users


def _parse_user_crontabs(resources: list[Resource]) -> None:
    for user in _candidate_users():
        try:
            proc = subprocess.run(
                ["sudo", "-n", "crontab", "-l", "-u", user],
                capture_output=True, text=True, timeout=TIMEOUT, check=False,
            )
        except Exception:
            logger.exception("cron_src: crontab -l failed for %s", user)
            continue
        if proc.returncode != 0:
            continue  # no crontab for this user
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.split()[0].count("=") and " " not in stripped.split("=", 1)[0]:
                continue  # env assignment e.g. PATH=...
            m = re.match(r"^(@\w+|\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.*)$", stripped)
            if not m:
                continue
            schedule, command = m.group(1), m.group(2)
            key = f"user-crontab:{user}:{schedule}:{command}"
            resources.append(
                Resource(
                    type="cron_entry",
                    key=key,
                    display=f"[{user}] {command[:80]}",
                    path=None,
                    state="active",
                    data={
                        "source": f"user-crontab:{user}",
                        "user": user,
                        "schedule": schedule,
                        "command": command,
                        "referenced_paths": _referenced_paths(command),
                    },
                )
            )


def collect() -> list[Resource]:
    """Collect cron entries from /etc/crontab, /etc/cron.d, the periodic
    run-parts directories, and per-user crontabs. Read-only."""
    resources: list[Resource] = []

    try:
        _parse_system_crontab("/etc/crontab", resources)
    except Exception:
        logger.exception("cron_src: /etc/crontab parse failed")

    try:
        if os.path.isdir("/etc/cron.d"):
            for name in sorted(os.listdir("/etc/cron.d")):
                _parse_system_crontab(os.path.join("/etc/cron.d", name), resources)
    except Exception:
        logger.exception("cron_src: /etc/cron.d parse failed")

    try:
        _parse_periodic_dirs(resources)
    except Exception:
        logger.exception("cron_src: periodic dirs parse failed")

    try:
        _parse_user_crontabs(resources)
    except Exception:
        logger.exception("cron_src: user crontabs parse failed")

    return resources
