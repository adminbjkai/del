"""Assistant: the read-only advisory chat page plus its JSON / NDJSON-stream
endpoints. See docs/ASSISTANT.md for the contract.

Only `del_app.assistant` is imported here (lazily, so this package still
boots — and the page degrades to its "disabled" panel — before that lane
lands). Tests monkeypatch `del_app.web.assistant.assistant` with a fake.
"""
from __future__ import annotations

import json
from typing import Any, Iterator
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Depends, Form, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from del_app import auth
from del_app.auth import User
from del_app.db import get_db
from del_app.web.queries import RESOURCE_TYPE_LABELS
from del_app.web.render import _csrf_response, _render, _require_csrf

try:
    from del_app import assistant
except ImportError:  # pragma: no cover - exercised until lane A lands
    assistant = None  # type: ignore[assignment]

router = APIRouter()

# Human labels for the scope chips; the ids themselves come from
# `assistant.SCOPES` so the two cannot drift apart.
_SCOPE_LABELS = {
    "general": "General",
    "app": "Application",
    "orphans": "Orphans",
    "resource_type": "Resource type",
    "resource": "Resource",
}
_DEFAULT_SCOPES = list(_SCOPE_LABELS)
_DEFAULT_RESOURCE_TYPES = ["container", "image", "network", "volume"]

_UNAVAILABLE = {
    "enabled": False,
    "configured": False,
    "model": None,
    "key_source": None,
    "reason": "Assistant unavailable",
}


