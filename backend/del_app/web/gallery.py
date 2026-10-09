"""App gallery (/view-apps, /app-icon/{domain}): live HTTPS-probed launcher
and the favicon proxy."""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from urllib.parse import unquote, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from del_app import auth
from del_app.auth import User
from del_app.config import get_settings
from del_app.db import get_db, q
from del_app.web.queries import _json_or, _latest_scan_id, _rows
from del_app.web.render import _render

logger = logging.getLogger(__name__)

router = APIRouter()

# A gallery request may need to verify dozens of Nginx endpoints. Keep those
# network checks off the request thread pool and cache the result briefly so
# normal navigation never turns into a thundering herd of HTTPS requests.
_APP_PROBE_CACHE: dict[str, dict[str, Any]] = {}
_APP_PROBE_LOCK = threading.Lock()
_PROBE_REFRESHING: set[str] = set()  # coalesces concurrent background refreshes
# The in-memory cache is seeded from the disk file exactly once per process
# (see `warm_probe_cache`), at startup, not lazily on the first request. A
# lazy load inside `_probe_domains` would make "empty memory cache" mean two
# different things and break the first-load blocking contract below.
_PROBE_DISK_LOADED = False
_APP_PROBE_TTL = 300
_APP_PROBE_TIMEOUT = 3.0


def _probe_disk_path() -> Path:
    return Path(get_settings().db_path).parent / "probe-cache.json"


def _load_probe_cache() -> None:
    """Seed the in-memory cache from the persisted file.

    Entries are restored as already stale (`cached_at` in the past) so the
    first gallery load serves them instantly and revalidates them in the
    background — never a cold, empty gallery after a restart.
    """
    p = _probe_disk_path()
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        now = time.monotonic()
        with _APP_PROBE_LOCK:
            for domain, entry in data.items():
                if domain not in _APP_PROBE_CACHE and isinstance(entry, dict) and "result" in entry:
                    _APP_PROBE_CACHE[domain] = {
                        "cached_at": now - _APP_PROBE_TTL - 1.0,
                        "result": entry["result"],
                    }
    except Exception:
        logger.warning("Could not load persisted probe cache", exc_info=True)


def warm_probe_cache() -> None:
    """Load the persisted probe cache once, at process startup.

    Called from the app lifespan; idempotent so a test or reload cannot double
    load. Kept separate from `_load_probe_cache` so tests can exercise either
    behaviour directly.
    """
    global _PROBE_DISK_LOADED
    if _PROBE_DISK_LOADED:
        return
    _PROBE_DISK_LOADED = True
    _load_probe_cache()


def _persist_probe_cache() -> None:
    p = _probe_disk_path()
    try:
        with _APP_PROBE_LOCK:
            payload = {
                d: {"result": v["result"]}
                for d, v in _APP_PROBE_CACHE.items()
                if "result" in v
            }
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        logger.warning("Could not persist probe cache", exc_info=True)

_CATEGORY_ORDER = [
    "Favorites",
    "AI & Automation",
    "Media & Streaming",
    "Notes & Knowledge",
    "Productivity",
    "Files & Data",
    "Developer Tools",
    "Infrastructure",
    "Security & Identity",
    "Business & Finance",
    "Utilities",
    "Other",
]

