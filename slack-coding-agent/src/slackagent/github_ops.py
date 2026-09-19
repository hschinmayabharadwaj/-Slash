"""GitHub App authentication and repository operations."""

import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import jwt
import requests

from .config import GitHubConfig
from .security import SecurityError


class GitHubAuthError(Exception):
    """Raised when GitHub authentication fails."""

    pass


class GitHubOperationError(Exception):
    """Raised when a GitHub operation fails."""

    pass


class GitHubClient:
    """GitHub API client with App authentication."""

    def __init__(self, config: GitHubConfig):
        """Initialize GitHub client.

        Args:
            config: GitHub configuration
        """
        self.config = config
        self._installation_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None

    def _generate_jwt(self) -> str:
        """Generate JWT for GitHub App authentication.

        Returns:
            JWT token

        Raises:
            GitHubAuthError: If JWT generation fails
        """
        try:
            # Read private key
            with open(self.config.private_key_path, "r") as f:
                private_key = f.read()

            # Create JWT payload
            now = int(time.time())
            payload = {
                "iat": now - 60,  # Issued 60 seconds in the past to account for clock drift
                "exp": now + (10 * 60),  # Expires in 10 minutes
                "iss": self.config.app_id,  # GitHub App ID
            }

            # Generate JWT
            token = jwt.encode(payload, private_key, algorithm="RS256")
            return token

        except Exception as e:
            raise GitHubAuthError(f"Failed to generate JWT: {e}")

    def _get_installation_token(self) -> str:
        """Get or refresh installation access token.

        Returns:
            Installation access token

        Raises:
            GitHubAuthError: If token retrieval fails
        """
        # Return cached token if still valid
        if (
            self._installation_token
            and self._token_expires_at
            and datetime.utcnow() < self._token_expires_at - timedelta(minutes=5)
        ):
            return self._installation_token

        # Generate JWT for authentication
        jwt_token = self._generate_jwt()

        # Request installation access token
        url = f"https://api.github.com/app/installations/{self.config.installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            response = requests.post(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()

            self._installation_token = data["token"]
            # Token expires in 1 hour, cache with some buffer
            self._token_expires_at = datetime.utcnow() + timedelta(minutes=55)

            return self._installation_token

        except Exception as e:
            raise GitHubAuthError(f"Failed to get installation token: {e}")

    def _make_request(
        self, method: str, url: str, json_data: Optional[Dict] = None
    ) -> requests.Response:
        """Make authenticated GitHub API request.

        Args:
            method: HTTP method
            url: API URL
            json_data: Optional JSON body

        Returns:
            Response object

        Raises:
            GitHubOperationError: If request fails
        """
        token = self._get_installation_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            response = requests.request(
                method, url, headers=headers, json=json_data, timeout=30
            )
            response.raise_for_status()
            return response
        except Exception as e:
            raise GitHubOperationError(f"GitHub API request failed: {e}")

    def create_branch(self, repo_owner: str, repo_name: str, branch_name: str) -> None:
        """Create a new branch from the default branch.

        Args:
            repo_owner: Repository owner
            repo_name: Repository name
            branch_name: New branch name

        Raises:
            GitHubOperationError: If branch creation fails
        """
        # Get default branch SHA
        url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/git/refs/heads/main"
        try:
            response = self._make_request("GET", url)
            default_sha = response.json()["object"]["sha"]
        except GitHubOperationError:
            # Try 'master' if 'main' doesn't exist
            url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/git/refs/heads/master"
            response = self._make_request("GET", url)
            default_sha = response.json()["object"]["sha"]

        # Create new branch
        url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/git/refs"
        data = {"ref": f"refs/heads/{branch_name}", "sha": default_sha}

        self._make_request("POST", url, data)

    def create_pull_request(
        self,
        repo_owner: str,
        repo_name: str,
        head_branch: str,
        title: str,
        body: str,
        base_branch: str = "main",
    ) -> str:
        """Create a pull request.

        Args:
            repo_owner: Repository owner
            repo_name: Repository name
            head_branch: Source branch
            title: PR title
            body: PR body
            base_branch: Target branch (default: main)

        Returns:
            Pull request URL

        Raises:
            GitHubOperationError: If PR creation fails
        """
        url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls"
        data = {
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "body": body,
        }

        response = self._make_request("POST", url, data)
        return response.json()["html_url"]

    def get_clone_url(self, repo_owner: str, repo_name: str) -> str:
        """Get authenticated clone URL for a repository.

        Args:
            repo_owner: Repository owner
            repo_name: Repository name

        Returns:
            Clone URL with embedded token
        """
        token = self._get_installation_token()
        return f"https://x-access-token:{token}@github.com/{repo_owner}/{repo_name}.git"


