"""Assistant error type. `kind` is the machine-readable category surfaced in
JSON/NDJSON error payloads, `status` the HTTP status the web layer maps it to
(docs/ASSISTANT.md "Provider" and "Service")."""
from __future__ import annotations


class AssistantError(Exception):
    """kind: scope | target | disabled | busy | auth | model | upstream |
    protocol | key. `retry_after` (seconds) is set for provider 429s."""

    def __init__(
        self, kind: str, message: str, status: int = 500, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after = retry_after

    def to_dict(self) -> dict:
        return {"error": self.message, "kind": self.kind}


class ProviderError(AssistantError):
    """Raised by provider.py for anything Ollama Cloud / the transport did."""
