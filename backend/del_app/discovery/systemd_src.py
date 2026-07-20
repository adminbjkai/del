"""Systemd discovery source: services, timers, and custom (non-vendor) unit
detail via `systemctl show`. Read-only: only `list-units`, `list-unit-files`,
`list-timers`, and `show` are called — nothing starts/stops/enables/disables a
unit. Env var VALUES are stripped from `Environment=`; only names are kept.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import subprocess

from del_app.models import Resource

logger = logging.getLogger("del_app.discovery.systemd_src")

TIMEOUT = 30
CUSTOM_UNIT_PREFIXES = ("/etc/systemd/system", "/home/")
BATCH_SIZE = 50


def _run(args: list[str]) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, check=False)
        if proc.returncode not in (0, 1):
            # systemctl list-* frequently exits 1 with partial output on some units; still usable.
            logger.warning("systemctl command exit %d: %s", proc.returncode, args)
        return proc.stdout
    except Exception:
        logger.exception("systemctl command errored: %s", args)
        return ""


def _env_names_from_line(env_line: str) -> list[str]:
    """Environment=KEY1=val1 KEY2=val2 -> ["KEY1", "KEY2"]. Tolerant of quoting."""
    names = []
    for token in re.findall(r'(\S+)=(?:"[^"]*"|\S*)', env_line):
        names.append(token)
    return names


def _list_services() -> list[dict]:
    raw = _run(
        ["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--plain", "--no-legend"]
    )
    out = []
    for line in raw.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        unit, load, active, sub = parts[0], parts[1], parts[2], parts[3]
        desc = parts[4] if len(parts) > 4 else ""
        out.append({"unit": unit, "load": load, "active": active, "sub": sub, "description": desc})
    return out


def _list_unit_files() -> dict[str, str]:
    raw = _run(["systemctl", "list-unit-files", "--no-pager", "--plain", "--no-legend"])
    out = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        out[parts[0]] = parts[1]
    return out


def _list_timers() -> list[dict]:
    raw = _run(["systemctl", "list-timers", "--all", "--no-pager", "--plain"])
    out = []
    lines = raw.splitlines()
    for line in lines[1:]:  # skip header
        line = line.strip()
        if not line or line.startswith("NEXT") or "timers listed" in line:
            continue
        # columns: NEXT LEFT LAST PASSED UNIT ACTIVATES  (whitespace-separated, timestamps have spaces)
        m = re.search(r"\s(\S+\.timer)\s+(\S+)\s*$", line)
        if not m:
            continue
        out.append({"timer": m.group(1), "activates": m.group(2)})
    return out


def _batched(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _show_units(unit_names: list[str]) -> dict[str, dict]:
    props = ["Id", "FragmentPath", "Description", "WorkingDirectory", "ExecStart",
             "Environment", "EnvironmentFiles", "ActiveState", "SubState", "UnitFileState"]
    result: dict[str, dict] = {}
    for batch in _batched(unit_names, BATCH_SIZE):
        if not batch:
            continue
        args = ["systemctl", "show", *batch, "--no-pager"]
        for p in props:
            args.extend(["-p", p])
        raw = _run(args)
        # Delimit records by the Id= property rather than blank lines: a
        # property value can contain blank lines, which would merge adjacent
        # units' blocks and drop one (its Id lost to the merged neighbour).
        current: dict[str, str] = {}
        for line in raw.splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k == "Id" and current.get("Id"):
                result[current["Id"]] = current
                current = {}
            current[k] = v
        if current.get("Id"):
            result[current["Id"]] = current
    return result


def collect() -> list[Resource]:
    """Collect systemd services (with detail for custom/non-vendor units) and
    timers. Read-only."""
    resources: list[Resource] = []

    try:
        services = _list_services()
    except Exception:
        logger.exception("systemd_src: list_services failed")
        services = []

    try:
        unit_file_states = _list_unit_files()
    except Exception:
        logger.exception("systemd_src: list_unit_files failed")
        unit_file_states = {}

    unit_names = [s["unit"] for s in services]
    try:
        details = _show_units(unit_names) if unit_names else {}
    except Exception:
        logger.exception("systemd_src: show_units failed")
        details = {}

    for svc in services:
        unit = svc["unit"]
        detail = details.get(unit, {})
        fragment_path = detail.get("FragmentPath", "")
        is_custom = fragment_path.startswith(CUSTOM_UNIT_PREFIXES)

        exec_start_raw = detail.get("ExecStart", "")
        exec_start_m = re.search(r"argv\[\]=([^;]+);", exec_start_raw)
        exec_start = exec_start_m.group(1).strip() if exec_start_m else None

        env_files_raw = detail.get("EnvironmentFiles", "")
        env_files = [tok.split(" ")[0] for tok in env_files_raw.split(";") if tok.strip()]

        data = {
            "load": svc["load"],
            "active": svc["active"],
            "sub": svc["sub"],
            "description": svc["description"] or detail.get("Description"),
            "unit_file_state": unit_file_states.get(unit, detail.get("UnitFileState")),
            "fragment_path": fragment_path or None,
            "is_custom": is_custom,
            "working_directory": detail.get("WorkingDirectory") or None,
            "exec_start": exec_start,
            "environment_files": env_files,
            "environment_var_names": _env_names_from_line(detail.get("Environment", "")),
        }

        resources.append(
            Resource(
                type="systemd_unit",
                key=unit,
                display=unit,
                path=fragment_path or None,
                state=svc["active"],
                data=data,
            )
        )

    # Also capture CUSTOM unit files that `list-units` omits because they are
    # disabled / never-loaded (e.g. an inactive htmls-webapp.service). These are
    # real app units on disk and must be visible so their app can be correlated.
    try:
        seen = {s["unit"] for s in services}
        extra_files: list[str] = []
        for d in ("/etc/systemd/system", "/usr/local/lib/systemd/system"):
            for path in glob.glob(os.path.join(d, "*.service")) + glob.glob(os.path.join(d, "*.timer")):
                name = os.path.basename(path)
                if name not in seen and not os.path.islink(path):
                    extra_files.append(name)
        # Show extras individually: batched `systemctl show` intermittently
        # drops records for certain inactive/unloaded units; the extras set is
        # small (disabled custom units only), so per-unit calls are cheap+robust.
        extra_details = {}
        for name in extra_files:
            extra_details.update(_show_units([name]))
        for unit in extra_files:
            detail = extra_details.get(unit, {})
            fragment_path = detail.get("FragmentPath", "")
            if not fragment_path.startswith(CUSTOM_UNIT_PREFIXES):
                continue
            if unit.endswith(".timer"):
                resources.append(Resource(type="systemd_timer", key=unit, display=unit,
                                          path=fragment_path or None,
                                          state=detail.get("ActiveState") or "inactive",
                                          data={"activates": unit[:-6] + ".service",
                                                "is_custom": True, "unit_file_state": detail.get("UnitFileState")}))
                continue
            exec_m = re.search(r"argv\[\]=([^;]+);", detail.get("ExecStart", ""))
            resources.append(Resource(
                type="systemd_unit", key=unit, display=unit, path=fragment_path or None,
                state=detail.get("ActiveState") or "inactive",
                data={
                    "load": detail.get("LoadState"), "active": detail.get("ActiveState"),
                    "sub": detail.get("SubState"),
                    "description": detail.get("Description"),
                    "unit_file_state": unit_file_states.get(unit, detail.get("UnitFileState")),
                    "fragment_path": fragment_path or None, "is_custom": True,
                    "working_directory": detail.get("WorkingDirectory") or None,
                    "exec_start": exec_m.group(1).strip() if exec_m else None,
                    "environment_files": [t.split(" ")[0] for t in detail.get("EnvironmentFiles", "").split(";") if t.strip()],
                    "environment_var_names": _env_names_from_line(detail.get("Environment", "")),
                }))
    except Exception:
        logger.exception("systemd_src: extra custom-unit-file scan failed")

    try:
        for t in _list_timers():
            resources.append(
                Resource(
                    type="systemd_timer",
                    key=t["timer"],
                    display=t["timer"],
                    path=None,
                    state="active",
                    data={"activates": t["activates"]},
                )
            )
    except Exception:
        logger.exception("systemd_src: timer resource build failed")

    return resources
