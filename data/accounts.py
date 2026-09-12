"""
accounts.py — who is allowed in, and who to email.

A shared PIN answers "may this request proceed". It cannot answer "who is
this", which is the question everything after the first five users depends on:
who was promised free access, who to email about pricing, who to cut off.

Auth is a magic link. The address is the account, a one-time link proves the
person reads that address, and a signed cookie carries the session afterwards.
No passwords -- storing password hashes for a handful of traders is a liability
with no upside, and every one of them would reuse a password anyway.

The cookie is stateless and HMAC-signed rather than a row in a sessions table:
the app restarts on every deploy, and a sessions table would sign everyone out
each time. Revocation still works because `read_session` re-checks the account
on every request, which is the only lookup that has to stay fast.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "accounts", "accounts.db")

# Deliberately permissive: one @, something either side, a dot in the domain.
# Anything stricter rejects addresses that genuinely exist, and the magic link
# is the real test of whether an address works.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

LINK_TTL_S    = 30 * 60             # a magic link is good for half an hour
SESSION_TTL_S = 60 * 60 * 24 * 30   # a session lasts a month

# The first accounts are founders: promised free access for good. Recorded at
# signup because reconstructing "who did I promise?" from memory later is how
# people end up honouring promises they never made and breaking ones they did.
FOUNDER_SEATS = 5

PLANS    = ("founder", "free", "paid")
STATUSES = ("pending", "active", "blocked")


def default_db_path() -> str:
    return os.environ.get("SCANNER_ACCOUNTS_DB") or _DEFAULT_DB


def normalize_email(raw: str) -> str:
    """Lowercased and trimmed, or ValueError. Case matters to no mail server
    anyone uses, and 'Dante@' signing up twice would be two accounts."""
    email = (raw or "").strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise ValueError("that does not look like an email address")
    return email


class AccountStore:
    def __init__(self, db_path: Optional[str] = None, secret: Optional[str] = None):
        db_path = db_path or default_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._secret = (secret or os.environ.get("SCANNER_SESSION_SECRET") or "").encode()
        if not self._secret:
            # A generated secret means sessions do not survive a restart, which
            # is an inconvenience. A hardcoded default would mean anyone with
            # the source could mint a session, which is a breach.
            self._secret = secrets.token_bytes(32)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    email       TEXT PRIMARY KEY,
                    plan        TEXT NOT NULL DEFAULT 'free',
                    status      TEXT NOT NULL DEFAULT 'pending',
                    subscribed  INTEGER NOT NULL DEFAULT 1,
                    created_at  REAL NOT NULL,
                    last_seen   REAL,
                    note        TEXT
                );
                CREATE TABLE IF NOT EXISTS links (
                    token      TEXT PRIMARY KEY,
                    email      TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at    REAL
                );
                CREATE INDEX IF NOT EXISTS idx_links_email ON links(email);
                """
            )
            self.conn.commit()

    # ── Signing up ───────────────────────────────────────────────────────────
    def request_access(self, raw_email: str, ttl_secs: float = LINK_TTL_S) -> Dict[str, Any]:
        """
        Create the account if new, and mint a one-time link either way.

        Returns the email, the token, and whether this address is new -- the
        caller decides what to say to a returning user.
        """
        email = normalize_email(raw_email)
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            row = self.conn.execute(
                "SELECT email, plan FROM users WHERE email = ?", (email,)).fetchone()
            is_new = row is None
            if is_new:
                seats_taken = self.conn.execute(
                    "SELECT COUNT(*) FROM users WHERE plan = 'founder'").fetchone()[0]
                plan = "founder" if seats_taken < FOUNDER_SEATS else "free"
                self.conn.execute(
                    "INSERT INTO users (email, plan, status, created_at) "
                    "VALUES (?, ?, 'pending', ?)", (email, plan, now))
            self.conn.execute(
                "INSERT INTO links (token, email, created_at, expires_at) VALUES (?,?,?,?)",
                (token, email, now, now + ttl_secs))
            # One live link per address: requesting a second one retires the
            # first, so a forwarded old email cannot be used behind them.
            self.conn.execute(
                "UPDATE links SET used_at = ? WHERE email = ? AND token != ? AND used_at IS NULL",
                (now, email, token))
            self.conn.commit()
        return {"email": email, "token": token, "is_new": is_new}

    def redeem(self, token: str) -> Optional[str]:
        """Consume a link. Returns the email, or None for anything not exactly
        one unused, unexpired link belonging to an account in good standing."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            row = self.conn.execute(
                "SELECT email, expires_at, used_at FROM links WHERE token = ?",
                (token,)).fetchone()
            if row is None or row["used_at"] is not None or row["expires_at"] <= now:
                return None
            user = self.conn.execute(
                "SELECT status FROM users WHERE email = ?", (row["email"],)).fetchone()
            if user is None or user["status"] == "blocked":
                return None
            self.conn.execute("UPDATE links SET used_at = ? WHERE token = ?", (now, token))
            self.conn.execute(
                "UPDATE users SET status = 'active', last_seen = ? WHERE email = ?",
                (now, row["email"]))
            self.conn.commit()
        return row["email"]

    # ── Sessions ─────────────────────────────────────────────────────────────
    def issue_session(self, email: str, ttl_secs: float = SESSION_TTL_S) -> str:
        payload = f"{email}|{int(time.time() + ttl_secs)}".encode()
        body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        return f"{body}.{self._sign(body)}"

    def read_session(self, cookie: str) -> Optional[str]:
        """The email this cookie proves, or None. Re-reads the account every
        time so blocking someone takes effect on their next request rather than
        whenever their cookie happens to expire."""
        if not cookie or "." not in cookie:
            return None
        body, _, sig = cookie.rpartition(".")
        if not hmac.compare_digest(sig, self._sign(body)):
            return None
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
            email, _, exp = raw.rpartition("|")
            if not email or time.time() > float(exp):
                return None
        except Exception:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT status FROM users WHERE email = ?", (email,)).fetchone()
        if row is None or row["status"] == "blocked":
            return None
        return email

    def _sign(self, body: str) -> str:
        mac = hmac.new(self._secret, body.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    # ── Administration ───────────────────────────────────────────────────────
    def get_user(self, raw_email: str) -> Optional[Dict[str, Any]]:
        email = normalize_email(raw_email)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def mailing_list(self) -> List[Dict[str, Any]]:
        """Everyone who has not unsubscribed. The only list an update may go to."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM users WHERE subscribed = 1 AND status != 'blocked' "
                "ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def set_plan(self, raw_email: str, plan: str) -> None:
        if plan not in PLANS:
            raise ValueError(f"plan must be one of {PLANS}")
        self._update(raw_email, "plan", plan)

    def set_status(self, raw_email: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        self._update(raw_email, "status", status)

    def set_subscribed(self, raw_email: str, subscribed: bool) -> None:
        self._update(raw_email, "subscribed", 1 if subscribed else 0)

    def set_note(self, raw_email: str, note: str) -> None:
        self._update(raw_email, "note", note)

    def _update(self, raw_email: str, column: str, value: Any) -> None:
        email = normalize_email(raw_email)
        with self._lock:
            self.conn.execute(
                f"UPDATE users SET {column} = ? WHERE email = ?", (value, email))
            self.conn.commit()

    def counts(self) -> Dict[str, int]:
        users = self.list_users()
        return {
            "total":     len(users),
            "active":    sum(1 for u in users if u["status"] == "active"),
            "pending":   sum(1 for u in users if u["status"] == "pending"),
            "blocked":   sum(1 for u in users if u["status"] == "blocked"),
            "founders":  sum(1 for u in users if u["plan"] == "founder"),
            "paid":      sum(1 for u in users if u["plan"] == "paid"),
            "subscribed": sum(1 for u in users if u["subscribed"]),
        }
