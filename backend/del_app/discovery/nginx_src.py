"""Nginx discovery source: tolerant regex parser for /etc/nginx/sites-enabled
and /etc/nginx/sites-available. Read-only: only reads files, never edits or
reloads nginx. Falls back to `sudo cat` only if direct read fails (files here
are world-readable, but hosts may differ)."""
from __future__ import annotations

import logging
import os
import re
import subprocess

from del_app.models import Resource

logger = logging.getLogger("del_app.discovery.nginx_src")

SITES_ENABLED = "/etc/nginx/sites-enabled"
SITES_AVAILABLE = "/etc/nginx/sites-available"


def _read_file(path: str) -> str | None:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except PermissionError:
        try:
            proc = subprocess.run(
                ["sudo", "-n", "cat", path], capture_output=True, text=True, timeout=10
            )
            if proc.returncode == 0:
                return proc.stdout
            logger.warning("nginx_src: sudo cat failed for %s: %s", path, proc.stderr[:200])
        except Exception:
            logger.exception("nginx_src: sudo cat errored for %s", path)
    except Exception:
        logger.exception("nginx_src: failed to read %s", path)
    return None


def _extract_blocks(text: str, header_re: str) -> list[str]:
    """Return the contents of every {..} block whose opening matches header_re
    (which must match up to and including the opening brace). Balances nested
    braces; tolerant of malformed input (unbalanced braces simply truncate)."""
    blocks = []
    for m in re.finditer(header_re, text):
        idx = m.end()
        depth = 1
        i = idx
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        blocks.append(text[idx : max(idx, i - 1)])
    return blocks


def _extract_location_blocks(text: str) -> list[tuple[str, str]]:
    results = []
    for m in re.finditer(r"location\s+([^\{\s][^\{]*?)\s*\{", text):
        path = m.group(1).strip()
        idx = m.end()
        depth = 1
        i = idx
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        results.append((path, text[idx : max(idx, i - 1)]))
    return results


def _parse_server_block(block: str) -> dict:
    server_names: list[str] = []
    for m in re.finditer(r"server_name\s+([^;]+);", block):
        server_names.extend(m.group(1).split())

    listens = [m.group(1).strip() for m in re.finditer(r"listen\s+([^;]+);", block)]

    ssl_cert_m = re.search(r"ssl_certificate\s+([^;]+);", block)
    ssl_cert = ssl_cert_m.group(1).strip() if ssl_cert_m else None

    body_size_m = re.search(r"client_max_body_size\s+([^;]+);", block)
    client_max_body_size = body_size_m.group(1).strip() if body_size_m else None

    upstreams = []
    for loc_path, loc_body in _extract_location_blocks(block):
        pp_m = re.search(r"proxy_pass\s+([^;]+);", loc_body)
        if not pp_m:
            continue
        proxy_pass = pp_m.group(1).strip()
        port = None
        port_m = re.search(r":(\d+)", proxy_pass)
        if port_m:
            port = int(port_m.group(1))
        upstreams.append({"location": loc_path, "proxy_pass": proxy_pass, "port": port})

    websocket = bool(re.search(r"proxy_set_header\s+Upgrade\b", block)) or "$connection_upgrade" in block

    return {
        "server_names": server_names,
        "listens": listens,
        "ssl_cert": ssl_cert,
        "client_max_body_size": client_max_body_size,
        "upstreams": upstreams,
        "websocket": websocket,
    }


def _resource_from_file(path: str, enabled: bool, stale_copy: bool = False) -> Resource | None:
    text = _read_file(path)
    if text is None:
        return None

    symlink_target = None
    if os.path.islink(path):
        try:
            symlink_target = os.path.realpath(path)
        except OSError:
            symlink_target = None

    server_blocks = _extract_blocks(text, r"server\s*\{")
    if not server_blocks:
        # conf.d snippet or non-server config file; still record it, empty fields.
        parsed = {
            "server_names": [],
            "listens": [],
            "ssl_cert": None,
            "client_max_body_size": None,
            "upstreams": [],
            "websocket": False,
        }
        merged_names = []
    else:
        merged = {
            "server_names": [],
            "listens": [],
            "ssl_cert": None,
            "client_max_body_size": None,
            "upstreams": [],
            "websocket": False,
        }
        for sb in server_blocks:
            p = _parse_server_block(sb)
            merged["server_names"].extend(p["server_names"])
            merged["listens"].extend(p["listens"])
            merged["upstreams"].extend(p["upstreams"])
            merged["ssl_cert"] = merged["ssl_cert"] or p["ssl_cert"]
            merged["client_max_body_size"] = merged["client_max_body_size"] or p["client_max_body_size"]
            merged["websocket"] = merged["websocket"] or p["websocket"]
        parsed = merged
        merged_names = merged["server_names"]

    deduped_names = list(dict.fromkeys(merged_names))
    parsed["server_names"] = deduped_names
    display = ", ".join(deduped_names) if deduped_names else os.path.basename(path)

    return Resource(
        type="nginx_site",
        key=path,
        display=display,
        path=path,
        state="enabled" if enabled else "available",
        data={
            "file": path,
            "symlink_target": symlink_target,
            "enabled": enabled,
            "stale_copy": stale_copy,
            **parsed,
        },
    )


def collect() -> list[Resource]:
    """Parse nginx sites-enabled (symlinks and regular files) and
    sites-available. Read-only."""
    resources: list[Resource] = []
    seen_paths: set[str] = set()
    enabled_names: set[str] = set()

    try:
        if os.path.isdir(SITES_ENABLED):
            for name in sorted(os.listdir(SITES_ENABLED)):
                path = os.path.join(SITES_ENABLED, name)
                enabled_names.add(name)
                try:
                    res = _resource_from_file(path, enabled=True)
                except Exception:
                    logger.exception("nginx_src: failed to parse %s", path)
                    res = None
                if res:
                    resources.append(res)
                seen_paths.add(path)
    except Exception:
        logger.exception("nginx_src: failed to list %s", SITES_ENABLED)

    try:
        if os.path.isdir(SITES_AVAILABLE):
            for name in sorted(os.listdir(SITES_AVAILABLE)):
                path = os.path.join(SITES_AVAILABLE, name)
                if path in seen_paths:
                    continue
                # basename not present in sites-enabled ⇒ not live: this is
                # config debris (*.bak, *.retired, *.stale, or any other
                # available file whose basename has no enabled counterpart).
                stale_copy = name not in enabled_names
                try:
                    res = _resource_from_file(path, enabled=False, stale_copy=stale_copy)
                except Exception:
                    logger.exception("nginx_src: failed to parse %s", path)
                    res = None
                if res:
                    resources.append(res)
    except Exception:
        logger.exception("nginx_src: failed to list %s", SITES_AVAILABLE)

    return resources
