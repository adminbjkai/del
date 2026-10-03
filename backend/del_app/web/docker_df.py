"""`docker system df` figures for the dashboard: reclaimable bytes and the
total size of local volumes. Cached in-process and refreshed in the
background, so a page render never waits on docker."""
from __future__ import annotations

import json
import subprocess
import threading
import time

from del_app.web.formatting import _parse_docker_size

_TTL = 300  # seconds
_RETRY = 60  # after docker failed to answer
_LOCK = threading.Lock()
# state: "measuring" until the first answer, "ok", or "unavailable" when docker
# never answered. Figures stay None unless docker answered at least once.
_CACHE: dict = {
    "ts": 0.0, "refreshing": False,
    "value": {"reclaimable": None, "volumes": None, "state": "measuring"},
}


def _compute() -> dict | None:
    """Blocking `docker system df`; only ever called off the request thread.
    None when docker does not answer (missing, timed out, failed).

    reclaimable: sum of every row's Reclaimable (images, containers, local
    volumes, build cache). volumes: the Size of the "Local Volumes" row."""
    out = {"reclaimable": 0, "volumes": 0}
    try:
        proc = subprocess.run(
            ["docker", "system", "df", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        out["reclaimable"] += _parse_docker_size(row.get("Reclaimable", ""))
        if row.get("Type") == "Local Volumes":
            out["volumes"] = _parse_docker_size(row.get("Size", ""))
    return out


def _refresh() -> None:
    value = _compute()
    with _LOCK:
        now = time.monotonic()
        if value is not None:
            _CACHE.update(value={**value, "state": "ok"}, ts=now, refreshing=False)
            return
        # Keep the last real figures; never present a failure as 0 B.
        if _CACHE["value"]["state"] != "ok":
            _CACHE["value"] = {"reclaimable": None, "volumes": None, "state": "unavailable"}
        _CACHE.update(ts=now - _TTL + _RETRY, refreshing=False)


def docker_df() -> dict:
    """Last known figures, immediately. A stale value starts one background
    refresh; until docker has answered once both figures are None and
    `state` says whether that is "measuring" or "unavailable"."""
    with _LOCK:
        value = dict(_CACHE["value"])
        trigger = time.monotonic() - _CACHE["ts"] >= _TTL and not _CACHE["refreshing"]
        if trigger:
            _CACHE["refreshing"] = True
    if trigger:
        threading.Thread(target=_refresh, name="del-docker-df", daemon=True).start()
    return value
