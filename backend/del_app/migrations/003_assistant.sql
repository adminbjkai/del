-- Assistant conversations and messages (docs/ASSISTANT.md "Persistence").
-- The inventory context block is never stored: it is rebuilt from the live
-- DB on every turn so answers reflect the latest scan.

CREATE TABLE IF NOT EXISTS assistant_conversations (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    scope TEXT NOT NULL,
    target TEXT,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assistant_messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES assistant_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,              -- 'user' | 'assistant'
    content TEXT NOT NULL,
    prompt_id TEXT,                  -- library id when the turn came from a card
    usage_json TEXT,                 -- {"prompt_eval_count","eval_count","ms"} on assistant rows
    context_truncated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_assistant_messages_conv ON assistant_messages(conversation_id, id);
