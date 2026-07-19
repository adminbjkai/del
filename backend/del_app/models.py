"""Typed pydantic models shared across DEL lanes."""
from __future__ import annotations

from typing import Literal

import pydantic

ConfidenceLevel = Literal["confirmed", "high", "probable", "possible", "unrelated", "manual"]

ResourceType = Literal[
    "container", "image", "volume", "network", "compose_project", "nginx_site",
    "systemd_unit", "systemd_timer", "cron_entry", "process", "port", "directory",
    "git_repo", "env_file", "tmux_session", "bind_mount",
]


class Resource(pydantic.BaseModel):
    type: str
    key: str
    display: str
    path: str | None = None
    state: str
    data: dict = pydantic.Field(default_factory=dict)


class Evidence(pydantic.BaseModel):
    source: str
    statement: str
    weight: int


class Association(pydantic.BaseModel):
    resource_key: str
    resource_type: str
    confidence: int
    level: ConfidenceLevel
    ownership: str
    shared: bool = False
    data_loss_risk: Literal["none", "config", "data"]
    removal_eligible: Literal["safe", "uncertain", "blocked"]
    recommended_action: str
    evidence: list[Evidence] = pydantic.Field(default_factory=list)
    excluded: bool = False
    approved: bool = False


class AppRecord(pydantic.BaseModel):
    slug: str
    name: str
    status: str
    kind: str
    protected: bool = False
    domains: list[str] = pydantic.Field(default_factory=list)
    ports: list[int] = pydantic.Field(default_factory=list)


class PlanStep(pydantic.BaseModel):
    seq: int
    stage: str
    operation: str
    args: dict
    description: str
    reversible: bool
    danger: Literal["safe", "warning", "data_loss"]


class Plan(pydantic.BaseModel):
    id: int | None = None
    app_slug: str
    options: dict = pydantic.Field(default_factory=dict)
    steps: list[PlanStep] = pydantic.Field(default_factory=list)
    warnings: list[str] = pydantic.Field(default_factory=list)
    preserved: list[str] = pydantic.Field(default_factory=list)
    manual_followup: list[str] = pydantic.Field(default_factory=list)
    est_reclaim_bytes: int = 0
