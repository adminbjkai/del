"""True file creation (birth) and modification times for discovery sources.

Python 3.10's os.stat has no st_birthtime on Linux, but ext4/xfs/btrfs record
a birth time that statx exposes; coreutils `stat` prints it as %W (0 when the
filesystem has none). Directory ctime is NOT a creation time: it changes every
time an entry is added, removed or renamed, so it was reporting week-old apps
as "installed today". Read-only.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone

logger = logging.getLogger("del_app.discovery.file_times")

STAT_TIMEOUT = 15
_BATCH = 200


def _iso(epoch: str) -> str | None:
    try:
        value = int(epoch)
    except (TypeError, ValueError):
        return None
    if value <= 0:  # %W prints 0 when birth time is unknown
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stat_times(paths: list[str]) -> dict[str, dict[str, str | None]]:
    """Map each path to {"birthtime", "mtime"} (ISO-UTC or None).

    Follows symlinks (an enabled nginx site is a symlink; its target file is
    the one that was written). Paths that cannot be statted are omitted.
    """
    out: dict[str, dict[str, str | None]] = {}
    unique = [p for p in dict.fromkeys(paths) if p]
    for i in range(0, len(unique), _BATCH):
        batch = unique[i:i + _BATCH]
        try:
            proc = subprocess.run(
                ["stat", "-L", "--printf", r"%W %Y %n\0", "--", *batch],
                capture_output=True, text=True, timeout=STAT_TIMEOUT, check=False,
            )
        except Exception:
            logger.warning("file_times: stat failed for a batch of %d paths", len(batch))
            continue
        # Non-zero exit just means some path vanished; the rest still printed.
        for record in proc.stdout.split("\0"):
            parts = record.split(" ", 2)
            if len(parts) != 3:
                continue
            out[parts[2]] = {"birthtime": _iso(parts[0]), "mtime": _iso(parts[1])}
    return out
