"""App gallery (/view-apps, /app-icon/{domain}): live HTTPS-probed launcher
plus docker-reclaimable-bytes and favicon-proxy helpers used by the
dashboard/gallery."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import re
import subprocess
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
from del_app.db import get_db, q
from del_app.web.formatting import _parse_docker_size
from del_app.web.queries import _json_or, _latest_scan_id, _rows
from del_app.web.render import _render

router = APIRouter()

_RECLAIMABLE_CACHE: dict[str, Any] = {"ts": 0.0, "value": 0, "refreshing": False}
_RECLAIMABLE_TTL = 300  # 5 minutes
_RECLAIMABLE_LOCK = threading.Lock()

# A gallery request may need to verify dozens of Nginx endpoints. Keep those
# network checks off the request thread pool and cache the result briefly so
# normal navigation never turns into a thundering herd of HTTPS requests.
_APP_PROBE_CACHE: dict[str, dict[str, Any]] = {}
_APP_PROBE_LOCK = threading.Lock()
_PROBE_REFRESHING: set[str] = set()  # coalesces concurrent background refreshes
_APP_PROBE_TTL = 300
_APP_PROBE_TIMEOUT = 3.0

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


def _compute_reclaimable_bytes() -> int:
    """Blocking read of docker's reclaimable bytes (images + build cache +
    volumes) via `docker system df`. Called only off the request thread."""
    total = 0
    try:
        proc = subprocess.run(
            ["docker", "system", "df", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += _parse_docker_size(obj.get("Reclaimable", ""))
    except Exception:
        pass
    return total


def _refresh_reclaimable_async() -> None:
    def _worker() -> None:
        value = _compute_reclaimable_bytes()
        with _RECLAIMABLE_LOCK:
            _RECLAIMABLE_CACHE["value"] = value
            _RECLAIMABLE_CACHE["ts"] = time.monotonic()
            _RECLAIMABLE_CACHE["refreshing"] = False
    threading.Thread(target=_worker, daemon=True).start()


def _reclaimable_bytes() -> int:
    """Docker's reported reclaimable bytes, cached in-process for 5 minutes.

    Stale-while-revalidate: the dashboard request NEVER blocks on docker. It
    returns the last cached value immediately and, when that value is stale,
    kicks off a single background refresh. Blocking here (a `docker system df`
    that can take seconds, worst right after a removal when docker is busy with
    the post-removal rescan) was the cause of slow / multi-click dashboard
    loads. First load after a restart shows 0 until the async refresh lands."""
    now = time.monotonic()
    with _RECLAIMABLE_LOCK:
        value = _RECLAIMABLE_CACHE["value"]
        stale = (now - _RECLAIMABLE_CACHE["ts"]) >= _RECLAIMABLE_TTL
        trigger = stale and not _RECLAIMABLE_CACHE["refreshing"]
        if trigger:
            _RECLAIMABLE_CACHE["refreshing"] = True
    if trigger:
        _refresh_reclaimable_async()
    return value


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

    Any HTTP response below 500 proves that DNS, TLS, Nginx routing, and an
    answering upstream are in place. Auth challenges and root-level 404s still
    count as live; 5xx responses do not count as correctly working.
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
_ICON_CACHE: dict[str, dict[str, Any]] = {}
_ICON_LOCK = threading.Lock()
_ICON_TTL = 86400          # a favicon changes about never
_ICON_NEGATIVE_TTL = 3600  # retry failures sooner than successes
_ICON_TIMEOUT = 4.0
_ICON_MAX_BYTES = 262144
_ICON_CACHE_MAX = 512
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


def _fetch_icon(domain: str) -> tuple[bytes, str] | None:
    """Fetch one icon/favicon for a domain. Returns (body, content_type) or None.

    1. Checks static official overrides for apps with special assets.
    2. Probes direct endpoints (/favicon.ico, /favicon.png, /favicon.svg).
    3. Scrapes root HTML for <link rel="icon"...> or apple-touch-icon.
    """
    # 1. Check official static icon overrides
    static_name = _DOMAIN_STATIC_ICONS.get(domain)
    if static_name:
        static_file = _STATIC_ICONS_DIR / static_name
        if static_file.is_file():
            try:
                body = static_file.read_bytes()
                ext = static_file.suffix.lower()
                ctype = "image/svg+xml" if ext == ".svg" else ("image/png" if ext == ".png" else "image/x-icon")
                return body, ctype
            except Exception:
                pass

    # 2. Check standard direct favicon paths
    for path in ("/favicon.ico", "/favicon.png", "/favicon.svg"):
        request = urllib.request.Request(
            f"https://{domain}{path}",
            headers={"User-Agent": "DEL-App-Gallery/1.0", "Accept": "image/*"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=_ICON_TIMEOUT) as response:
                if int(response.getcode()) != 200:
                    continue
                ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                body = response.read(_ICON_MAX_BYTES + 1)
                if not body or len(body) > _ICON_MAX_BYTES:
                    continue
                # Validate image signature or content-type
                is_img = (
                    ctype.startswith("image/")
                    or body[:4] == b"\x00\x00\x01\x00"
                    or body[:4] == b"\x89PNG"
                    or b"<svg" in body[:250]
                    or body[:4] == b"GIF8"
                )
                if not is_img or ctype == "text/html":
                    continue
                if ctype not in _ICON_ALLOWED_TYPES:
                    if body[:4] == b"\x00\x00\x01\x00":
                        ctype = "image/x-icon"
                    elif body[:4] == b"\x89PNG":
                        ctype = "image/png"
                    elif b"<svg" in body[:250]:
                        ctype = "image/svg+xml"
                    else:
                        ctype = "image/x-icon"
                return body, ctype
        except Exception:
            continue

    # 3. Inspect root HTML for <link rel="icon"...> or apple-touch-icon
    request = urllib.request.Request(
        f"https://{domain}/",
        headers={"User-Agent": "Mozilla/5.0 (DEL-App-Gallery/1.0)", "Accept": "text/html,*/*;q=0.8"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_ICON_TIMEOUT) as response:
            if int(response.getcode()) == 200:
                html = response.read(131072).decode("utf-8", errors="ignore")
                for link in re.findall(r"<link[^>]+>", html, re.I):
                    if re.search(r"rel=[\"']?(?:shortcut )?icon[\"']?", link, re.I) or re.search(
                        r"rel=[\"']?apple-touch-icon[\"']?", link, re.I
                    ):
                        m = re.search(r"href=[\"']([^\"']+)[\"']", link, re.I)
                        if not m:
                            continue
                        href = m.group(1).strip()
                        if href.startswith("data:image/svg+xml"):
                            if ";base64," in href:
                                return base64.b64decode(href.split(";base64,", 1)[1]), "image/svg+xml"
                            return unquote(href.split(",", 1)[1]).encode("utf-8"), "image/svg+xml"
                        elif href.startswith("data:image/png;base64,"):
                            return base64.b64decode(href.split(";base64,", 1)[1]), "image/png"

                        icon_url = urljoin(f"https://{domain}/", href)
                        ireq = urllib.request.Request(
                            icon_url,
                            headers={"User-Agent": "DEL-App-Gallery/1.0", "Accept": "image/*"},
                            method="GET",
                        )
                        try:
                            with urllib.request.urlopen(ireq, timeout=_ICON_TIMEOUT) as iresp:
                                if int(iresp.getcode()) == 200:
                                    idata = iresp.read(_ICON_MAX_BYTES + 1)
                                    ictype = (iresp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                                    is_img = (
                                        ictype.startswith("image/")
                                        or idata[:4] in (b"\x00\x00\x01\x00", b"\x89PNG")
                                        or b"<svg" in idata[:250]
                                    )
                                    if idata and len(idata) <= _ICON_MAX_BYTES and is_img and ictype != "text/html":
                                        if ictype not in _ICON_ALLOWED_TYPES:
                                            if idata[:4] == b"\x00\x00\x01\x00":
                                                ictype = "image/x-icon"
                                            elif idata[:4] == b"\x89PNG":
                                                ictype = "image/png"
                                            elif b"<svg" in idata[:250]:
                                                ictype = "image/svg+xml"
                                            else:
                                                ictype = "image/x-icon"
                                        return idata, ictype
                        except Exception:
                            pass
    except Exception:
        pass

    return None


def _cached_icon(domain: str) -> tuple[bytes, str] | None:
    now = time.monotonic()
    with _ICON_LOCK:
        entry = _ICON_CACHE.get(domain)
        if entry and now < entry["expires"]:
            return entry["value"]

    value = _fetch_icon(domain)

    with _ICON_LOCK:
        if len(_ICON_CACHE) >= _ICON_CACHE_MAX:
            for key in [k for k, v in _ICON_CACHE.items() if v["expires"] <= now]:
                del _ICON_CACHE[key]
            if len(_ICON_CACHE) >= _ICON_CACHE_MAX:
                _ICON_CACHE.clear()
        _ICON_CACHE[domain] = {
            "value": value,
            "expires": now + (_ICON_TTL if value else _ICON_NEGATIVE_TTL),
        }
    return value


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
    conn = get_db()
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
    for domain, app in owners.items():
        health = probes.get(domain, {})
        if not health.get("healthy") or domain in aliased:
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
    return _render(
        "view_apps.html",
        request,
        response,
        apps=apps,
        categories=categories,
        candidate_count=len(owners),
        excluded_count=max(0, len(owners) - len(apps) - len(aliased)),
        alias_count=len(aliased),
        latest_scan=latest,
        checked_at=checked_at,
        user=user,
    )


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
    if not normalized:
        return Response(status_code=404)

    conn = get_db()
    try:
        latest = _latest_scan_id(conn)
        known = False
        if latest is not None:
            for row in _rows(q(
                conn,
                "SELECT data_json FROM resources WHERE type = 'nginx_site' AND last_seen = ?",
                (latest,),
            )):
                data = _json_or(row.get("data_json"), {})
                if not data.get("enabled", False):
                    continue
                if any(_valid_gallery_domain(sn) == normalized
                       for sn in data.get("server_names", []) or []):
                    known = True
                    break
    finally:
        conn.close()
    if not known:
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
