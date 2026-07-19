-- DEL initial schema per docs/ARCHITECTURE.md "Data model"

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created TEXT NOT NULL DEFAULT (datetime('now')),
    expires TEXT NOT NULL,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL DEFAULT (datetime('now')),
    finished TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats_json TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    kind TEXT,
    protected INTEGER NOT NULL DEFAULT 0,
    manifest_path TEXT,
    first_seen INTEGER,
    last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    key TEXT NOT NULL,
    display TEXT,
    path TEXT,
    state TEXT,
    data_json TEXT,
    first_seen INTEGER,
    last_seen INTEGER,
    UNIQUE(type, key)
);

CREATE TABLE IF NOT EXISTS associations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES applications(id),
    resource_id INTEGER NOT NULL REFERENCES resources(id),
    confidence INTEGER NOT NULL,
    ownership TEXT,
    shared INTEGER NOT NULL DEFAULT 0,
    data_loss_risk TEXT,
    removal_eligible TEXT,
    recommended_action TEXT,
    evidence_json TEXT,
    source TEXT,
    approved_by_user INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES applications(id),
    created TEXT NOT NULL DEFAULT (datetime('now')),
    options_json TEXT,
    steps_json TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    hmac TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    mode TEXT NOT NULL,
    started TEXT,
    finished TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    user_id INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS job_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    seq INTEGER NOT NULL,
    stage TEXT NOT NULL,
    operation TEXT NOT NULL,
    args_json TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    exit_code INTEGER,
    output_sanitized TEXT,
    started TEXT,
    finished TEXT,
    reversible INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    kind TEXT NOT NULL,
    src TEXT,
    dest TEXT,
    sha256 TEXT,
    size INTEGER,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    user_id INTEGER,
    action TEXT NOT NULL,
    subject TEXT,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
