"""Compose-file discovery: walks settings.scan_roots for docker-compose files and
parses them (tolerant of YAML errors, missing anchors, etc.) into compose_project
Resources. This complements docker_src (which only sees currently-running
containers) by also surfacing compose projects that are stopped or whose
containers were removed, plus the bind-mount sources declared in the file.

Read-only: only opens and parses files, never writes or executes anything.
"""
from __future__ import annotations

import logging
import os

import yaml

from del_app.config import get_settings
from del_app.models import Resource

logger = logging.getLogger("del_app.discovery.compose_src")

COMPOSE_FILENAMES = {
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
    "compose.override.yml",
    "compose.override.yaml",
}

PRUNE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "vendor",
    ".cache", "dist", "build",
}

MAX_DEPTH = 6


def _find_compose_files(root: str) -> list[str]:
    found: list[str] = []
    if not os.path.isdir(root):
        return found
    root_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        depth = dirpath.rstrip("/").count("/") - root_depth
        if depth >= MAX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn in COMPOSE_FILENAMES:
                found.append(os.path.join(dirpath, fn))
    return found


def _declared_images(services: dict) -> list[str]:
    images = []
    if not isinstance(services, dict):
        return images
    for svc in services.values():
        if isinstance(svc, dict) and svc.get("image"):
            images.append(str(svc["image"]))
    return images


def _bind_mount_sources(services: dict) -> list[str]:
    sources = []
    if not isinstance(services, dict):
        return sources
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        for vol in svc.get("volumes", []) or []:
            if isinstance(vol, str) and ":" in vol:
                parts = vol.split(":")
                src = parts[0]
                if src.startswith("/") or src.startswith("./") or src.startswith("../"):
                    sources.append(src)
            elif isinstance(vol, dict) and vol.get("type") == "bind":
                src = vol.get("source")
                if src:
                    sources.append(src)
    return sources


def _parse_compose_file(path: str) -> dict | None:
    try:
        with open(path, "r", errors="replace") as f:
            data = yaml.safe_load(f)
    except Exception:
        logger.warning("compose_src: failed to parse %s", path)
        return None
    if not isinstance(data, dict):
        return None
    return data


def collect() -> list[Resource]:
    """Scan scan_roots for compose files, group by project directory, produce
    one compose_project Resource per project directory found."""
    resources: list[Resource] = []
    try:
        settings = get_settings()
        scan_roots = settings.scan_roots
    except Exception:
        logger.exception("compose_src: could not load settings, aborting")
        return resources

    projects: dict[str, dict] = {}

    for root in scan_roots:
        try:
            files = _find_compose_files(root)
        except Exception:
            logger.exception("compose_src: walk failed for root %s", root)
            continue

        for path in files:
            working_dir = os.path.dirname(path)
            proj = projects.setdefault(
                working_dir,
                {
                    "working_dir": working_dir,
                    "config_files": [],
                    "config_files_exist": {},
                    "services": set(),
                    "volumes": set(),
                    "networks": set(),
                    "bind_mount_sources": set(),
                    "declared_name": None,
                    "images": set(),
                },
            )
            proj["config_files"].append(path)
            proj["config_files_exist"][path] = os.path.isfile(path)

            data = _parse_compose_file(path)
            if not data:
                continue
            if data.get("name"):
                proj["declared_name"] = data.get("name")
            services = data.get("services") or {}
            if isinstance(services, dict):
                proj["services"].update(services.keys())
                proj["bind_mount_sources"].update(_bind_mount_sources(services))
                proj["images"].update(_declared_images(services))
            volumes = data.get("volumes") or {}
            if isinstance(volumes, dict):
                proj["volumes"].update(volumes.keys())
            networks = data.get("networks") or {}
            if isinstance(networks, dict):
                proj["networks"].update(networks.keys())

    for working_dir, proj in projects.items():
        name = proj["declared_name"] or os.path.basename(working_dir)
        resources.append(
            Resource(
                type="compose_project",
                key=working_dir,
                display=name,
                path=working_dir,
                state="found",
                data={
                    "declared_name": proj["declared_name"],
                    "working_dir": working_dir,
                    "config_files": sorted(proj["config_files"]),
                    "config_files_exist": proj["config_files_exist"],
                    "services": sorted(proj["services"]),
                    "volumes": sorted(proj["volumes"]),
                    "networks": sorted(proj["networks"]),
                    "bind_mount_sources": sorted(proj["bind_mount_sources"]),
                    "images": sorted(proj["images"]),
                },
            )
        )

    return resources