# Ordered: the FIRST category with a keyword hit wins, so put the more
# specific/greedy categories ahead of the generic ones. Keywords prefixed with
# "=" must match a whole word (see _gallery_category) — used for short or
# substring-prone tokens like "ai", "tv" and "vault", which otherwise pull in
# "streamvault-iptv", "nativetv", etc.
_CATEGORY_KEYWORDS = [
    ("Media & Streaming", ("flix", "media", "jelly", "stream", "iptv", "=tv", "cine",
                           "video", "immich", "photo", "ente", "plex", "emby", "music")),
    ("Security & Identity", ("auth", "=vault", "vaultwarden", "passbolt", "bitwarden",
                             "identity", "login", "sso", "keycloak")),
    ("Notes & Knowledge", ("note", "memo", "docmost", "karakeep", "bookmark", "wiki",
                           "papra", "knowledge", "hearth", "affine", "warden", "obsidian",
                           "outline", "docnow")),
    ("Business & Finance", ("expense", "wallos", "monize", "invoice", "finance", "nocodb",
                            "=crm", "twenty", "billing", "budget")),
    ("Files & Data", ("file", "share", "stash", "sheet", "database", "libredb", "byte",
                      "openbook", "drive", "upload")),
    ("AI & Automation", ("=ai", "ocr", "agent", "glean", "gongyu", "deep", "poco",
                         "claude", "glm", "llm", "gpt", "chat")),
    ("Productivity", ("kan", "board", "task", "plan", "focal", "calendar", "slash",
                      "banban", "lastboard", "=todo")),
    ("Infrastructure", ("portainer", "dock", "komodo", "netdata", "beszel", "monitor",
                        "speed", "hivedock", "appwrite", "cron", "schedule", "uptime",
                        "status", "dashboard", "homepage", "=home", "portracker")),
    ("Developer Tools", ("code", "dev", "git", "gist", "query", "api", "draw",
                         "excalidraw", "tldraw", "prettier", "json", "termix", "repo",
                         "deploy")),
    ("Utilities", ("convert", "pdf", "tools", "txt", "b64", "short", "calc", "simple",
                   "tiny", "qr", "paste")),
]


def _valid_gallery_domain(value: Any) -> str | None:
    """Return a normalized DNS hostname suitable for the app gallery.

    Nginx may contain wildcard/default server names or literal IPs. The gallery
    is intentionally for human-facing HTTPS domains, so those entries stay out.
    """
    host = str(value or "").strip().lower().rstrip(".")
    if not host or "*" in host or host == "_" or len(host) > 253:
        return None
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        return None
    labels = host.split(".")
    if len(labels) < 2:
        return None
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        return None
    return host


def _gallery_category(slug: str, name: str, domain: str) -> str:
    """Bucket a gallery app. First matching category in _CATEGORY_KEYWORDS wins.

    A keyword written as ``"=word"`` must match on word boundaries; a plain
    keyword matches anywhere. Only the first domain label is considered, and
    the public suffix is dropped, so every `*.bjk.ai` endpoint does not land
    in AI & Automation on the strength of the TLD.
    """
    # An app's cards stay together: its slug and name decide, and a domain
    # label (api.boxy..., status.x...) is only a fallback. A slug derived from
    # a domain ("tix-bjk-ai") drops that suffix so "-ai" is not a keyword hit.
    def _strip(text: str) -> str:
        return re.sub(r"[-_ .]bjk[-_ .]ai$", "", text.lower())

    category = _match_category(" ".join((_strip(slug), _strip(name))))
    if category == "Other" and domain:
        # A bundled icon already says what kind of app the domain is.
        category = _ICON_CATEGORY.get(_DOMAIN_STATIC_ICONS.get(domain, ""), "Other")
    if category == "Other" and domain:
        category = _match_category(domain.split(".", 1)[0].lower())
    return category


def _match_category(haystack: str) -> str:
    for category, keywords in _CATEGORY_KEYWORDS:
        for keyword in keywords:
            if keyword.startswith("="):
                word = keyword[1:]
                if re.search(rf"(?:^|[-_ ]){re.escape(word)}(?:$|[-_ ])", haystack):
                    return category
            elif keyword in haystack:
                return category
    return "Other"


