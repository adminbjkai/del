"""ask() orchestration: validate → context → messages → stream → persist →
audit; status(); one-in-flight concurrency guard (docs/ASSISTANT.md
"Service"). Nothing here imports helper_client, planner or jobs."""
from __future__ import annotations

import threading
from typing import Iterator

from del_app import auditlog
from del_app.assistant import provider, store
from del_app.assistant.context import SCOPES, build_context
from del_app.assistant.errors import AssistantError
from del_app.assistant.prompts import SYSTEM_PROMPT
from del_app.config import get_settings
from del_app.db import get_db

# Ollama Cloud's free tier allows one concurrent request; a second ask gets
# 429 immediately instead of queueing behind a streamed response.
_INFLIGHT = threading.Semaphore(1)

_MESSAGE_MAX_CHARS = 8000


def status() -> dict:
    """{"enabled", "configured", "model", "key_source", "reason"} — never the key."""
    cfg = get_settings().assistant
    out = {
        "enabled": bool(cfg.enabled),
        "configured": False,
        "model": cfg.model,
        "key_source": None,
        "reason": None,
    }
    if not cfg.enabled:
        out["reason"] = "disabled in config ([assistant] enabled = false)"
        return out
    try:
        key, source = provider.resolve_api_key(cfg.api_key_file)
    except AssistantError as exc:
        out["reason"] = exc.message
        return out
    if not key:
        out["reason"] = (
            f"no API key: set DEL_OLLAMA_API_KEY or create {cfg.api_key_file} (mode 0600)"
        )
        return out
    out["configured"] = True
    out["key_source"] = source
    return out


def _client() -> provider.OllamaCloudClient:
    cfg = get_settings().assistant
    key, _source = provider.resolve_api_key(cfg.api_key_file)
    if not key:
        raise AssistantError("disabled", "assistant is not configured", 503)
    return provider.OllamaCloudClient(cfg.base_url, key, cfg.model, cfg.timeout_seconds)


def test_connection() -> dict:
    """Settings "Test connection": provider.ping() → {ok, model, latency_ms}."""
    st = status()
    if not (st["enabled"] and st["configured"]):
        raise AssistantError("disabled", st["reason"] or "assistant disabled", 503)
    return _client().ping()


def ask(
    *,
    user_id: int | None,
    scope: str,
    target: str | None,
    message: str,
    conversation_id: int | None = None,
    prompt_id: str | None = None,
) -> Iterator[dict]:
    """Generator of NDJSON events: meta → delta* → done | error. Validation
    errors (AssistantError) are raised on the first next() so the route can
    answer with plain JSON before streaming starts."""
    st = status()
    if not (st["enabled"] and st["configured"]):
        raise AssistantError("disabled", st["reason"] or "assistant disabled", 503)
    if scope not in SCOPES:
        raise AssistantError("scope", f"unknown scope: {scope}", 400)
    message = (message or "").strip()
    if not message:
        raise AssistantError("message", "message is empty", 400)
    if len(message) > _MESSAGE_MAX_CHARS:
        raise AssistantError("message", f"message longer than {_MESSAGE_MAX_CHARS} chars", 400)
    if scope in ("general", "orphans"):
        target = None

    cfg = get_settings().assistant
    conn = get_db()
    try:
        bundle = build_context(conn, scope, target, cfg.context_budget_chars)
    finally:
        conn.close()

    if not _INFLIGHT.acquire(blocking=False):
        raise AssistantError("busy", "another assistant request is in progress", 429)
    try:
        conn = get_db()
        try:
            if conversation_id is not None:
                conv = store.get_conversation(conn, conversation_id, user_id)
                if conv is None:
                    raise AssistantError("target", "no such conversation", 404)
                history = store.recent_messages(conn, conversation_id, cfg.history_messages)
            else:
                conversation_id = store.create_conversation(
                    conn, user_id, scope, bundle.target, message
                )
                history = []
            user_message_id = store.add_message(
                conn, conversation_id, "user", message, prompt_id=prompt_id
            )
        finally:
            conn.close()

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "system",
                "content": (
                    f"### Inventory context (scope={scope}, target={bundle.target or '-'})\n"
                    f"{bundle.text}"
                ),
            },
            *history,
            {"role": "user", "content": message},
        ]

        yield {
            "type": "meta",
            "conversation_id": conversation_id,
            "message_id": user_message_id,
            "scope": scope,
            "target": bundle.target,
            "context_truncated": bundle.truncated,
        }

        parts: list[str] = []
        usage: dict = {}
        error: AssistantError | None = None
        try:
            client = _client()
            for chunk in client.chat_stream(
                messages, think=cfg.think, temperature=cfg.temperature
            ):
                if chunk["content"]:
                    parts.append(chunk["content"])
                    yield {"type": "delta", "text": chunk["content"]}
                if chunk["done"]:
                    usage = dict(chunk.get("usage") or {})
                    break
        except AssistantError as exc:
            error = exc
        finally:
            text = "".join(parts)
            if error is not None:
                usage["error"] = error.kind
            conn = get_db()
            try:
                store.add_message(
                    conn, conversation_id, "assistant", text,
                    usage=usage, context_truncated=bundle.truncated,
                )
            finally:
                conn.close()
            auditlog.audit(
                user_id,
                "assistant.ask",
                f"{scope}:{bundle.target or '-'}",
                {
                    "conversation_id": conversation_id,
                    "prompt_id": prompt_id,
                    "chars_in": len(message),
                    "chars_out": len(text),
                    "context_truncated": bundle.truncated,
                },
            )
        if error is not None:
            yield {"type": "error", "kind": error.kind, "message": error.message}
            return
        yield {"type": "done", "usage": usage}
    finally:
        _INFLIGHT.release()
