"""GitHub App authentication and git operations.

Hardening notes:

* Installation tokens are requested per repository with only the permissions
  the bot needs (contents + pull requests write), not installation-wide.
* Tokens are never embedded in clone URLs, so they are never written to
  ``.git/config`` (which is mounted into the sandbox). Git receives them via
  ``GIT_CONFIG_*`` environment variables only for the clone/push call itself.
* Every git call disables hooks and uses explicit timeouts.
"""

import base64
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import jwt
import requests

from .config import GitHubConfig

GITHUB_API = "https://api.github.com"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

# Hooks never run, and only the protocols we use are allowed.
_SAFE_GIT = ["-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
             "-c", "protocol.https.allow=always", "-c", "protocol.file.allow=always"]


class GitHubAuthError(Exception):
    """Raised when GitHub authentication fails."""


class GitHubOperationError(Exception):
    """Raised when a GitHub or git operation fails."""


def validate_repo_name(owner: str, name: str) -> None:
    """Reject owner/repo values that could be abused in URLs or paths."""
    for part in (owner, name):
        if not _NAME_RE.match(part or "") or part in (".", ".."):
            raise GitHubOperationError(f"Invalid repository name: {owner}/{name}")


def _redact(text: str) -> str:
    """Remove anything that looks like a token from git error output."""
    text = re.sub(r"(x-access-token:)[^@\s]+", r"\1***", text or "")
    return re.sub(r"\bgh[spousr]_[A-Za-z0-9_]{10,}", "gh*_***", text)


