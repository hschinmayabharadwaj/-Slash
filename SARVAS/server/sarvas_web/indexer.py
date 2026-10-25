"""Local repository index for Ask mode.

Each repository gets its own directory and its own SQLite database under
index.dir (default data/repos/<owner>/<name>/), so queries can never cross
repositories or organizations. Everything stays on this machine:

  checkout/  shallow clone of the default branch (no credentials stored:
             the token reaches git only through GIT_CONFIG_* env vars)
  index.db   files, line-numbered chunks in a full-text index, and an
             overview (languages, layout, dependency manifests)

Updates are incremental: fetch the new head, diff it against the indexed
commit, re-index only changed files. Search results are re-read from the
current checkout, so stale index text is never returned as fact.

Never indexed: protected paths (.env*, keys, secrets/...), symlinks, binary
or oversized files, vendored/build directories. Secret-looking values in
indexed text are redacted.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from contextlib import closing
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
BRANCH_RE = re.compile(r"^(?!.*\.\.)(?!/)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<![./])$")
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", ".venv", "venv", "__pycache__",
             ".next", ".nuxt", ".idea", ".vscode", "coverage", ".gradle", ".terraform", "bower_components"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "go.sum",
              "composer.lock", "Gemfile.lock"}
STOPWORDS = set("""a an and are as at be but by can could do does did for from has have how i if in into is it its
of on or our please should so than that the their them then there these they this to us was we were what when
where which who why will with would you your about any all also code codebase project repo repository file files
show tell explain find work works working used use using make makes need want get gets""".split())
SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*)(['\"])[^'\"\s]{8,}\2"),
]
MANIFESTS = ("package.json", "requirements.txt", "pyproject.toml", "go.mod", "Cargo.toml", "pom.xml",
             "build.gradle", "Gemfile", "composer.json", "Dockerfile", "docker-compose.yml", "Makefile")


class IndexError_(Exception):
    """User-facing indexing problem (message has no secrets)."""


def validate_name(owner: str, name: str) -> None:
    for part in (owner, name):
        if not NAME_RE.match(part or "") or part in (".", ".."):
            raise IndexError_(f"Invalid repository name: {owner}/{name}")


def redact(text: str) -> str:
    for rx in SECRET_RES:
        text = rx.sub(lambda m: (m.group(1) + m.group(2) + "[redacted]" + m.group(2)) if m.re.groups >= 2
                      else "[redacted]", text)
    return text


def path_denied(path: str, patterns: list[str]) -> bool:
    """Same semantics as the Go guards: no '/' -> basename match; '**/' = any depth."""
    p = path.lower()
    while p.startswith("./"):
        p = p[2:]
    base = PurePosixPath(p).name
    for pat in patterns:
        pl = pat.strip().lower()
        if not pl:
            continue
        if "/" not in pl:
            if fnmatch(base, pl):
                return True
            continue
        rx = _glob_re(pl)
        if rx.match(p) or (not pl.startswith("**/") and _glob_re("**/" + pl).match(p)):
            return True
    return False


_GLOBS: dict[str, re.Pattern] = {}


def _glob_re(pattern: str) -> re.Pattern:
    if pattern not in _GLOBS:
        out, i = "", 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out, i = out + "(?:.*/)?", i + 3
            elif pattern.startswith("**", i):
                out, i = out + ".*", i + 2
            elif pattern[i] == "*":
                out, i = out + "[^/]*", i + 1
            elif pattern[i] == "?":
                out, i = out + "[^/]", i + 1
            else:
                out, i = out + re.escape(pattern[i]), i + 1
        _GLOBS[pattern] = re.compile("^" + out + "$")
    return _GLOBS[pattern]


def split_words(text: str) -> list[str]:
    """Identifier-aware words: getUserName / get_user_name -> get user name."""
    words: list[str] = []
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9]*", text):
        words.extend(p.lower() for p in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", tok) if len(p) > 1)
    return words


def stem(word: str) -> str:
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def query_terms(question: str) -> list[str]:
    seen, terms = set(), []
    for w in split_words(question):
        s = stem(w)
        if len(s) >= 3 and w not in STOPWORDS and s not in seen:
            seen.add(s)
            terms.append(s)
    return terms[:16]


def git_auth_env(token: str) -> dict:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}


_TOKEN_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]+|basic [A-Za-z0-9+/=]+|x-access-token:[^@\s]+)")


def _git(args: list[str], cwd: Path | None = None, env: dict | None = None, timeout: int = 300,
         allow_file: bool = False) -> str:
    safe = ["-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
            "-c", "core.symlinks=false", "-c", "submodule.recurse=false"]
    if allow_file:
        safe += ["-c", "protocol.file.allow=always"]
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_ASKPASS": "/bin/false",
                **(env or {})}
    try:
        res = subprocess.run(["git", *safe, *args], cwd=cwd, env=full_env, capture_output=True, text=True,
                             timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise IndexError_(f"git {args[0]} timed out") from None
    except FileNotFoundError:
        raise IndexError_("git is not installed on the server") from None
    if res.returncode != 0:
        msg = _TOKEN_RE.sub("***", res.stderr.strip())[-300:]
        raise IndexError_(f"git {args[0]} failed: {msg}")
    return res.stdout


SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, lang TEXT, size INTEGER, lines INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
  path, start_line UNINDEXED, end_line UNINDEXED, body, words, tokenize = 'unicode61');
"""


