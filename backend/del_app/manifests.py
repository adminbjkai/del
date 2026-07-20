"""YAML manifests: per-app operator-authored overrides/augmentations that
correlate.py treats as authoritative (level=manual/confirmed). Stored as one
YAML file per app slug under settings.manifests_dir.
"""
from __future__ import annotations

import logging
import os

import pydantic
import yaml

from del_app.config import get_settings

logger = logging.getLogger("del_app.manifests")


class Manifest(pydantic.BaseModel):
    id: str
    name: str | None = None
    status: str | None = None
    domains: list[str] = pydantic.Field(default_factory=list)
    compose: list[str] = pydantic.Field(default_factory=list)
    repositories: list[str] = pydantic.Field(default_factory=list)
    host_paths: list[str] = pydantic.Field(default_factory=list)
    systemd_units: list[str] = pydantic.Field(default_factory=list)
    nginx: list[str] = pydantic.Field(default_factory=list)
    cron: list[str] = pydantic.Field(default_factory=list)
    notes: str | None = None
    shared: list[str] = pydantic.Field(default_factory=list)
    excluded: list[str] = pydantic.Field(default_factory=list)


def _manifests_dir() -> str:
    return get_settings().manifests_dir


def _path_for(slug: str) -> str:
    return os.path.join(_manifests_dir(), f"{slug}.yaml")


def load_all() -> dict[str, Manifest]:
    """Load every manifest under manifests_dir, keyed by app slug. Tolerant of
    a missing directory or individual malformed files (log + skip)."""
    result: dict[str, Manifest] = {}
    manifests_dir = _manifests_dir()
    if not os.path.isdir(manifests_dir):
        return result

    for name in sorted(os.listdir(manifests_dir)):
        if not name.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(manifests_dir, name)
        try:
            with open(path, "r", errors="replace") as f:
                data = yaml.safe_load(f)
            if not data:
                continue
            manifest = Manifest(**data)
            result[manifest.id] = manifest
        except Exception:
            logger.exception("manifests: failed to load %s", path)
            continue
    return result


def save(m: Manifest) -> None:
    """Write a manifest to manifests_dir/<slug>.yaml, creating the directory
    if needed."""
    manifests_dir = _manifests_dir()
    os.makedirs(manifests_dir, exist_ok=True)
    path = _path_for(m.id)
    with open(path, "w") as f:
        yaml.safe_dump(m.model_dump(exclude_none=True), f, sort_keys=False)


