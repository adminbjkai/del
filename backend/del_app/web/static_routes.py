"""Static assets (no auth: /login needs them too; CSP 'self', no CDN).

Templates link every asset through `asset_url()`, which appends a short
content hash (`/static/app.css?v=1a2b3c4d`). A request carrying the current
hash is cached for a year as immutable, so a page view costs no asset
round trips at all; a deploy changes the hash and therefore the URL. Requests
without (or with a stale) hash still work but are only cached briefly.

Fonts are the exception: app.css names them by plain path, so a font file
never changes in place; ship a changed font under a new file name. They are
always immutable, and asset_url() gives them no hash so a <link rel=preload>
matches the URL the stylesheet requests.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse, RedirectResponse

from del_app.web.render import STATIC_DIR, templates

router = APIRouter()

_IMMUTABLE = "public, max-age=31536000, immutable"
_SHORT = "public, max-age=300, must-revalidate"

# Only these folders are public. static/icons/ stays private: the gallery
# reads those files server-side for /app-icon/{domain}.
_PUBLIC_DIRS = {STATIC_DIR, STATIC_DIR / "fonts"}
_MEDIA_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".txt": "text/plain",
}

# (path, mtime_ns, size) -> short sha256; a changed file gets a new entry.
_HASHES: dict[tuple[str, int, int], str] = {}


def _resolve(name: str) -> Path | None:
    try:
        path = (STATIC_DIR / name).resolve()
    except (OSError, ValueError):
        return None
    if path.parent not in _PUBLIC_DIRS or path.suffix not in _MEDIA_TYPES:
        return None
    return path if path.is_file() else None


def _version(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    digest = _HASHES.get(key)
    if digest is None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        _HASHES[key] = digest
    return digest


def asset_url(name: str) -> str:
    """URL for a static file with its content hash, for templates."""
    path = _resolve(name)
    if path is None or path.suffix == ".woff2":
        return f"/static/{name}"
    return f"/static/{name}?v={_version(path)}"


templates.env.globals["asset"] = asset_url


@router.get("/static/{name:path}")
def static_file(name: str, v: str = "") -> Response:
    path = _resolve(name)
    if path is None:
        return Response(status_code=404)
    fresh = path.suffix == ".woff2" or (v and v == _version(path))
    cache = _IMMUTABLE if fresh else _SHORT
    return FileResponse(path, media_type=_MEDIA_TYPES[path.suffix], headers={"Cache-Control": cache})


@router.get("/favicon.ico")
def favicon_ico() -> Response:
    """Browsers request /favicon.ico unprompted; answer it instead of 404ing.

    Redirects to the SVG rather than shipping a second binary asset.
    """
    return RedirectResponse(url="/static/favicon.svg", status_code=301)
