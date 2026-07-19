"""Authentication: password hashing, sessions, CSRF, login rate limiting."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta

import pydantic
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request, Response
from itsdangerous import BadSignature, Signer

from del_app.config import get_settings
from del_app.db import get_db, q, x

SECRET_KEY_PATH = "/apps/del/config/secret.key"
SESSION_COOKIE_NAME = "del_session"

_hasher = PasswordHasher()


class User(pydantic.BaseModel):
    id: int
    username: str


class NeedsLogin(Exception):
    """Raised by require_user; handled by main.py to redirect to /login."""


def get_secret_key() -> bytes:
    """Return the app secret key, generating and persisting it (0600) on
    first run."""
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "rb") as f:
            return f.read()
    key = secrets.token_bytes(32)
    os.makedirs(os.path.dirname(SECRET_KEY_PATH), exist_ok=True)
    fd = os.open(SECRET_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def _signer() -> Signer:
    return Signer(get_secret_key())


def sign_token(token: str) -> str:
    """Sign a raw session token for storage in the browser cookie."""
    return _signer().sign(token.encode()).decode()


def unsign_token(value: str) -> str | None:
    """Verify a signed cookie value; return the raw token, or None if
    invalid/tampered."""
    try:
        return _signer().unsign(value.encode()).decode()
    except BadSignature:
        return None


# ---------------------------------------------------------------------------
# Users / passwords
# ---------------------------------------------------------------------------

def create_user(username: str, password: str) -> int:
    """Create a user with an argon2id password hash. Returns the user id."""
    password_hash = _hasher.hash(password)
    conn = get_db()
    try:
        return x(
            conn,
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
    finally:
        conn.close()


def change_password(username: str, new_password: str) -> None:
    """Update a user's password hash."""
    password_hash = _hasher.hash(new_password)
    conn = get_db()
    try:
        x(conn, "UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username))
    finally:
        conn.close()


def verify(username: str, password: str) -> int | None:
    """Verify credentials; return user id on success, None on failure."""
    conn = get_db()
    try:
        rows = q(conn, "SELECT id, password_hash FROM users WHERE username = ?", (username,))
        if not rows:
            return None
        row = rows[0]
        try:
            _hasher.verify(row["password_hash"], password)
        except VerifyMismatchError:
            return None
        x(conn, "UPDATE users SET last_login = datetime('now') WHERE id = ?", (row["id"],))
        return row["id"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def login_session(resp: Response, user_id: int, ip: str | None = None) -> str:
    """Create a server-side session and set the signed cookie on resp.
    Returns the raw token (mainly useful for tests)."""
    settings = get_settings()
    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.utcnow() + timedelta(hours=settings.session_hours)).isoformat()
    conn = get_db()
    try:
        x(
            conn,
            "INSERT INTO sessions (token_hash, user_id, expires, ip) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, expires, ip),
        )
    finally:
        conn.close()
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        sign_token(token),
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return token


def _get_session_user(token: str) -> User | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = get_db()
    try:
        rows = q(
            conn,
            """
            SELECT s.expires, u.id, u.username
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        )
        if not rows:
            return None
        row = rows[0]
        if datetime.fromisoformat(row["expires"]) < datetime.utcnow():
            return None
        return User(id=row["id"], username=row["username"])
    finally:
        conn.close()


def require_user(request: Request) -> User:
    """FastAPI dependency: returns the logged-in User or raises NeedsLogin
    (handled in main.py as a redirect to /login)."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        raise NeedsLogin()
    token = unsign_token(cookie)
    if token is None:
        raise NeedsLogin()
    user = _get_session_user(token)
    if user is None:
        raise NeedsLogin()
    return user


def logout_session(request: Request, resp: Response) -> None:
    """Delete the session row and clear the cookie."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        token = unsign_token(cookie)
        if token:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            conn = get_db()
            try:
                x(conn, "DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            finally:
                conn.close()
    resp.delete_cookie(SESSION_COOKIE_NAME)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def csrf_token(session: str) -> str:
    """Derive a CSRF token bound to the given session token (raw, unsigned)."""
    return hmac.new(get_secret_key(), session.encode(), hashlib.sha256).hexdigest()


def check_csrf(request: Request, submitted: str) -> bool:
    """Verify a submitted CSRF token against the current session cookie.
    Called on all POST routes."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return False
    token = unsign_token(cookie)
    if token is None:
        return False
    expected = csrf_token(token)
    return hmac.compare_digest(expected, submitted or "")


# ---------------------------------------------------------------------------
# Login rate limiting (5/min/IP, in-memory)
# ---------------------------------------------------------------------------

_login_attempts: dict[str, list[float]] = {}
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW_SECONDS = 60


def rate_limited(ip: str) -> bool:
    """Return True if this IP has exceeded the login attempt rate limit."""
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    return len(attempts) >= RATE_LIMIT_MAX


def record_attempt(ip: str) -> None:
    """Record a login attempt for this IP."""
    _login_attempts.setdefault(ip, []).append(time.time())
