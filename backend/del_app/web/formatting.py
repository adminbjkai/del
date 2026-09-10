"""Pure formatting helpers shared across the web UI: byte/date/duration
display strings. No DB access, no routes."""
from __future__ import annotations

from typing import Any

from del_app.web.queries import _json_or


def _human_size(num: Any) -> str:
    """Human-format a byte count. Accepts int/float/None; returns e.g. '1.2 GB'."""
    try:
        n = float(num)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while n >= 1000 and i < len(units) - 1:
        n /= 1000.0
        i += 1
    return (f"{n:.0f} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}")


def _level(confidence: Any, source: str | None) -> str:
    """Map numeric confidence + source to a level (mirrors planner mapping)."""
    if source == "manual":
        return "manual"
    try:
        c = int(confidence)
    except (TypeError, ValueError):
        return "possible"
    if c >= 95:
        return "confirmed"
    if c >= 80:
        return "high"
    if c >= 60:
        return "probable"
    if c >= 30:
        return "possible"
    return "unrelated"


# Display timezone for every human-facing datetime in the UI.
# Storage remains UTC (sqlite datetime('now'), Docker Created Z, fs_src ISO-Z).
_DISPLAY_TZ_NAME = "America/New_York"


def _display_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(_DISPLAY_TZ_NAME)


def _parse_dt(value: Any):
    """Parse ISO / sqlite / docker datetime into a timezone-aware UTC datetime.

    Naive values (sqlite `datetime('now')`, space-separated timestamps) are
    treated as UTC — that matches how DEL writes scan/job rows. Aware values
    (Docker `Created`, fs_src `…Z`) are converted to UTC.
    Returns None if unparseable.
    """
    from datetime import datetime, timezone

    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Docker Created often ends with fractional seconds + Z / offset.
    s = s.replace("Z", "+00:00")
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Truncate fractional seconds for "2026-05-13T11:31:00.840175564+00:00"
        # style strings whose fractional part is too long for fromisoformat.
        core = s
        if "." in core:
            head, rest = core.split(".", 1)
            frac = ""
            tz = ""
            for i, ch in enumerate(rest):
                if ch.isdigit():
                    frac += ch
                else:
                    tz = rest[i:]
                    break
            frac = (frac + "000000")[:6]
            try:
                dt = datetime.fromisoformat(f"{head}.{frac}{tz}")
            except ValueError:
                try:
                    dt = datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
        else:
            try:
                dt = datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        # sqlite datetime('now') is UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _to_eastern(value: Any):
    """Timezone-aware America/New_York datetime, or None."""
    dt = _parse_dt(value)
    if dt is None:
        return None
    return dt.astimezone(_display_tz())


def _format_dt(value: Any, *, date_only: bool = False) -> str:
    """Format a datetime for UI display in Eastern (New York).

    Compact style (no timezone suffix — values are always Eastern):
      full:      ``05-13-26 7:31 AM``   (MM-DD-YY H:MM AM/PM)
      date_only: ``05-13-26``
    Returns ``—`` if unparseable. Calendar day follows Eastern, not UTC.
    """
    local = _to_eastern(value)
    if local is None:
        return "—"
    # %-I is glibc (Linux) — hour without leading zero.
    if date_only:
        return local.strftime("%m-%d-%y")
    return local.strftime("%m-%d-%y %-I:%M %p")


def _relative_dt(value: Any) -> str:
    """Short relative age string ('3d ago', '2h ago'), or '' if unparseable."""
    from datetime import datetime, timezone

    dt = _parse_dt(value)
    if dt is None:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 86400 * 45:
        return f"{int(secs // 86400)}d ago"
    if secs < 86400 * 365:
        return f"{int(secs // (86400 * 30))}mo ago"
    return f"{int(secs // (86400 * 365))}y ago"


def _iso_sort_key(value: Any) -> str:
    """UTC ISO string for data-sort-value (stable chronological sort)."""
    dt = _parse_dt(value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _earliest_iso(*values: Any) -> str | None:
    """Return the earliest parseable timestamp as ISO-UTC, or None."""
    best_dt = None
    for v in values:
        dt = _parse_dt(v)
        if dt is None:
            continue
        if best_dt is None or dt < best_dt:
            best_dt = dt
    if best_dt is None:
        return None
    return best_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _installed_at_from_resources(resource_rows: list[dict]) -> str | None:
    """Best-effort install time from associated containers/directories.

    Preference order of *signals* (earliest wins across all of them):
      - container `created` (Docker Created)
      - directory birthtime / ctime / mtime (filesystem)
    Returns ISO-UTC string or None when no signal is available.
    """
    candidates: list[Any] = []
    for r in resource_rows:
        rtype = r.get("type") or r.get("resource_type")
        data = r.get("data") if isinstance(r.get("data"), dict) else None
        if data is None:
            data = _json_or(r.get("data_json") or r.get("resource_data_json"), {})
        if rtype == "container":
            if data.get("created"):
                candidates.append(data["created"])
        elif rtype == "directory":
            for key in ("birthtime", "ctime", "mtime"):
                if data.get(key):
                    candidates.append(data[key])
                    break  # one directory contributes its best single signal
    return _earliest_iso(*candidates)


def _duration(started: Any, finished: Any) -> str:
    """Human duration between two ISO/sqlite datetime strings, or '—'."""
    s = _parse_dt(started)
    f = _parse_dt(finished)
    if s is None or f is None:
        return "—"
    secs = (f - s).total_seconds()
    if secs < 0:
        return "—"
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.1f}m"
    return f"{secs / 3600:.1f}h"


def _parse_docker_size(text: str) -> int:
    """Parse a docker-cli human size string (e.g. '1.2GB', '500MB (40%)',
    '0B') into a byte count. Docker's go-units formats with 1000-based
    units, matching this module's own _human_size."""
    text = (text or "").split("(")[0].strip()
    if not text:
        return 0
    units = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4, "PB": 1000**5}
    for suffix, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if text.upper().endswith(suffix):
            num = text[: -len(suffix)].strip()
            try:
                return int(float(num) * mult)
            except ValueError:
                return 0
    return 0
