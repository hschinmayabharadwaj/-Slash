"""Deterministic guardrails for Gemini-based agent. These run in plain code, outside the model's control.

The agent proposes; these checks (plus CI and human review) decide.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Tuple

# --------------------------------------------------------------------------- path patterns


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Convert glob pattern to regex for efficient matching."""
    i, n, out = 0, len(pattern), []
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches_any(path: str, patterns: list[str]) -> bool:
    """True if `path` matches any deny pattern (case-insensitive).

    Patterns without a leading ``**/`` but containing ``/`` are treated as
    relative — they can match at any depth in the path tree.  For example,
    ``secrets/**`` will match ``app/secrets/file.txt`` as well as
    ``secrets/file.txt``.
    """
    p = path.lower()
    while p.startswith("./"):  # strip a literal "./" prefix only
        p = p[2:]
    for pat in patterns:
        pl = pat.lower()
        if "/" not in pl:
            # No slash → match against the basename only
            if fnmatch.fnmatchcase(p.rsplit("/", 1)[-1], pl):
                return True
        else:
            if _glob_regex(pl).match(p):
                return True
            # Also try as a relative (non-rooted) pattern so that e.g.
            # "secrets/**" matches "app/secrets/file.txt".
            if not pl.startswith("**/") and _glob_regex("**/" + pl).match(p):
                return True
    return False


# --------------------------------------------------------------------------- diff inspection


@dataclass
class FileChange:
    """Represents a file change from git diff."""
    path: str
    status: str = "M"  # M=modified, A=added, D=deleted
    mode: str = "100644"
    added: int = 0
    deleted: int = 0
    binary: bool = False


@dataclass
class DiffReport:
    """Summary of git diff changes."""
    files: List[FileChange] = field(default_factory=list)
    added_lines: List[str] = field(default_factory=list)

    @property
    def total_changed(self) -> int:
        """Total lines changed across all files."""
        return sum(f.added + f.deleted for f in self.files)


_MAX_PATCH_CHARS = 2_000_000


def inspect_worktree(repo_dir: str | Path) -> DiffReport:
    """Stage everything the agent changed and summarize it. Stages with `git add -A`."""
    from .gitutil import run_git
    
    run_git(repo_dir, "add", "-A")
    common = ["diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv"]
    raw = run_git(repo_dir, *common, "--raw", "-z")
    stat = run_git(repo_dir, *common, "--numstat", "-z")
    patch = run_git(repo_dir, *common, "-U0", "--no-color")

    files: dict[str, FileChange] = {}
    tokens = raw.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith(":") and i + 1 < len(tokens):
            meta = tok[1:].split()  # oldmode newmode oldsha newsha status
            path = tokens[i + 1]
            files[path] = FileChange(path=path, status=meta[4][:1], mode=meta[1])
            i += 2
        else:
            i += 1

    for entry in stat.split("\0"):
        if not entry:
            continue
        added, deleted, path = entry.split("\t", 2)
        change = files.setdefault(path, FileChange(path=path))
        if added == "-" or deleted == "-":
            change.binary = True
        else:
            change.added, change.deleted = int(added), int(deleted)

    added_lines = [
        line[1:]
        for line in patch[:_MAX_PATCH_CHARS].splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    return DiffReport(files=list(files.values()), added_lines=added_lines)


def check_policy(
    report: DiffReport, 
    *, 
    max_files: int, 
    max_changed_lines: int, 
    deny_patterns: list[str]
) -> list[str]:
    """Return human-readable violations (empty list = OK)."""
    problems: list[str] = []
    
    if len(report.files) > max_files:
        problems.append(f"it changes {len(report.files)} files (limit is {max_files})")
    
    if report.total_changed > max_changed_lines:
        problems.append(
            f"it changes {report.total_changed} lines (limit is {max_changed_lines})"
        )
    
    protected = [f.path for f in report.files if matches_any(f.path, deny_patterns)]
    if protected:
        shown = ", ".join(f"`{p}`" for p in protected[:5])
        problems.append(f"it touches protected paths: {shown}")
    
    special = [f.path for f in report.files if f.mode in ("120000", "160000")]
    if special:
        problems.append(f"it adds a symlink or submodule: `{special[0]}`")
    
    binary = [f.path for f in report.files if f.binary]
    if binary:
        problems.append(f"it includes a binary file: `{binary[0]}`")
    
    return problems


# --------------------------------------------------------------------------- secret scan

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "a Google/Gemini API key": re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    "an AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "a private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "a Slack token": re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"),
    "a GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[0-9A-Za-z_]{20,}\b"),
    "an Anthropic API key": re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,}\b"),
    "a hard-coded secret": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['\"][^'\"\s]{16,}['\"]"
    ),
    "a database connection string": re.compile(
        r"\b(?:postgres|mysql|mongodb)://[^:]+:[^@]+@[^/]+"
    ),
}