class GitOperations:
    """Git command execution with security hardening."""

    @staticmethod
    def clone_repository(
        clone_url: str, dest_path: Path, branch: Optional[str] = None
    ) -> None:
        """Clone a repository.

        Args:
            clone_url: Repository clone URL
            dest_path: Destination directory
            branch: Optional branch to checkout

        Raises:
            GitHubOperationError: If clone fails
        """
        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([clone_url, str(dest_path)])

        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as e:
            raise GitHubOperationError(f"Failed to clone repository: {e.stderr}")
        except subprocess.TimeoutExpired:
            raise GitHubOperationError("Repository clone timed out")

    @staticmethod
    def create_branch(repo_path: Path, branch_name: str) -> None:
        """Create and checkout a new git branch.

        Args:
            repo_path: Repository path
            branch_name: Branch name

        Raises:
            GitHubOperationError: If branch creation fails
        """
        try:
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise GitHubOperationError(f"Failed to create branch: {e.stderr}")

    @staticmethod
    def commit_changes(
        repo_path: Path, commit_message: str, author_name: str = "Slack Coding Agent",
        author_email: str = "bot@example.com"
    ) -> None:
        """Commit all changes in the repository.

        Args:
            repo_path: Repository path
            commit_message: Commit message
            author_name: Git author name
            author_email: Git author email

        Raises:
            GitHubOperationError: If commit fails
        """
        try:
            # Configure git user
            subprocess.run(
                ["git", "config", "user.name", author_name],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", author_email],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )

            # Add all changes
            subprocess.run(
                ["git", "add", "-A"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )

            # Commit
            subprocess.run(
                ["git", "commit", "-m", commit_message],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise GitHubOperationError(f"Failed to commit changes: {e.stderr}")

    @staticmethod
    def push_branch(repo_path: Path, branch_name: str) -> None:
        """Push a branch to remote.

        Args:
            repo_path: Repository path
            branch_name: Branch name

        Raises:
            GitHubOperationError: If push fails
        """
        try:
            subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as e:
            raise GitHubOperationError(f"Failed to push branch: {e.stderr}")
        except subprocess.TimeoutExpired:
            raise GitHubOperationError("Branch push timed out")

    @staticmethod
    def get_diff(repo_path: Path, cached: bool = True) -> str:
        """Get git diff output.

        Args:
            repo_path: Repository path
            cached: If True, get staged diff; otherwise get unstaged diff

        Returns:
            Diff output

        Raises:
            GitHubOperationError: If diff fails
        """
        try:
            cmd = ["git", "diff"]
            if cached:
                cmd.append("--cached")

            result = subprocess.run(
                cmd,
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise GitHubOperationError(f"Failed to get diff: {e.stderr}")

    @staticmethod
    def has_changes(repo_path: Path) -> bool:
        """Check if repository has uncommitted changes.

        Args:
            repo_path: Repository path

        Returns:
            True if there are changes, False otherwise
        """
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
            return bool(result.stdout.strip())
        except subprocess.CalledProcessError:
            return False
