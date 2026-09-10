"""Ollama Cloud client: stdlib urllib, NDJSON streaming, API key resolution.

No HTTP client packages (docs/INTERFACES.md allowed-dependency list). Tests
patch `del_app.assistant.provider.urllib.request.urlopen`, the same convention
as web/gallery.py.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
import urllib.error
import urllib.request
from typing import Iterator

from del_app.assistant.errors import AssistantError, ProviderError
from del_app.config import get_settings

API_KEY_ENV = "DEL_OLLAMA_API_KEY"

# Provider error text is surfaced to the browser: cap it and never let an
# Authorization value through, whatever the upstream echoed back.
_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?\S+")
_ERROR_MAX_CHARS = 300


def sanitize_error(text: str | None) -> str:
    text = _AUTH_RE.sub(r"\1***", str(text or "")).strip()
    return text[:_ERROR_MAX_CHARS]


def resolve_api_key(api_key_file: str | None = None) -> tuple[str | None, str | None]:
    """(key, source) where source is "env" | "file" | None.

    Order: DEL_OLLAMA_API_KEY, then `api_key_file` (stripped, must be 0600 —
    refused with AssistantError("key", 503) if group/world readable), else
    (None, None). The key is returned only to the caller; never logged."""
    env = os.environ.get(API_KEY_ENV, "").strip()
    if env:
        return env, "env"
    path = api_key_file or get_settings().assistant.api_key_file
    if not path or not os.path.exists(path):
        return None, None
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise AssistantError(
            "key",
            f"API key file {path} is group/world accessible (mode {mode:04o}); "
            "run: chmod 600 " + path,
            503,
        )
    with open(path, encoding="utf-8") as f:
        key = f.read().strip()
    if not key:
        return None, None
    return key, "file"


def normalize_think(value) -> str | bool:
    """Coerce the configured think setting; never returns False (this model
    then leaks its reasoning trace into `content`)."""
    if value is True:
        return True
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("low", "medium", "high"):
            return v
        if v == "true":
            return True
    return "low"


class OllamaCloudClient:
    """POST {base_url}/api/chat with Bearer auth; streams NDJSON chunks."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout_seconds: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def _request(self, body: dict) -> urllib.request.Request:
        return urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
                "User-Agent": "DEL-Assistant/1.0",
            },
            method="POST",
        )

    def _open(self, body: dict):
        """urlopen with the spec's error mapping applied."""
        try:
            return urllib.request.urlopen(self._request(body), timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            raise _map_http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                "upstream", f"Ollama Cloud unreachable: {sanitize_error(exc)}", 502
            ) from None

    def chat_stream(
        self, messages: list[dict], *, think, temperature: float
    ) -> Iterator[dict]:
        """Yield Chunk = {"content", "thinking", "done", "usage"} per NDJSON
        line. `thinking` text is yielded for completeness; the service does
        not forward it to the browser."""
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": normalize_think(think),
            "options": {"temperature": temperature},
        }
        started = time.monotonic()
        resp = self._open(body)
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    raise ProviderError(
                        "protocol", f"malformed stream line: {sanitize_error(line)}", 502
                    ) from None
                if not isinstance(obj, dict):
                    raise ProviderError("protocol", "malformed stream line: not an object", 502)
                if obj.get("error"):
                    raise ProviderError(
                        "upstream", f"Ollama Cloud error: {sanitize_error(obj['error'])}", 502
                    )
                msg = obj.get("message") or {}
                done = bool(obj.get("done"))
                usage = None
                if done:
                    total_ns = obj.get("total_duration")
                    usage = {
                        "prompt_eval_count": obj.get("prompt_eval_count"),
                        "eval_count": obj.get("eval_count"),
                        "ms": (
                            int(total_ns / 1_000_000)
                            if isinstance(total_ns, (int, float))
                            else int((time.monotonic() - started) * 1000)
                        ),
                    }
                yield {
                    "content": str(msg.get("content") or ""),
                    "thinking": str(msg.get("thinking") or ""),
                    "done": done,
                    "usage": usage,
                }
                if done:
                    return
        except (TimeoutError, OSError) as exc:
            raise ProviderError(
                "upstream", f"stream interrupted: {sanitize_error(exc)}", 502
            ) from None
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def ping(self) -> dict:
        """Tiny non-streaming request; {ok, model, latency_ms}. Raises
        AssistantError on failure (the Settings route turns it into a flash)."""
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": "Reply with OK"}],
            "stream": False,
            "think": "low",
            "options": {"temperature": 0},
        }
        started = time.monotonic()
        resp = self._open(body)
        try:
            payload = resp.read()
        except (TimeoutError, OSError) as exc:
            raise ProviderError("upstream", f"read failed: {sanitize_error(exc)}", 502) from None
        finally:
            try:
                resp.close()
            except Exception:
                pass
        try:
            obj = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            raise ProviderError("protocol", "non-JSON reply from Ollama Cloud", 502) from None
        if isinstance(obj, dict) and obj.get("error"):
            raise ProviderError(
                "upstream", f"Ollama Cloud error: {sanitize_error(obj['error'])}", 502
            )
        return {
            "ok": True,
            "model": (obj.get("model") if isinstance(obj, dict) else None) or self.model,
            "latency_ms": max(1, int((time.monotonic() - started) * 1000)),
        }


def _map_http_error(exc: urllib.error.HTTPError) -> ProviderError:
    try:
        detail = exc.read().decode("utf-8", "replace")
    except Exception:
        detail = ""
    try:
        parsed = json.loads(detail)
        if isinstance(parsed, dict) and parsed.get("error"):
            detail = str(parsed["error"])
    except ValueError:
        pass
    detail = sanitize_error(detail)
    code = int(exc.code)
    if code == 401:
        return ProviderError("auth", "API key rejected by Ollama Cloud", 503)
    if code == 404:
        return ProviderError("model", f"model not available: {detail or 'not found'}", 503)
    if code == 429:
        retry = None
        try:
            retry = int(str(exc.headers.get("Retry-After", "")).strip())
        except (TypeError, ValueError):
            retry = None
        return ProviderError(
            "busy", "Ollama Cloud rate limit reached; try again shortly", 429, retry_after=retry
        )
    return ProviderError("upstream", f"Ollama Cloud HTTP {code}: {detail or exc.reason}", 502)