def _probe_domain(domain: str) -> dict[str, Any]:
    """Verify an HTTPS endpoint without downloading its response body.

    Any HTTP response below 500 proves DNS, TLS and nginx routing; 5xx does
    not count. Root-level 404s count as live. A 401/403 may come from nginx's
    own basic auth, so view_apps also checks the upstream port listens.
    """
    started = time.monotonic()
    status: int | None = None
    error = ""
    request = urllib.request.Request(
        f"https://{domain}/",
        headers={"User-Agent": "DEL-App-Gallery/1.0", "Accept": "text/html,*/*;q=0.8"},
        method="GET",
    )
    final_url = ""
    try:
        with urllib.request.urlopen(request, timeout=_APP_PROBE_TIMEOUT) as response:
            status = int(response.getcode())
            final_url = response.geturl() or ""
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        final_url = exc.geturl() or ""
    except Exception as exc:
        error = type(exc).__name__
    latency_ms = max(1, round((time.monotonic() - started) * 1000))
    # urllib follows redirects; the host it ended on tells an alias
    # (c64.bjk.ai -> gd64.bjk.ai) apart from an app of its own.
    final_host = (urllib.parse.urlsplit(final_url).hostname or "").lower() if final_url else ""
    return {
        "healthy": status is not None and 100 <= status < 500,
        "redirects_to": final_host if final_host and final_host != domain else None,
        "status": status,
        "latency_ms": latency_ms,
        "error": error,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _run_probes(pending: list[str]) -> dict[str, dict[str, Any]]:
    """Probe a batch concurrently and write the results into the cache."""
    results: dict[str, dict[str, Any]] = {}
    if not pending:
        return results
    with ThreadPoolExecutor(max_workers=min(16, len(pending))) as pool:
        futures = {pool.submit(_probe_domain, domain): domain for domain in pending}
        for future in as_completed(futures):
            domain = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # defensive: one probe must not break the page
                result = {
                    "healthy": False,
                    "status": None,
                    "latency_ms": 0,
                    "error": type(exc).__name__,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
            results[domain] = result
            with _APP_PROBE_LOCK:
                _APP_PROBE_CACHE[domain] = {
                    "cached_at": time.monotonic(),
                    "result": dict(result),
                }
    _persist_probe_cache()
    return results


def _refresh_probes_async(pending: list[str]) -> None:
    """Refresh stale probes off the request thread, one batch at a time."""
    def _worker() -> None:
        try:
            _run_probes(pending)
        finally:
            with _APP_PROBE_LOCK:
                _PROBE_REFRESHING.clear()
    threading.Thread(target=_worker, daemon=True).start()


def _probe_domains(domains: list[str], *, force: bool = False) -> dict[str, dict[str, Any]]:
    """Health for the gallery's domains, stale-while-revalidate.

    Probing ~125 domains takes seconds even 16-way (each has a 3 s timeout),
    and with a 120 s TTL a browsing operator paid that cost every two minutes.
    So: serve whatever is cached immediately and refresh in the background,
    the same pattern `_reclaimable_bytes` already uses for `docker system df`.

    Only two cases block: an explicit `?refresh=1`, and the very first load
    after a restart, where there is nothing cached to show and an empty
    gallery would be worse than a slow one.
    """
    unique = sorted(set(domains))
    now = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    stale: list[str] = []
    uncached: list[str] = []

    with _APP_PROBE_LOCK:
        for domain in unique:
            cached = _APP_PROBE_CACHE.get(domain)
            if cached is None:
                uncached.append(domain)
                continue
            results[domain] = dict(cached["result"])
            if now - float(cached.get("cached_at", 0)) >= _APP_PROBE_TTL:
                stale.append(domain)

    if force:
        results.update(_run_probes(unique))
        return results

    # Nothing cached for these yet — block, or the gallery renders empty.
    if uncached:
        results.update(_run_probes(uncached))

    if stale:
        with _APP_PROBE_LOCK:
            already = bool(_PROBE_REFRESHING)
            if not already:
                _PROBE_REFRESHING.add("1")
        if not already:
            _refresh_probes_async(stale)

    return results


# Favicons are fetched server-side and re-served from DEL's own origin.
# Loading them straight from the third-party domain (<img src="https://x/favicon.ico">)
# makes the BROWSER perform the request, so any app sitting behind HTTP basic
# auth answers 401 + WWW-Authenticate and the browser opens a credential
# dialog on top of the gallery — several times over, for a page the operator
# is already authenticated to. Proxying means DEL absorbs the 401 and simply
# serves nothing, letting the card fall back to its initial letter.
#
# Two cache layers: memory for this process, and one small file per domain
# next to the database so a restart does not refetch every icon (each miss
# costs up to five outbound requests). In-flight lookups are shared per domain.
_ICON_CACHE: dict[str, dict[str, Any]] = {}
_ICON_LOCK = threading.Lock()
_ICON_INFLIGHT: dict[str, threading.Lock] = {}
_ICON_TTL = 7 * 86400          # a favicon changes about never
_ICON_NEGATIVE_TTL = 6 * 3600  # retry failures sooner than successes
_ICON_TIMEOUT = 4.0
_ICON_MAX_BYTES = 131072
_ICON_CACHE_MAX = 256
_ICON_ALLOWED_TYPES = (
    "image/x-icon", "image/vnd.microsoft.icon", "image/png", "image/gif",
    "image/jpeg", "image/svg+xml", "image/webp", "image/bmp",
)


_STATIC_ICONS_DIR = Path(__file__).resolve().parent / "static" / "icons"

_DOMAIN_STATIC_ICONS = {
    "openknowledge.bjk.ai": "openknowledge.svg",
    "turkflix.bjk.ai": "turkflix.png",
    "tldraw.bjk.ai": "tldraw.svg",
    "ironcalc.bjk.ai": "ironcalc.svg",
    "caprust.bjk.ai": "caprust.svg",
    "notecapai.bjk.ai": "notecapai.svg",
    "tix.bjk.ai": "tix.svg",
    "usg.bjk.ai": "usg.svg",
    "astv.bjk.ai": "astv.svg",
    "atv.bjk.ai": "atv.svg",
    "fileshare2.bjk.ai": "fileshare.svg",
    "fs2.bjk.ai": "fileshare.svg",
    "vshare.bjk.ai": "fileshare.svg",
    "b64pdf2.bjk.ai": "pdf64.svg",
    "pdf64.bjk.ai": "pdf64.svg",
    "cdx64.bjk.ai": "b64.svg",
    "d64.bjk.ai": "b64.svg",
    "gd64.bjk.ai": "b64.svg",
    "img2.bjk.ai": "img2.svg",
    "claw-audit.bjk.ai": "claw.svg",
    "ginstall.bjk.ai": "installer.svg",
    "agyinstall.bjk.ai": "installer.svg",
    "mitv.bjk.ai": "iptv.svg",
    "xtreampulsar.bjk.ai": "iptv.svg",
    "hls.bjk.ai": "iptv.svg",
    "lives.bjk.ai": "iptv.svg",
    "shows.bjk.ai": "iptv.svg",
    "17run.bjk.ai": "monitor.svg",
    "montr.bjk.ai": "monitor.svg",
    "mtxt.bjk.ai": "editor.svg",
    "txt.bjk.ai": "editor.svg",
    "htmls.bjk.ai": "editor.svg",
    "jsonp.bjk.ai": "json.svg",
    "tbl.bjk.ai": "table.svg",
    "n50.bjk.ai": "notion.svg",
    "vnce.bjk.ai": "vnc.svg",
}


# Bundled icons double as a category hint for names no keyword catches.
_ICON_CATEGORY = {
    "iptv.svg": "Media & Streaming",
    "fileshare.svg": "Files & Data",
    "monitor.svg": "Infrastructure",
    "vnc.svg": "Infrastructure",
    "installer.svg": "Developer Tools",
    "notion.svg": "Notes & Knowledge",
    "b64.svg": "Utilities",
    "pdf64.svg": "Utilities",
    "img2.svg": "Utilities",
    "editor.svg": "Utilities",
    "json.svg": "Utilities",
    "table.svg": "Utilities",
}


def _http_get(url: str, accept: str, limit: int) -> tuple[int, str, bytes] | None:
    """GET a URL; (status, content type, first `limit`+1 bytes) or None."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (DEL-App-Gallery/1.0)", "Accept": accept}, method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_ICON_TIMEOUT) as response:
            ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return int(response.getcode()), ctype, response.read(limit + 1)
    except Exception:
        return None


def _as_icon(body: bytes, ctype: str) -> tuple[bytes, str] | None:
    """Accept a response body as an icon if it is a small image."""
    if not body or len(body) > _ICON_MAX_BYTES or ctype == "text/html":
        return None
    if body[:4] == b"\x00\x00\x01\x00":
        sniffed = "image/x-icon"
    elif body[:4] == b"\x89PNG":
        sniffed = "image/png"
    elif body[:4] == b"GIF8":
        sniffed = "image/gif"
    elif b"<svg" in body[:250]:
        sniffed = "image/svg+xml"
    else:
        sniffed = None
    if not (ctype.startswith("image/") or sniffed):
        return None
    return body, ctype if ctype in _ICON_ALLOWED_TYPES else (sniffed or "image/x-icon")


def _fetch_icon(domain: str) -> tuple[bytes, str] | None:
    """Fetch one icon for a domain: (body, content_type) or None.

    1. A bundled icon for apps without a usable favicon (static/icons/).
    2. /favicon.ico, /favicon.png, /favicon.svg.
    3. <link rel="icon"> or apple-touch-icon in the root page.

    Never raises: a broken icon (a malformed data: URI, an unreadable bundled
    file) is a miss, cached like any other, not a 500 retried on every view.
    """
    try:
        return _find_icon(domain)
    except Exception:
        logger.debug("icon lookup failed for %s", domain, exc_info=True)
        return None


def _find_icon(domain: str) -> tuple[bytes, str] | None:
    static_name = _DOMAIN_STATIC_ICONS.get(domain)
    if static_name:
        static_file = _STATIC_ICONS_DIR / static_name
        if static_file.is_file():
            ctype = {".svg": "image/svg+xml", ".png": "image/png"}.get(static_file.suffix.lower(), "image/x-icon")
            return static_file.read_bytes(), ctype

    for path in ("/favicon.ico", "/favicon.png", "/favicon.svg"):
        got = _http_get(f"https://{domain}{path}", "image/*", _ICON_MAX_BYTES)
        if got and got[0] == 200:
            icon = _as_icon(got[2], got[1])
            if icon:
                return icon

    got = _http_get(f"https://{domain}/", "text/html,*/*;q=0.8", 131072)
    if not got or got[0] != 200:
        return None
    html = got[2].decode("utf-8", errors="ignore")
    for link in re.findall(r"<link[^>]+>", html, re.I):
        if not re.search(r"rel=[\"']?(?:shortcut )?(?:icon|apple-touch-icon)[\"']?", link, re.I):
            continue
        m = re.search(r"href=[\"']([^\"']+)[\"']", link, re.I)
        if not m:
            continue
        href = m.group(1).strip()
        if href.startswith("data:image/svg+xml"):
            if ";base64," in href:
                return base64.b64decode(href.split(";base64,", 1)[1]), "image/svg+xml"
            return unquote(href.split(",", 1)[1]).encode("utf-8"), "image/svg+xml"
        if href.startswith("data:image/png;base64,"):
            return base64.b64decode(href.split(";base64,", 1)[1]), "image/png"
        target = urljoin(f"https://{domain}/", href)
        # Only follow icon links on the same site over https: urllib would
        # otherwise also open file:// URLs or fetch from arbitrary hosts.
        parts = urllib.parse.urlsplit(target)
        if parts.scheme != "https" or (parts.hostname or "").lower() != domain:
            continue
        got = _http_get(target, "image/*", _ICON_MAX_BYTES)
        if got and got[0] == 200:
            icon = _as_icon(got[2], got[1])
            if icon:
                return icon
    return None


def _icon_disk_dir() -> Path:
    return Path(get_settings().db_path).parent / "icon-cache"


def _icon_disk_path(domain: str) -> Path:
    # Domains are validated hostnames (see app_icon), safe as file names.
    return _icon_disk_dir() / f"{domain}.icon"


def _disk_icon(domain: str) -> tuple[bool, tuple[bytes, str] | None, float]:
    """(found, value, age_seconds) from the on-disk cache. A file holds the
    content type, a newline, then the body; an empty file is a cached miss."""
    try:
        path = _icon_disk_path(domain)
        age = time.time() - path.stat().st_mtime
        raw = path.read_bytes()
    except OSError:
        return False, None, 0.0
    if not raw:
        return True, None, age
    ctype, _, body = raw.partition(b"\n")
    if not body:  # a torn write; fetch again
        return False, None, 0.0
    return True, (body, ctype.decode("ascii", "replace")), age


def _store_disk_icon(domain: str, value: tuple[bytes, str] | None) -> None:
    try:
        path = _icon_disk_path(domain)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(b"" if value is None else value[1].encode("ascii", "replace") + b"\n" + value[0])
        tmp.replace(path)
    except OSError:
        logger.debug("could not persist icon for %s", domain, exc_info=True)


def _remember_icon(domain: str, value: tuple[bytes, str] | None, ttl: float) -> None:
    with _ICON_LOCK:
        _ICON_CACHE.pop(domain, None)
        # Insertion order is age order: evict the oldest entries, not all.
        while len(_ICON_CACHE) >= _ICON_CACHE_MAX:
            del _ICON_CACHE[next(iter(_ICON_CACHE))]
        _ICON_CACHE[domain] = {"value": value, "expires": time.monotonic() + ttl}


def _cached_icon(domain: str) -> tuple[bytes, str] | None:
    with _ICON_LOCK:
        entry = _ICON_CACHE.get(domain)
        if entry and time.monotonic() < entry["expires"]:
            return entry["value"]
        inflight = _ICON_INFLIGHT.setdefault(domain, threading.Lock())

    with inflight:
        # Another request may have filled the cache while this one waited.
        with _ICON_LOCK:
            entry = _ICON_CACHE.get(domain)
            if entry and time.monotonic() < entry["expires"]:
                return entry["value"]
        found, value, age = _disk_icon(domain)
        ttl = _ICON_TTL if value else _ICON_NEGATIVE_TTL
        if not (found and age < ttl):
            value = _fetch_icon(domain)
            ttl = _ICON_TTL if value else _ICON_NEGATIVE_TTL
            age = 0.0
            _store_disk_icon(domain, value)
        _remember_icon(domain, value, ttl - age)
        return value


def _loopback_targets(upstreams: list[dict] | None) -> list[tuple[str, int]]:
    """The loopback (host, port) pairs a site's proxy_pass lines point at."""
    targets = set()
    for u in upstreams or []:
        parts = urllib.parse.urlsplit(str(u.get("proxy_pass") or ""))
        host = parts.hostname
        try:
            port = int(u.get("port") or parts.port or 0)
        except (TypeError, ValueError):
            port = 0
        if host in ("127.0.0.1", "localhost", "::1") and port:
            targets.add(("::1" if host == "::1" else "127.0.0.1", port))
    return sorted(targets)


def _any_listening(targets: list[tuple[str, int]]) -> bool:
    """Whether any target accepts a TCP connection.

    nginx answers basic-auth challenges (401) itself, before it proxies, so
    an HTTPS probe alone calls an app "online" while nothing serves it."""
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            continue
    return False


def _gallery_owner_score(app: dict, domain: str, confidence: Any) -> tuple[int, int, int]:
    """Rank competing correlations for a domain deterministically."""
    slug = str(app.get("slug") or "").lower()
    labels = domain.split(".")
    try:
        base = int(confidence or 0)
    except (TypeError, ValueError):
        base = 0
    exact = int(bool(slug and labels and labels[0] == slug))
    label_hit = int(bool(slug and slug in labels))
    return (exact, label_hit, base)


@router.get("/view-apps", response_class=HTMLResponse)
def view_apps(
    request: Request,
    response: Response,
    user: User = Depends(auth.require_user),
) -> HTMLResponse:
    """Homelab-style launcher for verified, currently serving web apps.

    Inventory remains authoritative: candidates must belong to an app and an
    enabled Nginx site in the latest completed scan. A short cached HTTPS probe
    then removes stale, broken, or otherwise unreachable endpoints.
    """
    conn = get_db(read_snapshot=True)
    try:
        latest = _latest_scan_id(conn)
        rows: list[dict] = []
        if latest is not None:
            rows = _rows(
                q(
                    conn,
                    """
                    SELECT ap.id AS app_id, ap.slug, ap.name, ap.status, ap.kind,
                           ap.protected, a.confidence, r.data_json
                    FROM applications ap
                    JOIN associations a ON a.app_id = ap.id AND a.excluded = 0
                    JOIN resources r ON r.id = a.resource_id
                    WHERE ap.last_seen = ? AND r.last_seen = ?
                      AND r.type = 'nginx_site'
                    """,
                    (latest, latest),
                )
            )
    finally:
        conn.close()

    candidates: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        data = _json_or(row.get("data_json"), {})
        if not data.get("enabled", False):
            continue
        for raw_domain in data.get("server_names", []) or []:
            domain = _valid_gallery_domain(raw_domain)
            if not domain:
                continue
            candidate = dict(row)
            candidate["owner_score"] = _gallery_owner_score(candidate, domain, row.get("confidence"))
            candidates.setdefault(domain, []).append(candidate)

    owners: dict[str, dict[str, Any]] = {}
    for domain, possible in candidates.items():
        owners[domain] = max(
            possible,
            key=lambda item: (item["owner_score"], str(item.get("slug") or "")),
        )

    force = request.query_params.get("refresh") == "1"
    probes = _probe_domains(list(owners), force=force)
    apps: list[dict[str, Any]] = []
    # A domain that redirects to another listed domain is an alias, not an
    # app: show it on the target's card instead of as its own "online" card.
    aliases: dict[str, list[str]] = {}
    for domain in owners:
        target = probes.get(domain, {}).get("redirects_to")
        if target and target in owners and probes.get(target, {}).get("healthy"):
            aliases.setdefault(target, []).append(domain)
    aliased = {d for group in aliases.values() for d in group}
    # One category per app, so boxy.bjk.ai and api.boxy.bjk.ai stay together:
    # the domain fallback uses the app's primary domain (first label == slug,
    # else the shortest).
    primary: dict[str, str] = {}
    for domain, app in owners.items():
        slug = str(app.get("slug") or "")
        best = primary.get(slug)
        rank = (domain.split(".", 1)[0] != slug.lower(), len(domain), domain)
        if best is None or rank < (best.split(".", 1)[0] != slug.lower(), len(best), best):
            primary[slug] = domain
    unavailable: list[dict[str, Any]] = []
    for domain, app in owners.items():
        if domain in aliased:
            continue
        health = probes.get(domain, {})
        reason = ""
        if not health.get("healthy"):
            status = health.get("status")
            reason = f"HTTP {status}" if status else (health.get("error") or "no answer")
        elif health.get("status") in (401, 403):
            # An auth challenge proves nginx is up, not the app behind it.
            targets = _loopback_targets(_json_or(app.get("data_json"), {}).get("upstreams"))
            if targets and not _any_listening(targets):
                ports = ", ".join(str(port) for _, port in targets)
                reason = f"login page only: nothing listens on port {ports}"
        if reason:
            unavailable.append({
                "slug": app.get("slug"), "name": app.get("name") or app.get("slug"),
                "domain": domain, "reason": reason, "status": app.get("status") or "unknown",
            })
            continue
        score = app.get("owner_score") or (0, 0, 0)
        inferred_name = domain.split(".", 1)[0].replace("-", " ").replace("_", " ").title()
        display_name = app.get("name") or app.get("slug") or inferred_name
        if not score[0] and not score[1]:
            display_name = inferred_name
        apps.append(
            {
                "id": f"{app.get('slug')}::{domain}",
                "slug": app.get("slug"),
                "name": display_name,
                "domain": domain,
                "url": f"https://{domain}",
                # Served through DEL, never straight from the third-party
                # origin — see _fetch_icon for why (basic-auth credential
                # prompts firing on top of the gallery).
                "icon_url": f"/app-icon/{domain}",
                "initial": str(display_name).strip()[:1].upper() or "?",
                "category": _gallery_category(
                    str(app.get("slug") or ""), str(app.get("name") or ""),
                    primary.get(str(app.get("slug") or ""), domain),
                ),
                "status": app.get("status") or "unknown",
                "kind": app.get("kind") or "unknown",
                "protected": bool(app.get("protected")),
                "http_status": health.get("status"),
                "aliases": sorted(aliases.get(domain, [])),
                "latency_ms": health.get("latency_ms"),
                "checked_at": health.get("checked_at"),
            }
        )

    category_rank = {name: i for i, name in enumerate(_CATEGORY_ORDER)}
    apps.sort(
        key=lambda app: (
            category_rank.get(app["category"], len(category_rank)),
            str(app["name"]).lower(),
            app["domain"],
        )
    )
    categories = [
        category
        for category in _CATEGORY_ORDER
        if category != "Favorites" and any(app["category"] == category for app in apps)
    ]
    checked_at = max((app.get("checked_at") or "" for app in apps), default="")
    unavailable.sort(key=lambda u: u["domain"])
    return _render(
        "view_apps.html",
        request,
        response,
        apps=apps,
        categories=categories,
        all_categories=[c for c in _CATEGORY_ORDER if c != "Favorites"],
        candidate_count=len(owners),
        unavailable=unavailable,
        excluded_count=len(unavailable),
        alias_count=len(aliased),
        latest_scan=latest,
        checked_at=checked_at,
        user=user,
    )


_ICON_DOMAINS: dict[str, Any] = {"scan": None, "domains": frozenset(), "last_check": 0.0}


def _icon_domains() -> frozenset:
    """Domains of enabled nginx sites in the latest scan, computed once per
    scan: a gallery view requests ~125 icons and each used to re-read and
    parse every site row."""
    now = time.monotonic()
    db_path = get_settings().db_path
    with _ICON_LOCK:
        if (
            _ICON_DOMAINS["scan"] is not None
            and _ICON_DOMAINS["scan"][0] == db_path
            and _ICON_DOMAINS["domains"]
            and (now - float(_ICON_DOMAINS.get("last_check", 0))) < 10.0
        ):
            return _ICON_DOMAINS["domains"]

    conn = get_db()
    try:
        latest = _latest_scan_id(conn)
        key = (db_path, latest)
        with _ICON_LOCK:
            _ICON_DOMAINS["last_check"] = now
            if _ICON_DOMAINS["scan"] == key and latest is not None and _ICON_DOMAINS["domains"]:
                return _ICON_DOMAINS["domains"]
        domains = set()
        if latest is not None:
            for row in q(conn, "SELECT data_json FROM resources WHERE type = 'nginx_site' AND last_seen = ?", (latest,)):
                data = _json_or(row["data_json"], {})
                if data.get("enabled", False):
                    domains.update(filter(None, (_valid_gallery_domain(sn) for sn in data.get("server_names") or [])))
    finally:
        conn.close()
    with _ICON_LOCK:
        _ICON_DOMAINS.update(scan=key, domains=frozenset(domains), last_check=now)
    return frozenset(domains)


@router.get("/app-icon/{domain}")
def app_icon(
    domain: str,
    user: User = Depends(auth.require_user),
) -> Response:
    """Serve a gallery app's favicon from DEL's own origin.

    Fetching these in the browser makes every basic-auth-protected app answer
    401 + WWW-Authenticate, which opens a credential dialog over the gallery.
    Proxying absorbs that: a protected app simply has no icon and the card
    shows its initial instead.

    The domain must be a syntactically valid hostname AND currently present as
    an enabled Nginx site in the latest scan, so this cannot be turned into a
    general-purpose outbound fetcher.
    """
    normalized = _valid_gallery_domain(domain)
    if not normalized or normalized not in _icon_domains():
        return Response(status_code=404)

    icon = _cached_icon(normalized)
    if icon is None:
        # 204 rather than 404: the card's fallback initial is the intended
        # result, and this keeps the browser from logging a console error.
        return Response(status_code=204, headers={"Cache-Control": "public, max-age=3600"})
    body, content_type = icon
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
