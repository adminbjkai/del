# DEL — Assistant (architecture and contract)

The Assistant is a **read-only advisory chat** built into DEL. It answers questions
about the current inventory — applications, orphan candidates, and Docker images,
containers, networks and volumes — using a hosted language model
(`glm-5.3-flash` on Ollama Cloud). It is deliberately unable to change anything:
it has no tools, no helper access, and no route that mutates state. Its job is to
help a human decide; the decision and the removal still go through DEL's planner.

This document is the design contract. `docs/ARCHITECTURE.md`, `SECURITY.md`,
`OPERATIONS.md`, `README.md` and the Fern page `fern/pages/guides/assistant.mdx`
must agree with it; when the code changes, this file changes first.

## Goals and non-goals

Goals
- Ask free-form questions about the inventory, scoped to: the whole server, one
  application, the orphan review list, one resource type (images, containers,
  networks, volumes), or one specific resource.
- Offer a library of predefined prompts per scope (one click to ask), e.g. "Is this
  image safe to remove, and is it used by more than one app?".
- Ground every answer in data DEL already holds (associations, confidence, shared
  flags, evidence), truncated to a fixed budget so cost and latency stay bounded.
- Persist conversations so an operator can return to a review thread.
- Degrade cleanly: when no API key is configured the UI explains how to enable it
  and the API answers `503`.

Non-goals
- No agentic tools, no actions, no writes to the inventory, no helper calls.
- No multi-user tenancy beyond DEL's single-admin model (conversations are keyed by
  user id anyway).
- No local model hosting; the only provider is Ollama Cloud.

## Module layout

```
backend/del_app/
  config.py                  Settings.assistant: AssistantSettings (new nested model)
  assistant/                 NEW package — everything provider/prompt/context related
    __init__.py              re-exports AssistantError, ask(), status(), prompt_library()
    errors.py                AssistantError(kind, message, status) and subclasses
    provider.py              OllamaCloudClient — urllib, NDJSON streaming, think="low"
    context.py               scope-aware context builders (read-only SQL) + budgeting
    prompts.py               SYSTEM_PROMPT and PROMPT_LIBRARY (predefined prompts)
    store.py                 conversations/messages persistence (tables below)
    service.py               ask() orchestration: validate → context → messages →
                             stream → persist → audit; status(); concurrency guard
  migrations/003_assistant.sql
  web/
    assistant.py             NEW router: /assistant page + JSON/stream endpoints
    routes.py                aggregator gains `assistant`
    static_routes.py         serves assistant.js and assistant.css (unauthenticated,
                             same as the other static assets)
    templates/assistant.html NEW page
    templates/base.html      Help|Ask right rail; csrf-token meta; loads assistant.js on every authenticated page
    static/assistant.js      DEL.assistant module (page + right-rail dock)
    static/assistant.css     assistant-only styles (tokens from app.css)
tests/
  test_assistant.py          provider parsing, context builders, prompt library, store,
                             service (provider faked)
  test_assistant_web.py      routes: auth, csrf, 503 when disabled, prompts, stream
                             shape, conversations, deep-link preselection, settings panel
```

