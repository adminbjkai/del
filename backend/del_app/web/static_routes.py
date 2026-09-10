"""Static assets (no auth: needed by /login too; CSP 'self', no CDN)."""
from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse, RedirectResponse

from del_app.web.render import STATIC_DIR

router = APIRouter()

# FileResponse already sets etag + last-modified, so a repeat visit gets a 304.
# Without Cache-Control the browser still pays a revalidation round trip for
# every asset on every page. These are revalidated rather than blindly cached
# (`must-revalidate` on a short max-age) so a deploy is picked up promptly
# without an asset-versioning scheme.
_STATIC_CACHE = "public, max-age=300, must-revalidate"


@router.get("/static/app.css")
def static_css() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "app.css", media_type="text/css",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/static/app.js")
def static_js() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "app.js", media_type="application/javascript",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/static/assistant.css")
def static_assistant_css() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "assistant.css", media_type="text/css",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/static/assistant.js")
def static_assistant_js() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "assistant.js", media_type="application/javascript",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/static/theme-init.js")
def static_theme_init() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "theme-init.js", media_type="application/javascript",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/static/favicon.svg")
def static_favicon() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "favicon.svg", media_type="image/svg+xml",
        headers={"Cache-Control": _STATIC_CACHE},
    )


@router.get("/favicon.ico")
def favicon_ico() -> Response:
    """Browsers request /favicon.ico unprompted; answer it instead of 404ing.

    Redirects to the SVG rather than shipping a second binary asset.
    """
    return RedirectResponse(url="/static/favicon.svg", status_code=301)
