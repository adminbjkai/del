"""Settings loader for DEL, backed by /apps/del/config/del.toml."""
from __future__ import annotations

import os
from functools import lru_cache

import tomli
import pydantic


DEFAULT_CONFIG_PATH = "/apps/del/config/del.toml"


class AssistantSettings(pydantic.BaseModel):
    """`[assistant]` table: the read-only advisory chat (docs/ASSISTANT.md).
    Every key has a default so installs without the table keep working."""

    enabled: bool = True
    base_url: str = "https://ollama.com"
    model: str = "glm-5.3-flash"
    api_key_file: str = "/apps/del/config/ollama-api-key.txt"
    timeout_seconds: int = 120
    temperature: float = 0.2
    # "low" | "high" | True — never False (see docs/ASSISTANT.md, Provider).
    think: str | bool = "low"
    context_budget_chars: int = 48000
    history_messages: int = 12


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
    assistant: AssistantSettings = AssistantSettings()


@lru_cache
def get_settings() -> Settings:
    config_path = os.environ.get("DEL_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    with open(config_path, "rb") as f:
        data = tomli.load(f)
    return Settings(**data)