def scan_secrets(added_lines: list[str]) -> list[str]:
    """Names of secret types found. Never returns the secret itself."""
    found: list[str] = []
    for name, pattern in SECRET_PATTERNS.items():
        if any(pattern.search(line) for line in added_lines):
            found.append(f"it appears to add {name}")
    return found


# --------------------------------------------------------------------------- text safety

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_OUR_TAGS = re.compile(
    r"</?\s*(?:request|slack_thread|repo_conventions|plan)\b[^>]*>", re.IGNORECASE
)


def sanitize_untrusted(text: str, max_chars: int) -> str:
    """Prepare user/Slack/repo text to be embedded as DATA in a prompt."""
    text = _CONTROL.sub("", text or "")
    text = _OUR_TAGS.sub("[tag removed]", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[truncated]"
    return text


def escape_slack(text: str) -> str:
    """Escape model- or user-provided text before posting to Slack (blocks @channel pings)."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def neutralize_github_mentions(text: str) -> str:
    """Stop model-written PR text from @-mentioning (and notifying) people or teams."""
    return (text or "").replace("@", "@\u200b")


def slugify(text: str, max_len: int = 40) -> str:
    """Create a URL-safe slug from text."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].strip("-") or "change"


def tail(text: str, max_chars: int = 1500) -> str:
    """Return the tail of text, truncated to max_chars."""
    text = (text or "").strip()
    return text if len(text) <= max_chars else "…" + text[-max_chars:]


# --------------------------------------------------------------------------- kill switch


def kill_switch_on(config) -> bool:
    """True if BOT_DISABLED is set or the kill-switch file exists (no restart needed)."""
    if os.environ.get("BOT_DISABLED", "").lower() in {"1", "true", "yes"}:
        return True
    
    kill_switch_file = getattr(config.security, 'kill_switch_file', None)
    if kill_switch_file and Path(kill_switch_file).exists():
        return True
    
    return False


# --------------------------------------------------------------------------- Backwards compatibility with SecurityManager


class SecurityError(Exception):
    """Raised when a security policy is violated."""
    pass


class PathGuard:
    """Enforces path-based access controls (compatibility layer)."""

    def __init__(self, protected_patterns: List[str]):
        self.protected_patterns = protected_patterns

    def is_path_protected(self, path: str) -> bool:
        """Check if a path matches any protected pattern."""
        return matches_any(path, self.protected_patterns)

    def check_path_access(self, path: str, operation: str = "access") -> None:
        """Check if path access is allowed, raise if not."""
        if self.is_path_protected(path):
            raise SecurityError(
                f"Access denied: Cannot {operation} protected path '{path}'"
            )


class SecretScanner:
    """Scans text and diffs for potential secrets (compatibility layer)."""

    def scan(self, text: str) -> List[Tuple[str, str]]:
        """Scan text for potential secrets."""
        findings = []
        for name, pattern in SECRET_PATTERNS.items():
            matches = pattern.finditer(text)
            for match in matches:
                findings.append((name, match.group(0)))
        return findings

    def scan_diff(self, diff_text: str) -> List[Tuple[str, str, str]]:
        """Scan a git diff for secrets in added lines."""
        findings = []
        current_file = None

        for line in diff_text.split("\n"):
            if line.startswith("diff --git"):
                parts = line.split()
                if len(parts) >= 4:
                    current_file = parts[2][2:]  # Remove "a/" prefix
            elif line.startswith("+") and not line.startswith("+++"):
                line_content = line[1:]
                secrets = self.scan(line_content)
                for secret_type, matched in secrets:
                    findings.append((current_file or "unknown", secret_type, matched))

        return findings


class SecurityManager:
    """Centralized security management (compatibility layer)."""

    def __init__(self, config):
        self.config = config
        self.path_guard = PathGuard(config.protected_paths)
        self.secret_scanner = SecretScanner()

    def check_file_access(self, path: str, operation: str = "access") -> None:
        """Check if file access is allowed."""
        self.path_guard.check_path_access(path, operation)

        if Path(path).exists():
            file_size_mb = Path(path).stat().st_size / (1024 * 1024)
            if file_size_mb > self.config.max_file_size_mb:
                raise SecurityError(
                    f"File too large: {file_size_mb:.1f}MB exceeds "
                    f"limit of {self.config.max_file_size_mb}MB"
                )

    def scan_diff_for_secrets(self, diff_text: str) -> str | None:
        """Scan a diff for secrets."""
        if not self.config.scan_secrets:
            return None

        findings = self.secret_scanner.scan_diff(diff_text)
        if findings:
            messages = []
            for file_path, secret_type, matched in findings:
                redacted = matched[:8] + "..." if len(matched) > 8 else "***"
                messages.append(f"  - {file_path}: {secret_type} ({redacted})")

            return (
                "⚠️ **Potential secrets detected in changes:**\n"
                + "\n".join(messages)
                + "\n\nPlease remove sensitive data before committing."
            )

        return None
