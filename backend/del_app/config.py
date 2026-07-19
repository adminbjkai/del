"""Settings loader for DEL, backed by /apps/del/config/del.toml."""
from __future__ import annotations

import os
from functools import lru_cache

import tomli
import pydantic


DEFAULT_CONFIG_PATH = "/apps/del/config/del.toml"


class Settings(pydantic.BaseModel):
    port: int
    db_path: str
    manifests_dir: str
    backups_dir: str
    logs_dir: str
    helper_socket: str = "/run/del/helper.sock"
    session_hours: int = 12
    scan_roots: list[str]
    protected_apps: list[str] = ["del"]


@lru_cache
def get_settings() -> Settings:
    config_path = os.environ.get("DEL_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    with open(config_path, "rb") as f:
        data = tomli.load(f)
    return Settings(**data)