class GitHubClient:
    """GitHub API client with App authentication."""

    def __init__(self, config: GitHubConfig):
        self.config = config
        self._tokens: Dict[str, Tuple[str, datetime]] = {}
        self._lock = threading.Lock()

    def _generate_jwt(self) -> str:
        """Generate a short-lived JWT for GitHub App authentication."""
        try:
            private_key = Path(self.config.private_key_path).read_text()
            now = int(time.time())
            payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": str(self.config.app_id)}
            return jwt.encode(payload, private_key, algorithm="RS256")
        except Exception as e:
            raise GitHubAuthError(f"Failed to generate JWT: {e}") from e

    def _get_installation_token(self, repo_name: Optional[str] = None) -> str:
        """Installation token scoped to ``repo_name`` (cached ~55 minutes)."""
        cache_key = repo_name or "*"
        with self._lock:
            cached = self._tokens.get(cache_key)
            if cached and datetime.utcnow() < cached[1]:
                return cached[0]

            body: Dict = {
                "permissions": {"contents": "write", "pull_requests": "write"},
            }
            if repo_name:
                body["repositories"] = [repo_name]

            url = f"{GITHUB_API}/app/installations/{self.config.installation_id}/access_tokens"
            headers = {
                "Authorization": f"Bearer {self._generate_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            try:
                response = requests.post(url, headers=headers, json=body, timeout=15)
                response.raise_for_status()
                token = response.json()["token"]
            except Exception as e:
                raise GitHubAuthError(f"Failed to get installation token: {_redact(str(e))}") from e

            self._tokens[cache_key] = (token, datetime.utcnow() + timedelta(minutes=55))
            return token

    def _make_request(
        self, method: str, url: str, repo_name: Optional[str] = None, json_data: Optional[Dict] = None
    ) -> requests.Response:
        """Authenticated GitHub API request."""
        headers = {
            "Authorization": f"Bearer {self._get_installation_token(repo_name)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = requests.request(method, url, headers=headers, json=json_data, timeout=30)
            response.raise_for_status()
            return response
        except Exception as e:
            raise GitHubOperationError(f"GitHub API request failed: {_redact(str(e))}") from e

    def create_pull_request(
        self,
        repo_owner: str,
        repo_name: str,
        head_branch: str,
        title: str,
        body: str,
        base_branch: str = "main",
    ) -> str:
        """Open a pull request and return its URL."""
        validate_repo_name(repo_owner, repo_name)
        url = f"{GITHUB_API}/repos/{repo_owner}/{repo_name}/pulls"
        data = {"title": title, "head": head_branch, "base": base_branch, "body": body}
        return self._make_request("POST", url, repo_name, data).json()["html_url"]

    def get_clone_url(self, repo_owner: str, repo_name: str) -> str:
        """Plain HTTPS clone URL (no credentials embedded)."""
        validate_repo_name(repo_owner, repo_name)
        return f"https://github.com/{repo_owner}/{repo_name}.git"

    def git_auth_env(self, repo_owner: str, repo_name: str) -> Dict[str, str]:
        """Environment that authenticates one git command to GitHub.

        Uses git's ``GIT_CONFIG_COUNT`` mechanism so the token lives only in
        the child process environment, never in argv or on disk.
        """
        token = self._get_installation_token(repo_name)
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }


class GitOperations:
    """Git command execution with security hardening."""

    @staticmethod
    def _run(
        args, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
        timeout: int = 60, input_text: Optional[str] = None,
    ) -> str:
        import os

        full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
        try:
            proc = subprocess.run(
                ["git", *_SAFE_GIT, *args],
                cwd=cwd,
                env=full_env,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
            return proc.stdout
        except subprocess.CalledProcessError as e:
            raise GitHubOperationError(
                f"git {args[0]} failed: {_redact(e.stderr.strip())[:500]}"
            ) from None
        except subprocess.TimeoutExpired:
            raise GitHubOperationError(f"git {args[0]} timed out") from None

    @staticmethod
    def clone_repository(
        clone_url: str,
        dest_path: Path,
        branch: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        """Shallow-clone a repository without submodules or LFS smudging."""
        args = ["clone", "--depth", "1", "--no-recurse-submodules"]
        if branch:
            args += ["--branch", branch]
        args += [clone_url, str(dest_path)]
        GitOperations._run(args, env={**(env or {}), "GIT_LFS_SKIP_SMUDGE": "1"}, timeout=180)

    @staticmethod
    def create_branch(repo_path: Path, branch_name: str) -> None:
        """Create and check out a new branch."""
        GitOperations._run(["check-ref-format", "--branch", branch_name], cwd=repo_path)
        GitOperations._run(["checkout", "-q", "-b", branch_name], cwd=repo_path)

    @staticmethod
    def apply_patch(repo_path: Path, patch: str) -> None:
        """Apply a patch to the index and worktree (fails if it doesn't apply cleanly)."""
        GitOperations._run(
            ["apply", "--index", "--whitespace=nowarn", "-"],
            cwd=repo_path,
            input_text=patch,
        )

    @staticmethod
    def commit_changes(
        repo_path: Path,
        commit_message: str,
        author_name: str = "slack-coding-agent[bot]",
        author_email: str = "slack-coding-agent@users.noreply.github.com",
    ) -> None:
        """Stage and commit everything as the bot identity."""
        GitOperations._run(["add", "-A"], cwd=repo_path)
        GitOperations._run(
            ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}",
             "commit", "-q", "--no-verify", "-m", commit_message],
            cwd=repo_path,
        )

    @staticmethod
    def push_branch(repo_path: Path, branch_name: str, env: Optional[Dict[str, str]] = None) -> None:
        """Push a new branch to origin (never force)."""
        GitOperations._run(
            ["push", "--no-verify", "origin", f"HEAD:refs/heads/{branch_name}"],
            cwd=repo_path,
            env=env,
            timeout=120,
        )

    @staticmethod
    def get_diff(repo_path: Path, cached: bool = True) -> str:
        """Diff of the worktree (``cached`` = staged changes, binary-safe)."""
        args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--binary"]
        if cached:
            args.append("--cached")
        return GitOperations._run(args, cwd=repo_path)

    @staticmethod
    def has_changes(repo_path: Path) -> bool:
        """Whether the worktree has uncommitted changes."""
        try:
            return bool(GitOperations._run(["status", "--porcelain"], cwd=repo_path).strip())
        except GitHubOperationError:
            return False
