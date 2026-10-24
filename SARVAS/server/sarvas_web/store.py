"""Server-side state in SQLite.
 - sessions: the browser holds a random ID; the DB stores only its SHA-256
 - GitHub tokens are AES-256-GCM encrypted
 - proposals are stored server-side and approved by ID (exact preview only)
 - audit log of every sign-in, proposal, refusal and approval
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .crypto import decrypt, encrypt, random_token, sha256

HOUR_MS = 3600 * 1000


def _now() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, data_dir: str, key: bytes, session_hours: float = 8):
        Path(data_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data_dir) / "web.db", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.key = key
        self.ttl = int(session_hours * HOUR_MS)
        self.lock = threading.Lock()
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS sessions (
              id_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, oauth_state TEXT,
              login TEXT, tokens TEXT, expires_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS proposals (
              id TEXT PRIMARY KEY, login TEXT NOT NULL, data TEXT NOT NULL,
              fingerprint TEXT NOT NULL, status TEXT NOT NULL, pr_url TEXT, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT, event TEXT NOT NULL,
              detail TEXT, created_at TEXT NOT NULL);
        """)

    def _run(self, sql: str, *args) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, args)

    def _one(self, sql: str, *args) -> dict | None:
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    # -- sessions -------------------------------------------------------------

    def new_session(self, oauth_state: str | None = None) -> str:
        sid = random_token()
        self._run("DELETE FROM sessions WHERE expires_at < ?", _now())
        self._run("INSERT INTO sessions (id_hash, csrf, oauth_state, expires_at) VALUES (?, ?, ?, ?)",
                  sha256(sid), random_token(), oauth_state, _now() + self.ttl)
        return sid

    def get_session(self, sid: str | None) -> dict | None:
        if not sid:
            return None
        return self._one("SELECT * FROM sessions WHERE id_hash = ? AND expires_at > ?", sha256(sid), _now())

    def sign_in(self, old_id: str | None, login: str, tokens: dict) -> str:
        """Bind a GitHub identity; returns a NEW session ID (prevents fixation)."""
        self.delete_session(old_id)
        sid = self.new_session()
        self._run("UPDATE sessions SET login = ?, tokens = ? WHERE id_hash = ?",
                  login, encrypt(self.key, tokens), sha256(sid))
        return sid

    def tokens(self, session: dict | None) -> dict | None:
        return decrypt(self.key, session["tokens"]) if session and session.get("tokens") else None

    def update_tokens(self, sid: str, tokens: dict) -> None:
        self._run("UPDATE sessions SET tokens = ? WHERE id_hash = ?", encrypt(self.key, tokens), sha256(sid))

    def delete_session(self, sid: str | None) -> None:
        if sid:
            self._run("DELETE FROM sessions WHERE id_hash = ?", sha256(sid))

    # -- proposals ------------------------------------------------------------

    def save_proposal(self, login: str, proposal: dict, fingerprint: str) -> str:
        pid = random_token(12)
        self._run("""INSERT INTO proposals (id, login, data, fingerprint, status, created_at)
                     VALUES (?, ?, ?, ?, 'pending', ?)""", pid, login, json.dumps(proposal), fingerprint, _now())
        return pid

    def get_proposal(self, pid: str, login: str) -> dict | None:
        """Only the person who made a proposal can see it."""
        row = self._one("SELECT * FROM proposals WHERE id = ? AND login = ?", pid, login)
        return {**row, "proposal": json.loads(row["data"])} if row else None

    def claim_proposal(self, pid: str, login: str) -> bool:
        """Atomically pending -> applying, so a double click can't apply twice. Expires after 1h."""
        cur = self._run("""UPDATE proposals SET status = 'applying'
                           WHERE id = ? AND login = ? AND status = 'pending' AND created_at > ?""",
                        pid, login, _now() - HOUR_MS)
        return cur.rowcount == 1

    def finish_proposal(self, pid: str, status: str, pr_url: str | None = None) -> None:
        self._run("UPDATE proposals SET status = ?, pr_url = ? WHERE id = ?", status, pr_url, pid)

    def proposals_last_hour(self, login: str) -> int:
        return self._one("SELECT COUNT(*) AS n FROM proposals WHERE login = ? AND created_at > ?",
                         login, _now() - HOUR_MS)["n"]

    # -- audit ----------------------------------------------------------------

    def record(self, login: str | None, event: str, detail: dict | None = None) -> None:
        self._run("INSERT INTO audit (login, event, detail, created_at) VALUES (?, ?, ?, ?)",
                  login, event, json.dumps(detail or {}),
                  datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))

    def events_last_hour(self, login: str, event: str) -> int:
        since = datetime.fromtimestamp((_now() - HOUR_MS) / 1000, timezone.utc)
        return self._one("SELECT COUNT(*) AS n FROM audit WHERE login = ? AND event = ? AND created_at > ?",
                         login, event, since.isoformat(timespec="milliseconds").replace("+00:00", "Z"))["n"]

    def events(self, login: str, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT event, detail, created_at FROM audit WHERE login = ? "
                                   "ORDER BY id DESC LIMIT ?", (login, limit)).fetchall()
        return [{"event": r["event"], "detail": json.loads(r["detail"]), "at": r["created_at"]} for r in rows]
