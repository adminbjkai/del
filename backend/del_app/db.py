"""SQLite connection + migration runner for DEL."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from del_app.config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def get_db(db_path: str | None = None) -> sqlite3.Connection:
    """Return a new per-call sqlite3 connection with Row factory, WAL mode,
    busy_timeout and foreign_keys enabled."""
    path = db_path or get_settings().db_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL already gives crash-safety; NORMAL drops one fsync per commit, which
    # matters because x() commits per statement (create_job writes one row per
    # plan step, auditlog writes one row per step transition).
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def latest_done_scan_id(conn: sqlite3.Connection) -> int | None:
    """Id of the most recent *completed* scan, or None if there is none yet.

    Single source of truth for scan scoping. run_scan() inserts its scan row as
    'running' before it collects anything, so MAX(id) with no status filter
    points at an empty in-flight scan — which made every scoped query (the
    inventory pages, and worse, build_plan) see zero resources mid-scan.
    """
    row = conn.execute(
        "SELECT MAX(id) AS m FROM scans WHERE status = 'done'"
    ).fetchone()
    return row["m"] if row else None


def run_migrations(db_path: str | None = None) -> None:
    """Apply backend/del_app/migrations/NNN_*.sql in order, tracked in
    schema_migrations."""
    conn = get_db(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
        applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            sql = path.read_text()
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (name) VALUES (?)", (path.name,)
            )
            conn.commit()
    finally:
        conn.close()


def q(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    """Execute a query and return all rows."""
    cur = conn.execute(sql, params)
    return cur.fetchall()


def x(conn: sqlite3.Connection, sql: str, params=()) -> int:
    """Execute a statement, commit, and return lastrowid."""
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid
