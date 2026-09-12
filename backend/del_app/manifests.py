"""YAML manifests: per-app operator-authored overrides/augmentations that
correlate.py treats as authoritative (level=manual/confirmed). Stored as one
YAML file per app slug under settings.manifests_dir.
"""
from __future__ import annotations

import logging
import os
import re

import pydantic
import yaml

from del_app.config import get_settings

logger = logging.getLogger("del_app.manifests")

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _valid_slug(value: str) -> str:
    """Accept a filename-safe application identifier, never a path."""
    if not _SLUG_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError("id must contain only letters, numbers, dots, dashes, or underscores")
    return value


class Manifest(pydantic.BaseModel):
    id: str = pydantic.Field(min_length=1, max_length=128)
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

    @pydantic.field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _valid_slug(value)


def _manifests_dir() -> str:
    return get_settings().manifests_dir


def _path_for(slug: str) -> str:
    slug = _valid_slug(slug)
    root = os.path.realpath(_manifests_dir())
    path = os.path.realpath(os.path.join(root, f"{slug}.yaml"))
    if os.path.commonpath((root, path)) != root:  # defensive if validation changes
        raise ValueError("manifest path escapes manifests_dir")
    return path


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