class RepoIndex:
    def __init__(self, root: str | Path, owner: str, name: str, settings: dict, deny: list[str],
                 remote: str | None = None, allow_file_remote: bool = False):
        validate_name(owner, name)
        self.owner, self.name = owner, name
        self.dir = Path(root) / owner.lower() / name.lower()
        self.checkout = self.dir / "checkout"
        self.db_path = self.dir / "index.db"
        self.remote = remote or f"https://github.com/{owner}/{name}.git"
        self.allow_file = allow_file_remote
        self.settings, self.deny = settings, deny
        self.lock = threading.Lock()

    # -- storage ------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        return db

    def meta(self) -> dict:
        if not self.db_path.exists():
            return {}
        with closing(self._db()) as db:
            return {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key, value FROM meta")}

    def status(self) -> dict:
        m = self.meta()
        if not m.get("commit"):
            return {"state": "missing"}
        return {"state": "ready", "commit": m["commit"], "branch": m.get("branch"), "indexedAt": m.get("indexedAt"),
                "files": m.get("files", 0), "chunks": m.get("chunks", 0), "overview": m.get("overview", {})}

    # -- sync ---------------------------------------------------------------

    def sync(self, token: str | None, branch: str, head_sha: str | None = None) -> dict:
        """Bring the checkout and index to the branch head. Returns what changed."""
        if not BRANCH_RE.match(branch or ""):
            raise IndexError_("Invalid branch name.")
        env = git_auth_env(token) if token else None
        with self.lock:
            m = self.meta()
            if head_sha and m.get("commit") == head_sha and m.get("branch") == branch and self.checkout.is_dir():
                return {"mode": "unchanged", "changed": 0}
            if not (self.checkout / ".git").is_dir() or m.get("branch") != branch or not m.get("commit"):
                return self._full(env, branch)
            _git(["fetch", "--depth", "1", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                 cwd=self.checkout, env=env, allow_file=self.allow_file)
            new = _git(["rev-parse", f"refs/remotes/origin/{branch}"], cwd=self.checkout).strip()
            if new == m["commit"]:
                return {"mode": "unchanged", "changed": 0}
            try:
                raw = _git(["diff", "--name-status", "--no-renames", "-z", m["commit"], new], cwd=self.checkout)
            except IndexError_:
                return self._full(env, branch)  # old commit gone: start over
            _git(["checkout", "-q", "--force", "--detach", new], cwd=self.checkout)
            parts = [p for p in raw.split("\0") if p]
            changed = sorted({parts[i + 1] for i in range(0, len(parts) - 1, 2)})
            with closing(self._db()) as db:
                db.execute("BEGIN")
                for path in changed:
                    db.execute("DELETE FROM chunks WHERE path = ?", (path,))
                    db.execute("DELETE FROM files WHERE path = ?", (path,))
                    self._index_file(db, path)
                self._finish(db, new, branch)
                db.execute("COMMIT")
            return {"mode": "incremental", "changed": len(changed)}

    def _full(self, env: dict | None, branch: str) -> dict:
        tmp = self.dir / "checkout.new"
        _rmtree(tmp)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _git(["clone", "--depth", "1", "--single-branch", "--no-tags", "--no-recurse-submodules", "--branch", branch,
              self.remote, str(tmp)], env=env, allow_file=self.allow_file)
        _rmtree(self.checkout)
        tmp.rename(self.checkout)
        commit = _git(["rev-parse", "HEAD"], cwd=self.checkout).strip()
        # Keep the fetch refspec explicit so incremental updates use the same ref.
        _git(["update-ref", f"refs/remotes/origin/{branch}", commit], cwd=self.checkout)
        paths = [p for p in _git(["ls-files", "-z"], cwd=self.checkout).split("\0") if p]
        with closing(self._db()) as db:
            db.execute("BEGIN")
            db.execute("DELETE FROM chunks")
            db.execute("DELETE FROM files")
            for path in paths[: int(self.settings["maxFiles"]) * 4]:
                self._index_file(db, path)
            self._finish(db, commit, branch)
            db.execute("COMMIT")
        return {"mode": "full", "changed": len(paths)}

    def _readable(self, path: str) -> Path | None:
        """The file inside the checkout, or None if it must not be indexed/read."""
        parts = PurePosixPath(path).parts
        if not parts or path.startswith("/") or ".." in parts or any(p in SKIP_DIRS for p in parts[:-1]):
            return None
        if parts[-1] in SKIP_FILES or path_denied(path, self.deny):
            return None
        target = self.checkout / path
        try:
            st = target.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_size > int(self.settings["maxFileKb"]) * 1024:
            return None
        if not target.resolve().is_relative_to(self.checkout.resolve()):
            return None
        return target

    def _read_lines(self, path: str) -> list[str] | None:
        target = self._readable(path)
        if target is None:
            return None
        data = target.read_bytes()
        if b"\0" in data[:8192]:
            return None
        return redact(data.decode("utf-8", "replace")).split("\n")

    def _index_file(self, db: sqlite3.Connection, path: str) -> None:
        if db.execute("SELECT COUNT(*) FROM files").fetchone()[0] >= int(self.settings["maxFiles"]):
            return
        lines = self._read_lines(path)
        if lines is None:
            return
        db.execute("INSERT OR REPLACE INTO files (path, lang, size, lines) VALUES (?, ?, ?, ?)",
                   (path, PurePosixPath(path).suffix.lstrip(".").lower(), sum(len(x) + 1 for x in lines), len(lines)))
        size = max(10, int(self.settings["chunkLines"]))
        step = max(1, size - size // 6)
        path_words = " ".join(split_words(path))
        for start in range(0, max(1, len(lines)), step):
            body = "\n".join(lines[start: start + size])
            if not body.strip():
                continue
            end = min(len(lines), start + size)
            db.execute("INSERT INTO chunks (path, start_line, end_line, body, words) VALUES (?, ?, ?, ?, ?)",
                       (path, start + 1, end, body, path_words + " " + " ".join(split_words(body))))
            if start + size >= len(lines):
                break

    def _finish(self, db: sqlite3.Connection, commit: str, branch: str) -> None:
        files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        overview = self._overview(db)
        for key, value in {"commit": commit, "branch": branch, "files": files, "chunks": chunks,
                           "indexedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "overview": overview}.items():
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # -- overview (architecture + dependencies) -----------------------------

    def _overview(self, db: sqlite3.Connection) -> dict:
        langs = {r["lang"] or "(none)": r["n"] for r in db.execute(
            "SELECT lang, COUNT(*) AS n FROM files GROUP BY lang ORDER BY n DESC LIMIT 8")}
        top: dict[str, int] = {}
        for (path,) in db.execute("SELECT path FROM files"):
            first = path.split("/")[0] + ("/" if "/" in path else "")
            top[first] = top.get(first, 0) + 1
        manifests: dict[str, list[str]] = {}
        for (path,) in db.execute("SELECT path FROM files"):
            if PurePosixPath(path).name in MANIFESTS and path.count("/") <= 2 and len(manifests) < 12:
                manifests[path] = self._dependencies(path)
        readme = next((p for (p,) in db.execute("SELECT path FROM files WHERE path LIKE 'readme%' COLLATE NOCASE")
                       if "/" not in p), None)
        summary = ""
        if readme and (lines := self._read_lines(readme)):
            summary = "\n".join(lines[:15])[:1500]
        return {"languages": langs, "layout": dict(sorted(top.items())[:40]), "manifests": manifests,
                "readme": summary}

    def _dependencies(self, path: str) -> list[str]:
        lines = self._read_lines(path) or []
        name = PurePosixPath(path).name
        text = "\n".join(lines)
        deps: list[str] = []
        try:
            if name in ("package.json", "composer.json"):
                data = json.loads(text)
                for key in ("dependencies", "devDependencies", "require"):
                    deps += list((data.get(key) or {}).keys())
            elif name == "requirements.txt":
                deps = [re.split(r"[<>=!~\[; ]", ln.strip())[0] for ln in lines
                        if ln.strip() and not ln.startswith(("#", "-"))]
            elif name == "go.mod":
                deps = re.findall(r"^\s*([\w./-]+\.[\w./-]+)\s+v[\w.+-]+", text, re.M)
            elif name == "pyproject.toml":
                for arr in re.findall(r"(?s)dependencies\s*=\s*\[(.*?)\]", text):
                    deps += re.findall(r'["\']([A-Za-z0-9_.-]+)', arr)
            elif name == "Cargo.toml":
                block = re.search(r"(?ms)^\[dependencies\]\s*$(.*?)(?=^\[|\Z)", text)
                deps = re.findall(r"^([A-Za-z0-9_-]+)\s*=", block.group(1), re.M) if block else []
        except (ValueError, IndexError):
            pass
        return sorted(set(d for d in deps if d))[:40]

    def overview_text(self) -> str:
        o = self.meta().get("overview") or {}
        parts = [f"Repository {self.owner}/{self.name}"]
        if o.get("languages"):
            parts.append("File types: " + ", ".join(f"{k} ({v})" for k, v in o["languages"].items()))
        if o.get("layout"):
            parts.append("Top-level layout: " + ", ".join(f"{k} ({v} files)" for k, v in o["layout"].items()))
        for path, deps in (o.get("manifests") or {}).items():
            parts.append(f"{path}: " + (", ".join(deps) if deps else "(present)"))
        if o.get("readme"):
            parts.append("README start:\n" + o["readme"])
        return "\n".join(parts)

    # -- search -------------------------------------------------------------

    def search(self, question: str, k: int = 8) -> dict:
        """Best chunks for the question, re-read from the current checkout."""
        terms = query_terms(question)
        if not terms or not self.db_path.exists():
            return {"terms": terms, "hits": [], "coverage": 0.0}
        match = " OR ".join(f'"{t}"*' for t in terms)
        with closing(self._db()) as db:
            rows = db.execute("""SELECT path, start_line, end_line, bm25(chunks, 3.0, 0, 0, 1.0, 0.6) AS score
                                 FROM chunks WHERE chunks MATCH ? ORDER BY score LIMIT ?""",
                              (match, k * 4)).fetchall()
        hits: list[dict] = []
        best, per_file = 0.0, dict[str, int]()
        for r in rows:
            if per_file.get(r["path"], 0) >= 2:
                continue
            lines = self._read_lines(r["path"])  # verify against the current files
            if lines is None:
                continue
            start, end = int(r["start_line"]), min(int(r["end_line"]), len(lines))
            text = "\n".join(lines[start - 1: end])
            hay = set(stem(w) for w in split_words(r["path"] + " " + text))
            matched = [t for t in terms if any(h.startswith(t) for h in hay)]
            if not matched:
                continue
            coverage = len(matched) / len(terms)
            best = max(best, coverage)
            per_file[r["path"]] = per_file.get(r["path"], 0) + 1
            hits.append({"path": r["path"], "startLine": start, "endLine": end, "text": text,
                         "matched": matched, "coverage": round(coverage, 2)})
            if len(hits) >= k:
                break
        return {"terms": terms, "hits": hits, "coverage": round(best, 2)}

    def remove(self) -> None:
        with self.lock:
            _rmtree(self.dir)


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


class Indexes:
    """One RepoIndex per repository, plus background (re)index jobs."""

    def __init__(self, config: dict, remote_for=None, allow_file_remote: bool = False):
        self.config = config
        self.remote_for = remote_for  # tests: (owner, name) -> local path
        self.allow_file = allow_file_remote
        self.cache: dict[str, RepoIndex] = {}
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def get(self, owner: str, name: str) -> RepoIndex:
        key = f"{owner}/{name}".lower()
        with self.lock:
            if key not in self.cache:
                self.cache[key] = RepoIndex(self.config["index"]["dir"], owner, name, self.config["index"],
                                            self.config["security"]["protectedPaths"],
                                            self.remote_for(owner, name) if self.remote_for else None,
                                            self.allow_file)
            return self.cache[key]

    def job(self, owner: str, name: str) -> dict | None:
        return self.jobs.get(f"{owner}/{name}".lower())

    def start(self, owner: str, name: str, token: str, branch: str, head: str | None = None) -> dict:
        """Run sync in the background (one job per repo at a time)."""
        key = f"{owner}/{name}".lower()
        with self.lock:
            if (j := self.jobs.get(key)) and j["state"] == "indexing":
                return j
            job = self.jobs[key] = {"state": "indexing", "startedAt": time.time()}
        idx = self.get(owner, name)

        def run():
            try:
                job.update(idx.sync(token, branch, head), state="done")
            except IndexError_ as e:
                job.update(state="error", error=str(e))
            except Exception:  # noqa: BLE001 -- never leak details (may include paths)
                job.update(state="error", error="Indexing failed unexpectedly.")

        threading.Thread(target=run, daemon=True).start()
        return job
