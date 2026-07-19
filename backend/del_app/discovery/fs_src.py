"""Filesystem discovery source: top-level project directories under
settings.scan_roots. Read-only: stats/reads files only, never writes/deletes.
Only `.env` VAR NAMES are recorded, never values or file contents beyond what's
needed to list variable names and detect a compose file / git repo.
"""
from __future__ import annotations

import logging
import os
import subprocess

from del_app.config import get_settings
from del_app.models import Resource

logger = logging.getLogger("del_app.discovery.fs_src")

DU_TIMEOUT = 15
GIT_TIMEOUT = 5
COMPOSE_FILENAMES = {
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
}


def _du_size_kb(path: str) -> int | None:
    try:
        proc = subprocess.run(
            ["du", "-s", "--one-file-system", "-x", path],
            capture_output=True, text=True, timeout=DU_TIMEOUT, check=False,
        )
        if proc.returncode != 0:
            return None
        return int(proc.stdout.split()[0])
    except Exception:
        logger.warning("fs_src: du failed/timed out for %s", path)
        return None


def _env_var_names(path: str) -> tuple[bool, list[str], int]:
    env_path = os.path.join(path, ".env")
    if not os.path.isfile(env_path):
        return False, [], 0
    names = []
    try:
        with open(env_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name = line.split("=", 1)[0].strip()
                if name:
                    names.append(name)
    except Exception:
        logger.exception("fs_src: failed to read %s", env_path)
    return True, names, len(names)


def _git_info(path: str) -> dict | None:
    git_dir = os.path.join(path, ".git")
    if not os.path.exists(git_dir):
        return None
    info: dict = {"present": True}
    try:
        branch = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False,
        )
        info["branch"] = branch.stdout.strip() if branch.returncode == 0 else None
    except Exception:
        info["branch"] = None

    try:
        status = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False,
        )
        info["dirty"] = bool(status.stdout.strip()) if status.returncode == 0 else None
    except Exception:
        info["dirty"] = None

    return info


def _top_level_dirs(root: str) -> list[str]:
    try:
        return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    except Exception:
        logger.exception("fs_src: failed to list %s", root)
        return []


def collect() -> list[Resource]:
    """Collect top-level project directories under scan_roots: size (fast du
    estimate), compose-file presence, .env var NAMES (never values), and git
    repo info (branch, dirty). Read-only."""
    resources: list[Resource] = []

    try:
        settings = get_settings()
        scan_roots = settings.scan_roots
    except Exception:
        logger.exception("fs_src: could not load settings, aborting")
        return resources

    seen_paths: set[str] = set()

    for root in scan_roots:
        if not os.path.isdir(root):
            continue
        for name in _top_level_dirs(root):
            path = os.path.join(root, name)
            if path in seen_paths:
                continue
            seen_paths.add(path)

            try:
                size_kb = _du_size_kb(path)
                has_env, env_var_names, env_var_count = _env_var_names(path)
                git_info = _git_info(path)
                compose_files = []
                try:
                    for fn in os.listdir(path):
                        if fn in COMPOSE_FILENAMES:
                            compose_files.append(os.path.join(path, fn))
                except PermissionError:
                    logger.warning("fs_src: no permission to list %s, skipping", path)
                except Exception:
                    logger.exception("fs_src: failed to list top-level files of %s", path)

                data = {
                    "size_kb": size_kb,
                    "has_compose": bool(compose_files),
                    "compose_files": compose_files,
                    "has_env": has_env,
                    "env_var_count": env_var_count,
                    "env_var_names": env_var_names,
                    "git": git_info,
                }

                resources.append(
                    Resource(
                        type="directory",
                        key=path,
                        display=name,
                        path=path,
                        state="found",
                        data=data,
                    )
                )

                if git_info is not None:
                    resources.append(
                        Resource(
                            type="git_repo",
                            key=f"{path}/.git",
                            display=name,
                            path=path,
                            state="dirty" if git_info.get("dirty") else "clean",
                            data=git_info,
                        )
                    )

                if has_env:
                    resources.append(
                        Resource(
                            type="env_file",
                            key=os.path.join(path, ".env"),
                            display=f"{name}/.env",
                            path=os.path.join(path, ".env"),
                            state="found",
                            data={"var_names": env_var_names, "var_count": env_var_count},
                        )
                    )
            except Exception:
                logger.exception("fs_src: failed to process %s", path)
                continue

    return resources
