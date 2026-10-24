// Server-side state in SQLite (node:sqlite, no native deps).
//  - sessions: the browser holds a random ID; the DB stores only its SHA-256
//  - GitHub tokens are AES-256-GCM encrypted
//  - proposals are stored server-side and approved by ID (exact preview only)
//  - audit log of every sign-in, proposal, refusal and approval
import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { decrypt, encrypt, randomToken, sha256 } from './crypto.js';

const HOUR = 3600 * 1000;

export class Store {
  constructor(dataDir, key, sessionHours = 8) {
    fs.mkdirSync(dataDir, { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path.join(dataDir, 'web.db'));
    this.key = key;
    this.ttl = sessionHours * HOUR;
    this.db.exec(`
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
    `);
  }

  // -- sessions -----------------------------------------------------------

  newSession(oauthState = null) {
    const id = randomToken();
    this.db.prepare('DELETE FROM sessions WHERE expires_at < ?').run(Date.now());
    this.db
      .prepare('INSERT INTO sessions (id_hash, csrf, oauth_state, expires_at) VALUES (?, ?, ?, ?)')
      .run(sha256(id), randomToken(), oauthState, Date.now() + this.ttl);
    return id;
  }

  getSession(id) {
    if (!id) return null;
    return this.db
      .prepare('SELECT * FROM sessions WHERE id_hash = ? AND expires_at > ?')
      .get(sha256(id), Date.now()) ?? null;
  }

  /** Bind a GitHub identity; returns a NEW session ID (prevents fixation). */
  signIn(oldId, login, tokens) {
    this.deleteSession(oldId);
    const id = this.newSession();
    this.db.prepare('UPDATE sessions SET login = ?, tokens = ? WHERE id_hash = ?')
      .run(login, encrypt(this.key, tokens), sha256(id));
    return id;
  }

  tokens(session) {
    return session?.tokens ? decrypt(this.key, session.tokens) : null;
  }

  updateTokens(id, tokens) {
    this.db.prepare('UPDATE sessions SET tokens = ? WHERE id_hash = ?').run(encrypt(this.key, tokens), sha256(id));
  }

  deleteSession(id) {
    if (id) this.db.prepare('DELETE FROM sessions WHERE id_hash = ?').run(sha256(id));
  }

  // -- proposals ----------------------------------------------------------

  saveProposal(login, proposal, fingerprint) {
    const id = randomToken(12);
    this.db
      .prepare(`INSERT INTO proposals (id, login, data, fingerprint, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)`)
      .run(id, login, JSON.stringify(proposal), fingerprint, Date.now());
    return id;
  }

  /** Only the person who made a proposal can see it. */
  getProposal(id, login) {
    const row = this.db.prepare('SELECT * FROM proposals WHERE id = ? AND login = ?').get(id, login);
    return row ? { ...row, proposal: JSON.parse(row.data) } : null;
  }

  /** Atomically pending -> applying, so a double click can't apply twice. Expires after 1h. */
  claimProposal(id, login) {
    const result = this.db
      .prepare(`UPDATE proposals SET status = 'applying'
                WHERE id = ? AND login = ? AND status = 'pending' AND created_at > ?`)
      .run(id, login, Date.now() - HOUR);
    return result.changes === 1;
  }

  finishProposal(id, status, prUrl = null) {
    this.db.prepare('UPDATE proposals SET status = ?, pr_url = ? WHERE id = ?').run(status, prUrl, id);
  }

  proposalsLastHour(login) {
    return this.db
      .prepare('SELECT COUNT(*) AS n FROM proposals WHERE login = ? AND created_at > ?')
      .get(login, Date.now() - HOUR).n;
  }

  // -- audit --------------------------------------------------------------

  record(login, event, detail = {}) {
    this.db.prepare('INSERT INTO audit (login, event, detail, created_at) VALUES (?, ?, ?, ?)')
      .run(login, event, JSON.stringify(detail), new Date().toISOString());
  }

  events(login, limit = 100) {
    return this.db
      .prepare('SELECT event, detail, created_at FROM audit WHERE login = ? ORDER BY id DESC LIMIT ?')
      .all(login, limit)
      .map((e) => ({ event: e.event, detail: JSON.parse(e.detail), at: e.created_at }));
  }
}
