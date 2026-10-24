"""Server-side state for the web page (same SQLite file as the task store).

* Sessions: the browser only holds a random ID in an HttpOnly cookie; the
  database stores its SHA-256, so a copied database can't be replayed as a
  login.
* GitHub tokens are encrypted at rest (Fernet / AES-128-CBC + HMAC) with a
  key kept in a 0600 file outside git.
* Proposals: a pending change is stored server-side and approved by ID, so
  the page can only ever apply the exact preview the person saw.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from .github_user import TokenSet
from .repo_actions import Proposal


def load_or_create_key(path: str) -> bytes:
    """Encryption key from WEB_ENCRYPTION_KEY, or a 0600 key file (created once)."""
    env_key = os.environ.get("WEB_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode()
    key_file = Path(path)
    if key_file.exists():
        return key_file.read_bytes().strip()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def _hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


class WebStore:
    def __init__(self, db_path: str, key: bytes, session_hours: int = 8):
        self.db_path = db_path
        self.fernet = Fernet(key)
        self.ttl = session_hours * 3600
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS web_sessions (
                    id_hash TEXT PRIMARY KEY,
                    csrf TEXT NOT NULL,
                    oauth_state TEXT,
                    github_login TEXT,
                    tokens TEXT,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_proposals (
                    id TEXT PRIMARY KEY,
                    github_login TEXT NOT NULL,
                    data TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pr_url TEXT,
                    created_at REAL NOT NULL
                );
                """
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # -- sessions ---------------------------------------------------------

    def new_session(self, oauth_state: Optional[str] = None) -> str:
        session_id = secrets.token_urlsafe(32)
        with self._conn() as conn:
            conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (time.time(),))
            conn.execute(
                "INSERT INTO web_sessions (id_hash, csrf, oauth_state, expires_at) VALUES (?, ?, ?, ?)",
                (_hash(session_id), secrets.token_urlsafe(32), oauth_state, time.time() + self.ttl),
            )
        return session_id

    def get_session(self, session_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not session_id:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM web_sessions WHERE id_hash = ? AND expires_at > ?",
                (_hash(session_id), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def sign_in(self, session_id: str, login: str, tokens: TokenSet) -> str:
        """Bind a GitHub identity; returns a NEW session ID (prevents fixation)."""
        self.delete_session(session_id)
        new_id = self.new_session()
        with self._conn() as conn:
            conn.execute(
                "UPDATE web_sessions SET github_login = ?, tokens = ? WHERE id_hash = ?",
                (login, self._encrypt(tokens), _hash(new_id)),
            )
        return new_id

    def update_tokens(self, session_id: str, tokens: TokenSet) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE web_sessions SET tokens = ? WHERE id_hash = ?",
                         (self._encrypt(tokens), _hash(session_id)))

    def tokens(self, session: Dict[str, Any]) -> Optional[TokenSet]:
        if not session.get("tokens"):
            return None
        try:
            return TokenSet(**json.loads(self.fernet.decrypt(session["tokens"].encode())))
        except (InvalidToken, ValueError, TypeError):
            return None

    def delete_session(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM web_sessions WHERE id_hash = ?", (_hash(session_id),))

    def _encrypt(self, tokens: TokenSet) -> str:
        return self.fernet.encrypt(json.dumps(asdict(tokens)).encode()).decode()

    # -- proposals --------------------------------------------------------

    def save_proposal(self, login: str, proposal: Proposal) -> str:
        proposal_id = secrets.token_urlsafe(12)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO web_proposals (id, github_login, data, fingerprint, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (proposal_id, login, json.dumps(asdict(proposal)), proposal.fingerprint(), time.time()),
            )
        return proposal_id

    def get_proposal(self, proposal_id: str, login: str) -> Optional[Dict[str, Any]]:
        """A proposal is only visible to the person who made it."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM web_proposals WHERE id = ? AND github_login = ?", (proposal_id, login)
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        record["proposal"] = Proposal(**json.loads(record.pop("data")))
        return record

    def claim_proposal(self, proposal_id: str, login: str) -> bool:
        """Atomically move pending -> applying (a double submit can't apply twice)."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE web_proposals SET status = 'applying' "
                "WHERE id = ? AND github_login = ? AND status = 'pending' AND created_at > ?",
                (proposal_id, login, time.time() - 3600),
            )
            return cur.rowcount == 1

    def finish_proposal(self, proposal_id: str, status: str, pr_url: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE web_proposals SET status = ?, pr_url = ? WHERE id = ?",
                         (status, pr_url, proposal_id))

    def proposals_last_hour(self, login: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM web_proposals WHERE github_login = ? AND created_at > ?",
                (login, time.time() - 3600),
            ).fetchone()
        return int(row["n"])