Layering: `assistant/context.py` may import the query helpers in
`web/queries.py` and `classify_orphan_candidate` from `web/orphans.py` (they are
DEL's read-model). `web/assistant.py` imports only `del_app.assistant`. Nothing in
`assistant/` imports `helper_client`, `planner` or `jobs`.

## Configuration

`config/del.toml` gains an optional table. Every key has a default so existing
installs and the test fixture keep working without it.

```toml
[assistant]
enabled = true                     # master switch; false hides the feature entirely
base_url = "https://ollama.com"    # Ollama Cloud
model = "glm-5.3-flash"
api_key_file = "/apps/del/config/ollama-api-key.txt"   # 0600, one line
timeout_seconds = 120              # whole streamed response
temperature = 0.2
think = "low"                      # "low" | "high" | true — never false (see Provider)
context_budget_chars = 48000       # hard cap on the inventory context block
history_messages = 12              # prior turns replayed to the model
```

Pydantic model `AssistantSettings` in `config.py`; `Settings.assistant:
AssistantSettings = AssistantSettings()`.

API key resolution order (`assistant/provider.py: resolve_api_key()`):
1. environment variable `DEL_OLLAMA_API_KEY` (for ad-hoc runs and tests);
2. the file at `api_key_file`, stripped; the file must not be group/world readable
   (mode check like `auth.get_secret_key`), otherwise it is refused with a clear
   error;
3. none → the feature reports `configured: false`.

The key is never written to the database, the audit log, journald, or a template.

## Provider (`assistant/provider.py`)

- `OllamaCloudClient(base_url, api_key, model, timeout_seconds)`.
- `chat_stream(messages: list[dict], *, think, temperature) -> Iterator[Chunk]`
  where `Chunk = {"content": str, "thinking": str, "done": bool, "usage": dict | None}`.
- Transport: `urllib.request` (the allowed-dependency list in `docs/INTERFACES.md`
  excludes HTTP client packages). `POST {base_url}/api/chat` with
  `Authorization: Bearer <key>`, body
  `{"model", "messages", "stream": true, "think", "options": {"temperature"}}`.
  The response is NDJSON; each line is parsed and yielded. `thinking` deltas are
  yielded but the service does not forward them to the browser.
- **Never send `"think": false`.** Verified 2026-09-10: for this model Ollama Cloud
  then leaks the reasoning trace into `content` (sometimes with a bare `</think>`
  separator, sometimes without). `"low"` keeps the trace short and separate.
- Errors map to `AssistantError`: 401 → `auth` (503 to the browser, "API key
  rejected"), 404 model → `model` (503), 429 → `busy` (429, honour `Retry-After`),
  timeout / URLError → `upstream` (502), malformed line → `protocol` (502).
- `ping()` performs a tiny non-streaming request (`"Reply with OK"`, `think:"low"`)
  and returns `{ok, model, latency_ms}`; used by the Settings "Test connection"
  button.
- Tests patch `del_app.assistant.provider.urllib.request.urlopen`, matching the
  existing `gallery.py` convention.

## Scopes, targets and context builders (`assistant/context.py`)

| scope | target | context block contains |
|---|---|---|
| `general` | — | last completed scan; counts; **shared / multi-owner resources**; **protected apps**; **stale candidates** (stopped/unknown/no domain+port); **removal-risk ranking** of every current app (`shared_assocs`, `data_risk`, warnings, resources, domains, ports); then ownership indexes (volumes/images/networks/containers). Prompt-answer sections come first so a budget cut cannot drop shared/stale/risk. |
| `app` | app slug | application row, manifest presence, domains/ports, then every association: resource type/key/display/path/state, confidence + level, ownership, shared, data_loss_risk, removal_eligible, recommended_action, up to 3 evidence statements |
| `orphans` | — | the orphan candidate list exactly as `/orphans` computes it: type, key, display, path, state, size where known, classification bucket/label/reason; counts per bucket |
| `resource_type` | `container` \| `image` \| `network` \| `volume` | every resource of that type in the latest scan with its owners (slug list), `shared`, state, size/dangling/containers_using/driver as available, plus whether it appears in the orphan list |
| `resource` | `<type>:<key>` | the single resource row with full `data` (minus noise), its owner apps with each association's confidence/level/ownership/shared/data_loss_risk, evidence, and whether it is an orphan candidate; for images also the containers using it and the compose projects declaring it |

Every builder returns `ContextBundle(scope, target, title, facts: dict, text: str,
truncated: bool)`. `text` is the compact, deterministic rendering sent to the model
(one fact per line, `key: value`, resources as bullet lines). `render_budgeted()`
cuts at `context_budget_chars` on a line boundary and appends
`[context truncated: N more lines omitted]` so the model knows. Ordering inside a
scope is stable: shared/high-risk items first, then by display name.

Unknown scope → `AssistantError("scope", 400)`; unknown target → `AssistantError("target", 404)`.

Query rules: every query is scoped with `latest_done_scan_id` exactly like the UI
(`AND last_seen = ?`); associations exclude `excluded = 1`; ownership uses the same
`_owner_map` helper as the Resources page so the assistant and the UI never
disagree about who owns a volume.

## Prompts (`assistant/prompts.py`)

`SYSTEM_PROMPT` establishes: DEL's vocabulary (confidence levels, ownership, shared,
data_loss_risk, removal_eligible, orphan buckets), the read-only role, and hard
rules:
1. Answer only from the supplied context; if something is not in it, say so.
2. Never call a resource "safe to delete" when `shared` is true or it has more than
   one owner; call that out explicitly and name the owners.
3. For anything with `data_loss_risk: data`, say a backup is required first.
4. Point the operator to the DEL page that performs the action (`/apps/<slug>`,
   `/orphans`, `/resources/<type>`) instead of giving shell commands, unless asked.
5. Be concise; use short bullet lists; quote resource keys exactly.
6. `/orphans` is review-only.
7. Cannot change the host; send change requests to the matching DEL page.
8. Shared / Stale / Risk / Protected sections are complete when present — do not claim those fields are missing.

`PROMPT_LIBRARY: list[Prompt]` where
`Prompt = {id, scope, label, text, description}`; `prompt_library(scope) ->
list[Prompt]`. Initial set (ids are stable API):

- general: `general.overview` "Summarise this server's inventory", `general.risky`
  "Which apps look most expensive or risky to remove?", `general.stale` "Which
  apps look unused or stale?", `general.shared` "What is shared between apps?",
  `general.owners` "Map every volume/image/network/container to its owners",
  `general.protected` "Which apps are protected and why that matters"
- app: `app.explain` "Explain what this app consists of", `app.remove_impact`
  "What would removing this app affect?", `app.data` "What data would be lost and
  what should be backed up?", `app.shared` "Which of its resources are shared with
  other apps?", `app.confidence` "Which associations are weak (possible / uncertain eligibility)?",
  `app.trace` "Trace this app to every shared resource and other app"
- orphans: `orphans.review` "Review the orphan list and group it", `orphans.safe`
  "Which orphans look lowest-risk to investigate next?", `orphans.suspicious` "Which orphans
  might belong to an app DEL missed?", `orphans.reclaim` "Rank by disk reclaimable"
- resource_type: `rtype.review` "Review all <type>s and flag anything shared",
  `rtype.unused` "Which <type>s are unused or dangling?", `rtype.multi_owner` "Which
  <type>s are tied to more than one app?"
- resource: `res.safe` "Is it safe to delete this? Is it tied to multiple apps?",
  `res.owners` "Who uses this and how confident is DEL?", `res.contents` "What does
  this contain and would deleting it lose data?"

`<type>` in labels is filled from the target at request time.

## Persistence (`migrations/003_assistant.sql`, `assistant/store.py`)

```sql
CREATE TABLE assistant_conversations (
  id INTEGER PRIMARY KEY,
  user_id INTEGER,
  scope TEXT NOT NULL,
  target TEXT,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE assistant_messages (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES assistant_conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL,              -- 'user' | 'assistant'
  content TEXT NOT NULL,
  prompt_id TEXT,                  -- library id when the turn came from a card
  usage_json TEXT,                 -- {"prompt_eval_count","eval_count","ms"} on assistant rows
  context_truncated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_assistant_messages_conv ON assistant_messages(conversation_id, id);
```

Store API: `create_conversation(conn, user_id, scope, target, title) -> id`,
`add_message(conn, conversation_id, role, content, prompt_id=None, usage=None,
context_truncated=False) -> id`, `list_conversations(conn, user_id, limit=30)`,
`get_conversation(conn, id, user_id)` (with messages), `delete_conversation(conn, id,
user_id)`, `recent_messages(conn, id, limit)`. Title = first 80 chars of the first
user message. The context block itself is **not** stored (it is rebuilt from the live
DB on every turn so answers reflect the latest scan).

## Service (`assistant/service.py`)

```python
def status() -> dict   # {"enabled", "configured", "model", "key_source": "env"|"file"|None, "reason"}
def ask(*, user_id, scope, target, message, conversation_id=None, prompt_id=None) -> Iterator[Event]
```

`ask()`:
1. `status()` must be enabled+configured, else `AssistantError("disabled", 503)`.
2. Validate scope/target; build the context bundle (`AssistantError` on failure).
3. Acquire the process-wide `threading.Semaphore(1)` non-blocking — Ollama Cloud's
   free tier allows one concurrent request; a second ask returns
   `AssistantError("busy", 429)` immediately instead of queueing.
4. Load or create the conversation (must belong to `user_id`), persist the user
   message.
5. Assemble messages: system prompt; a second system message containing the
   context block (`### Inventory context (scope=…, target=…)`); the last
   `history_messages` turns; the new user message.
6. Stream: yield `{"type":"meta","conversation_id","message_id","scope","target",
   "context_truncated"}` first, then `{"type":"delta","text"}` per content chunk,
   then `{"type":"done","usage":{...}}`. On provider error mid-stream yield
   `{"type":"error","kind","message"}` and stop.
7. Persist the assistant message (even if partial, flagged in `usage_json.error`),
   bump `updated_at`, release the semaphore, and write
   `auditlog.audit(user_id, "assistant.ask", f"{scope}:{target or '-'}",
   {"conversation_id", "prompt_id", "chars_in", "chars_out", "context_truncated"})`.
   Message text is **not** audited.

## HTTP interface (`web/assistant.py`)

All behind `auth.require_user`. JSON errors are `{"error": "...", "kind": "..."}`.

| Method & path | Purpose | Notes |
|---|---|---|
| `GET /assistant` | Page | query `scope`, `target`, `conversation` preselect; renders `assistant.html` with `status`, `scopes`, `targets` (apps + resource types), `prompts` for the preselected scope, recent conversations |
| `GET /assistant/status` | JSON `status()` | never includes the key |
| `GET /assistant/prompts?scope=&target=` | JSON `{"prompts":[…]}` | labels have `<type>` substituted |
| `GET /assistant/targets?scope=` | JSON `{"targets":[{"value","label"}]}` | apps for `app`; the four docker types for `resource_type`; for `resource` accepts `type=` and lists that type's resources |
| `POST /assistant/ask` | Stream NDJSON (`application/x-ndjson`) | JSON body `{scope, target, message, conversation_id?, prompt_id?}`; CSRF via header `X-CSRF-Token` (checked with `auth.check_csrf`, the same HMAC as form posts); 400/404/429/503 as JSON before streaming starts |
| `GET /assistant/conversations` | JSON list | current user only |
| `GET /assistant/conversations/{id}` | JSON conversation + messages | 404 if not owner |
| `POST /assistant/conversations/{id}/delete` | Delete | form post with `csrf_token` (matches existing form convention), redirects to `/assistant` |
| `POST /assistant/test` | Settings "Test connection" | form post; calls `provider.ping()`; redirects to `/settings?flash=`/`?error=` |

Streaming route is a plain `def` returning `StreamingResponse(generator,
media_type="application/x-ndjson")`; FastAPI runs the synchronous generator in its
threadpool, which is correct for the blocking urllib client under
`--workers 1`. Header `X-Accel-Buffering: no` is set so Nginx does not buffer the
stream; `Cache-Control: no-store`.

The new JSON-POST + `X-CSRF-Token` convention is documented in `SECURITY.md`
(Sessions, CSRF, rate limiting) as the second accepted CSRF carrier.

## UI

- Global **Help|Ask** right rail on every authenticated page (`base.html`):
  Help is the glossary; Ask embeds `_assistant_dock.html` except on `/assistant`
  itself (full page there). Page-scoped defaults come from the current route
  (app, orphans, resource type/row). Mobile **Ask** FAB opens the rail.
- Sidebar nav entry "Assistant" (after Orphans), palette page entry, glossary
  context `assistant`.
- `/assistant` layout: left column (scope chips: General / Application / Orphans /
  Resource type / Resource; target select shown for the scoped modes, resource
  mode has a type select then a searchable resource select; prompt cards for the
  scope; recent conversations list). Main column: transcript (user/assistant
  bubbles, assistant text rendered by a small safe Markdown subset — headings,
  bullets, numbered lists, bold, inline and fenced code; everything escaped first,
  links only for same-origin `/…` paths), status line (model, context truncated
  badge), and a composer (textarea, Ctrl/Cmd+Enter sends, Stop button aborts the
  fetch via `AbortController`).
- Disabled state: page renders a `box-info` panel with the exact enablement steps
  (key file path, mode `0600`, restart command) and no composer.
- Deep links: "Ask the assistant" buttons on `/apps/<slug>` (page actions),
  `/orphans` (page actions), `/resources/<type>` (page actions → resource_type) and
  per-row for docker types (→ `resource`, `target=<type>:<key>`).
- Settings page: "Assistant" panel showing enabled/configured/model/key source and
  the Test connection form.
- No inline scripts or handlers (nginx CSP `script-src 'self'`). `assistant.js`
  registers `window.DEL.assistant = { init }` and self-initialises on
  `#assistant-page` or `#assistant-dock`. CSRF for JSON posts is the `csrf-token`
  meta in `base.html`. Read-only: no helper, no planner, no inventory writes.
  Key file `/apps/del/config/ollama-api-key.txt` is mode `0600` (refused if wider).

## Security and privacy

- Inventory metadata (names, paths, image tags, volume names, ports, evidence
  strings) is sent to Ollama Cloud for every ask. No file contents, env values,
  secrets or job output are ever included; `context.py` strips keys matching
  `jobs._SECRET_RE`-style patterns from resource `data` before rendering. This
  outbound flow is stated in `SECURITY.md` (Threat model) and the Fern page.
- The assistant cannot act. Removal still requires the plan/confirm flow.
- Key file 0600, refused if wider. Key never logged; error messages from the
  provider are truncated to 300 chars and stripped of `Authorization` values before
  surfacing.
- One in-flight request per process; 429 otherwise.
- Conversations are per user id; cross-user access is 404.

## Operations

- Enable: `install -m 600 /dev/stdin /apps/del/config/ollama-api-key.txt <<< "<key>"`,
  ensure `[assistant] enabled = true` (default), `systemctl restart del-web`,
  then Settings → Assistant → Test connection.
- Disable: `enabled = false` or remove the key file; restart.
- Cost: Ollama Cloud bills per token (see ollama.com/pricing); the context budget
  bounds input size per ask.

## Acceptance criteria (frozen)

AC1 Config defaults keep the existing test fixture green; `status()` reports
`configured: false` without a key and the page/API degrade as described.
AC2 `POST /assistant/ask` streams `meta → delta* → done` NDJSON from a faked
provider; 403 without a valid `X-CSRF-Token`; 503 when disabled; 429 when busy.
AC3 `GET /assistant/prompts` returns the library filtered by scope with `<type>`
substituted; every library id is unique and every scope has ≥ 3 prompts.
AC4 Context builders: all five scopes produce text ≤ budget, `truncated` set when
cut, docker resources include owners + shared, unknown target → 404.
AC5 No route under `/assistant` writes to `applications`, `resources`,
`associations`, `plans`, `jobs`, or calls `helper_client`.
AC6 Migration 003 applies; conversations list/get/delete are owner-scoped; audit
rows are written for asks without message text.
AC7 `/assistant` renders enabled and disabled states; deep-link buttons exist on
app detail, orphans, resources pages; base.html blocks added; no inline scripts.
AC8 Docs updated: ARCHITECTURE (components, data model, frontend namespace),
SECURITY (CSRF header, file permissions, never logged, threat model), OPERATIONS
(enable/disable/test), INSTALL (key file), README (feature + quick facts), fern
`guides/assistant.mdx` + `docs.yml` nav; all agree with the code.
AC9 Full pytest suite green; pyflakes clean on `del_app` and `tests`.
