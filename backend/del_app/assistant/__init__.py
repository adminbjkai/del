"""DEL Assistant: read-only advisory chat over the inventory (docs/ASSISTANT.md).
The web layer imports only this package."""
from __future__ import annotations

from del_app.assistant.context import RESOURCE_TYPE_TARGETS, SCOPES, list_targets
from del_app.assistant.errors import AssistantError
from del_app.assistant.prompts import PROMPT_LIBRARY, prompt_library
from del_app.assistant.service import ask, status, test_connection

__all__ = [
    "AssistantError",
    "PROMPT_LIBRARY",
    "RESOURCE_TYPE_TARGETS",
    "SCOPES",
    "ask",
    "list_targets",
    "prompt_library",
    "status",
    "test_connection",
]