def _error(message: str, kind: str, status: int, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": message, "kind": kind}, status_code=status, headers=headers)


def _unavailable() -> JSONResponse:
    return _error("Assistant unavailable", "disabled", 503)


def _from_exc(exc: Exception) -> JSONResponse:
    message = getattr(exc, "message", None) or str(exc) or "Assistant error"
    kind = getattr(exc, "kind", None) or "error"
    status = int(getattr(exc, "status", None) or 500)
    headers = None
    retry = getattr(exc, "retry_after", None)
    if retry is not None:
        headers = {"Retry-After": str(retry)}
    return _error(str(message)[:300], str(kind), status, headers)


def current_status() -> dict:
    """`assistant.status()` guarded so a missing/broken lane never breaks a
    page render. Never contains the API key (status() does not expose it)."""
    if assistant is None:
        return dict(_UNAVAILABLE)
    try:
        st = dict(assistant.status())
    except Exception as exc:  # pragma: no cover - defensive
        st = dict(_UNAVAILABLE)
        st["reason"] = str(exc)[:300]
    st.pop("api_key", None)
    return st


def is_enabled() -> bool:
    st = current_status()
    return bool(st.get("enabled") and st.get("configured"))


def _scopes() -> list[str]:
    raw = getattr(assistant, "SCOPES", None) if assistant is not None else None
    return list(raw) if raw else list(_DEFAULT_SCOPES)


def _resource_types() -> list[str]:
    raw = getattr(assistant, "RESOURCE_TYPE_TARGETS", None) if assistant is not None else None
    return list(raw) if raw else list(_DEFAULT_RESOURCE_TYPES)


def _store():
    return getattr(assistant, "store", None) if assistant is not None else None


def _type_of(scope: str, target: str | None) -> str | None:
    if not target:
        return None
    if scope == "resource_type":
        return target
    if scope == "resource":
        return target.split(":", 1)[0]
    return None


def _prompts(scope: str, target: str | None) -> list[dict]:
    if assistant is None:
        return []
    try:
        items = assistant.prompt_library(scope)
    except Exception:
        return []
    rtype = _type_of(scope, target)
    out = []
    for p in items:
        p = dict(p)
        if rtype:
            for k in ("label", "text", "description"):
                if isinstance(p.get(k), str):
                    p[k] = p[k].replace("<type>", rtype)
        out.append(p)
    return out


def _targets(scope: str, resource_type: str | None = None) -> list[dict]:
    if assistant is None:
        return []
    if scope == "resource_type":
        return [
            {"value": t, "label": RESOURCE_TYPE_LABELS.get(t, t)} for t in _resource_types()
        ]
    if scope not in ("app", "resource"):
        return []
    conn = get_db()
    try:
        rows = assistant.list_targets(conn, scope, resource_type=resource_type)
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _conversations(user_id: int) -> list[dict]:
    store = _store()
    if store is None:
        return []
    conn = get_db()
    try:
        return [dict(c) for c in store.list_conversations(conn, user_id)]
    finally:
        conn.close()


def _conversation(conv_id: int, user_id: int) -> dict | None:
    store = _store()
    if store is None:
        return None
    conn = get_db()
    try:
        conv = store.get_conversation(conn, conv_id, user_id)
    finally:
        conn.close()
    return dict(conv) if conv else None


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@router.get("/assistant", response_class=HTMLResponse)
def assistant_page(
    request: Request, response: Response, user: User = Depends(auth.require_user)
) -> HTMLResponse:
    status = current_status()
    enabled = bool(status.get("enabled") and status.get("configured"))
    scopes = _scopes()
    scope = request.query_params.get("scope") or "general"
    if scope not in scopes:
        scope = "general"
    target = request.query_params.get("target") or ""
    conversation = None
    conv_param = request.query_params.get("conversation")
    if conv_param and conv_param.isdigit():
        conversation = _conversation(int(conv_param), user.id)
        if conversation is not None:
            scope = conversation.get("scope") or scope
            target = conversation.get("target") or ""
    resource_type = _type_of(scope, target) if scope == "resource" else None
    targets = {
        "app": _targets("app") if enabled else [],
        "resource_type": _targets("resource_type") if enabled else [],
        "resource": _targets("resource", resource_type) if enabled and resource_type else [],
    }
    return _render(
        "assistant.html",
        request,
        response,
        status=status,
        assistant_on=enabled,
        scopes=[{"id": s, "label": _SCOPE_LABELS.get(s, s)} for s in scopes],
        scope=scope,
        target=target,
        resource_type=resource_type or "",
        targets=targets,
        prompts=_prompts(scope, target) if enabled else [],
        conversations=_conversations(user.id) if enabled else [],
        conversation=conversation,
        user=user,
    )


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

@router.get("/assistant/status")
def assistant_status(user: User = Depends(auth.require_user)) -> JSONResponse:
    return JSONResponse(current_status())


@router.get("/assistant/prompts")
def assistant_prompts(
    user: User = Depends(auth.require_user), scope: str = "general", target: str = ""
) -> JSONResponse:
    if assistant is None:
        return _unavailable()
    if scope not in _scopes():
        return _error(f"unknown scope: {scope}", "scope", 400)
    return JSONResponse({"prompts": _prompts(scope, target or None)})


@router.get("/assistant/targets")
def assistant_targets(
    user: User = Depends(auth.require_user), scope: str = "app", type: str = ""
) -> JSONResponse:
    if assistant is None:
        return _unavailable()
    if scope not in _scopes():
        return _error(f"unknown scope: {scope}", "scope", 400)
    if scope == "resource":
        if not type:
            return _error("type is required for resource targets", "target", 400)
        if type not in _resource_types():
            return _error(f"unknown resource type: {type}", "target", 400)
    try:
        targets = _targets(scope, type or None)
    except Exception as exc:
        if assistant is not None and isinstance(exc, getattr(assistant, "AssistantError", ())):
            return _from_exc(exc)
        raise
    return JSONResponse({"targets": targets})


@router.get("/assistant/conversations")
def assistant_conversations(user: User = Depends(auth.require_user)) -> JSONResponse:
    if assistant is None:
        return _unavailable()
    return JSONResponse({"conversations": _conversations(user.id)})


@router.get("/assistant/conversations/{conv_id}")
def assistant_conversation(
    conv_id: int, user: User = Depends(auth.require_user)
) -> JSONResponse:
    if assistant is None:
        return _unavailable()
    conv = _conversation(conv_id, user.id)
    if conv is None:
        return _error("conversation not found", "not_found", 404)
    return JSONResponse(conv)


@router.post("/assistant/conversations/{conv_id}/delete")
def assistant_conversation_delete(
    conv_id: int,
    request: Request,
    user: User = Depends(auth.require_user),
    csrf_token: str = Form(""),
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    store = _store()
    if store is None:
        return RedirectResponse(url="/assistant?error=Assistant+unavailable", status_code=303)
    conn = get_db()
    try:
        deleted = store.delete_conversation(conn, conv_id, user.id)
    finally:
        conn.close()
    if not deleted:
        return RedirectResponse(url="/assistant?error=Conversation+not+found", status_code=303)
    return RedirectResponse(url="/assistant?flash=Conversation+deleted", status_code=303)


@router.post("/assistant/test")
def assistant_test(
    request: Request,
    user: User = Depends(auth.require_user),
    csrf_token: str = Form(""),
) -> Response:
    if not _require_csrf(request, csrf_token):
        return _csrf_response()
    if assistant is None:
        return RedirectResponse(url="/settings?error=Assistant+unavailable", status_code=303)
    try:
        result = dict(assistant.test_connection())
    except Exception as exc:
        message = getattr(exc, "message", None) or str(exc) or "connection failed"
        msg = "Assistant test failed: " + str(message)[:200]
        return RedirectResponse(
            url="/settings?" + _qs("error", msg), status_code=303
        )
    if not result.get("ok", True):
        msg = "Assistant test failed: " + str(result.get("error") or "no reply")[:200]
        return RedirectResponse(url="/settings?" + _qs("error", msg), status_code=303)
    msg = "Assistant OK: {} responded in {} ms".format(
        result.get("model") or "model", result.get("latency_ms", "?")
    )
    return RedirectResponse(url="/settings?" + _qs("flash", msg), status_code=303)


def _qs(key: str, value: str) -> str:
    return urlencode({key: value})


# ---------------------------------------------------------------------------
# Streaming ask
# ---------------------------------------------------------------------------

def _ndjson(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


@router.post("/assistant/ask")
def assistant_ask(
    request: Request,
    user: User = Depends(auth.require_user),
    payload: dict[str, Any] = Body(default_factory=dict),
) -> Response:
    """Plain `def`: FastAPI runs it (and the sync generator below) in its
    threadpool, which is what the blocking urllib provider needs."""
    header = request.headers.get("X-CSRF-Token", "")
    if not auth.check_csrf(request, header):
        return _error("invalid csrf token", "csrf", 403)
    if assistant is None:
        return _unavailable()

    scope = str(payload.get("scope") or "general")
    target = payload.get("target") or None
    message = str(payload.get("message") or "").strip()
    prompt_id = payload.get("prompt_id") or None
    conversation_id = payload.get("conversation_id")
    if conversation_id in ("", None):
        conversation_id = None
    else:
        try:
            conversation_id = int(conversation_id)
        except (TypeError, ValueError):
            return _error("conversation_id must be an integer", "conversation", 400)
    if scope not in _scopes():
        return _error(f"unknown scope: {scope}", "scope", 400)
    if not message:
        return _error("message is required", "message", 400)
    if len(message) > 8000:
        return _error("message too long (max 8000 chars)", "message", 400)

    # Pull the first event (meta) here so validation errors (400/404/429/503)
    # surface as a JSON response before any streaming has begun.
    try:
        events = iter(assistant.ask(
            user_id=user.id,
            scope=scope,
            target=target,
            message=message,
            conversation_id=conversation_id,
            prompt_id=prompt_id,
        ))
        first = next(events)
    except assistant.AssistantError as exc:
        return _from_exc(exc)
    except StopIteration:
        return _error("assistant produced no response", "protocol", 502)

    def stream() -> Iterator[bytes]:
        yield _ndjson(first)
        try:
            for event in events:
                yield _ndjson(event)
        except assistant.AssistantError as exc:
            yield _ndjson({
                "type": "error",
                "kind": str(getattr(exc, "kind", "error")),
                "message": str(getattr(exc, "message", None) or exc)[:300],
            })
        except Exception as exc:  # never leak a traceback into the stream
            yield _ndjson({"type": "error", "kind": "upstream", "message": str(exc)[:300]})

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )
