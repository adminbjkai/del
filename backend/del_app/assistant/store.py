"""Conversation/message persistence for the assistant (tables in
migrations/003_assistant.sql). Every function takes an open connection; all
reads and the delete are owner-scoped by user_id."""
from __future__ import annotations

import json

from del_app.db import q, x
from del_app.web.queries import _json_or, _rows

TITLE_MAX_CHARS = 80


def make_title(first_user_message: str) -> str:
    text = " ".join(str(first_user_message or "").split())
    return (text[:TITLE_MAX_CHARS] or "Conversation").strip()


def create_conversation(conn, user_id: int | None, scope: str, target: str | None, title: str) -> int:
    return x(
        conn,
        "INSERT INTO assistant_conversations (user_id, scope, target, title) VALUES (?,?,?,?)",
        (user_id, scope, target, make_title(title)),
    )


def add_message(
    conn,
    conversation_id: int,
    role: str,
    content: str,
    prompt_id: str | None = None,
    usage: dict | None = None,
    context_truncated: bool = False,
) -> int:
    mid = x(
        conn,
        "INSERT INTO assistant_messages "
        "(conversation_id, role, content, prompt_id, usage_json, context_truncated) "
        "VALUES (?,?,?,?,?,?)",
        (
            conversation_id,
            role,
            content,
            prompt_id,
            json.dumps(usage, default=str) if usage is not None else None,
            1 if context_truncated else 0,
        ),
    )
    x(
        conn,
        "UPDATE assistant_conversations SET updated_at = datetime('now') WHERE id = ?",
        (conversation_id,),
    )
    return mid


def list_conversations(conn, user_id: int | None, limit: int = 30) -> list[dict]:
    return _rows(
        q(
            conn,
            "SELECT c.*, (SELECT COUNT(*) FROM assistant_messages m "
            " WHERE m.conversation_id = c.id) AS message_count "
            "FROM assistant_conversations c WHERE c.user_id IS ? "
            "ORDER BY c.updated_at DESC, c.id DESC LIMIT ?",
            (user_id, int(limit)),
        )
    )


def get_conversation(conn, conversation_id: int, user_id: int | None) -> dict | None:
    """Conversation dict with `messages` (oldest first), or None when it does
    not exist or belongs to another user."""
    rows = _rows(
        q(
            conn,
            "SELECT * FROM assistant_conversations WHERE id = ? AND user_id IS ?",
            (conversation_id, user_id),
        )
    )
    if not rows:
        return None
    conv = rows[0]
    messages = _rows(
        q(
            conn,
            "SELECT * FROM assistant_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )
    )
    for m in messages:
        m["usage"] = _json_or(m.pop("usage_json", None), None)
        m["context_truncated"] = bool(m.get("context_truncated"))
    conv["messages"] = messages
    return conv


def delete_conversation(conn, conversation_id: int, user_id: int | None) -> bool:
    """True when a row owned by user_id was deleted. Messages go via the
    ON DELETE CASCADE (foreign_keys=ON in get_db)."""
    cur = conn.execute(
        "DELETE FROM assistant_conversations WHERE id = ? AND user_id IS ?",
        (conversation_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def recent_messages(conn, conversation_id: int, limit: int) -> list[dict]:
    """Last `limit` messages as [{"role","content"}], oldest first, for
    replay to the model."""
    if limit <= 0:
        return []
    rows = _rows(
        q(
            conn,
            "SELECT role, content FROM assistant_messages WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (conversation_id, int(limit)),
        )
    )
    rows.reverse()
    return [{"role": r["role"], "content": r["content"]} for r in rows]
