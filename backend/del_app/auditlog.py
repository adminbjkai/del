"""Append-only audit records: DB + logs/audit.log line. Never log secrets;
callers must pre-sanitize `details`."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from del_app.config import get_settings
from del_app.db import get_db, x


def audit(user_id: int | None, action: str, subject: str, details: dict) -> None:
    """Record an audit entry in the DB and append a line to logs/audit.log."""
    details_json = json.dumps(details, default=str)
    conn = get_db()
    try:
        x(
            conn,
            "INSERT INTO audit_log (user_id, action, subject, details_json) VALUES (?, ?, ?, ?)",
            (user_id, action, subject, details_json),
        )
    finally:
        conn.close()

    settings = get_settings()
    os.makedirs(settings.logs_dir, exist_ok=True)
    line = json.dumps(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "action": action,
            "subject": subject,
            "details": details,
        },
        default=str,
    )
    log_path = os.path.join(settings.logs_dir, "audit.log")
    with open(log_path, "a") as f:
        f.write(line + "\n")
