"""GitHub sign-in (OAuth web flow) and API calls made *as the signed-in user*.

The web page uses a GitHub App's user-to-server tokens rather than a classic
OAuth App: the token can never do more than BOTH the App's permissions
(Contents + Pull requests) AND the person's own access, and only on repos
where the App is installed. Tokens expire (8h) and are refreshed server-side.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import requests

GITHUB = "https://github.com"
API = "https://api.github.com"


class GitHubUserError(Exception):
    """A GitHub call failed. ``str(e)`` is safe to show to the user."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0  # unix time; 0 = does not expire

    def expired(self, skew: float = 120) -> bool:
        return bool(self.expires_at) and time.time() > self.expires_at - skew


def _tokens(data: Dict[str, Any]) -> TokenSet:
    if "access_token" not in data:
        raise GitHubUserError(f"GitHub sign-in failed: {data.get('error_description') or data.get('error') or 'no token'}")
    expires_in = int(data.get("expires_in") or 0)
    return TokenSet(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", ""),
        expires_at=time.time() + expires_in if expires_in else 0.0,
    )


class GitHubOAuth:
    """OAuth web flow for a GitHub App."""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, http=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http = http or requests.Session()

    def authorize_url(self, state: str) -> str:
        query = urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "allow_signup": "false",
        })
        return f"{GITHUB}/login/oauth/authorize?{query}"

    def _token_request(self, extra: Dict[str, str]) -> TokenSet:
        response = self.http.request(
            "POST",
            f"{GITHUB}/login/oauth/access_token",
            data={"client_id": self.client_id, "client_secret": self.client_secret, **extra},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if response.status_code >= 400:
            raise GitHubUserError("GitHub sign-in failed.", response.status_code)
        return _tokens(response.json())

    def exchange_code(self, code: str) -> TokenSet:
        return self._token_request({"code": code, "redirect_uri": self.redirect_uri})

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def revoke(self, access_token: str) -> None:
        """Invalidate the token at GitHub (best effort, used on sign-out)."""
        try:
            self.http.request(
                "DELETE",
                f"{API}/applications/{self.client_id}/token",
                auth=(self.client_id, self.client_secret),
                json={"access_token": access_token},
                headers={"Accept": "application/vnd.github+json"},
                timeout=10,
            )
        except requests.RequestException:
            pass


class GitHubUserAPI:
    """REST calls with a user-to-server token."""

    def __init__(self, token: str, http=None):
        self.token = token
        self.http = http or requests.Session()

    def _call(self, method: str, path: str, ok_404: bool = False, **kwargs) -> Optional[Any]:
        response = self.http.request(
            method,
            f"{API}{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
            **kwargs,
        )
        if response.status_code == 404 and ok_404:
            return None
        if response.status_code == 401:
            raise GitHubUserError("Your GitHub session expired. Please connect again.", 401)
        if response.status_code == 403:
            raise GitHubUserError("GitHub refused this action (no permission or rate limited).", 403)
        if response.status_code >= 400:
            message = ""
            try:
                message = str(response.json().get("message", ""))[:200]
            except ValueError:
                pass
            raise GitHubUserError(f"GitHub error {response.status_code}: {message}", response.status_code)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    # -- identity / repos ---------------------------------------------------

    def get_user(self) -> Dict[str, Any]:
        return self._call("GET", "/user")

    def list_repos(self, limit: int = 300) -> List[Dict[str, Any]]:
        """Repos where the App is installed AND this user has access."""
        repos: List[Dict[str, Any]] = []
        installations = self._call("GET", "/user/installations", params={"per_page": 100})
        for inst in installations.get("installations", []):
            page = 1
            while len(repos) < limit:
                data = self._call(
                    "GET", f"/user/installations/{inst['id']}/repositories",
                    params={"per_page": 100, "page": page},
                )
                batch = data.get("repositories", [])
                repos.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        repos.sort(key=lambda r: r["full_name"].lower())
        return repos[:limit]

    def get_repo(self, owner: str, name: str) -> Dict[str, Any]:
        return self._call("GET", f"/repos/{quote(owner)}/{quote(name)}")

    # -- contents -------------------------------------------------------------

    def get_file(self, owner: str, name: str, path: str, ref: str) -> Optional[Dict[str, Any]]:
        """File metadata + decoded text, or None if it doesn't exist."""
        data = self._call(
            "GET", f"/repos/{quote(owner)}/{quote(name)}/contents/{quote(path)}",
            ok_404=True, params={"ref": ref},
        )
        if data is None or isinstance(data, list) or data.get("type") != "file":
            return None
        text = ""
        if data.get("encoding") == "base64":
            text = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        return {"sha": data["sha"], "text": text, "size": data.get("size", 0)}

    def list_root(self, owner: str, name: str, ref: str) -> List[str]:
        data = self._call("GET", f"/repos/{quote(owner)}/{quote(name)}/contents/", params={"ref": ref}) or []
        return [f"{item['name']}{'/' if item.get('type') == 'dir' else ''}" for item in data][:200]

    def branch_sha(self, owner: str, name: str, branch: str) -> str:
        data = self._call("GET", f"/repos/{quote(owner)}/{quote(name)}/git/ref/heads/{quote(branch)}")
        return data["object"]["sha"]

    def create_branch(self, owner: str, name: str, branch: str, sha: str) -> None:
        self._call("POST", f"/repos/{quote(owner)}/{quote(name)}/git/refs",
                   json={"ref": f"refs/heads/{branch}", "sha": sha})

    def delete_branch(self, owner: str, name: str, branch: str) -> None:
        self._call("DELETE", f"/repos/{quote(owner)}/{quote(name)}/git/refs/heads/{quote(branch)}", ok_404=True)

    def put_file(self, owner: str, name: str, path: str, text: str, message: str, branch: str) -> None:
        self._call("PUT", f"/repos/{quote(owner)}/{quote(name)}/contents/{quote(path)}", json={
            "message": message,
            "content": base64.b64encode(text.encode()).decode(),
            "branch": branch,
        })

    def delete_file(self, owner: str, name: str, path: str, sha: str, message: str, branch: str) -> None:
        self._call("DELETE", f"/repos/{quote(owner)}/{quote(name)}/contents/{quote(path)}", json={
            "message": message, "sha": sha, "branch": branch,
        })

    def create_pull_request(self, owner: str, name: str, head: str, base: str, title: str, body: str) -> str:
        data = self._call("POST", f"/repos/{quote(owner)}/{quote(name)}/pulls", json={
            "title": title, "head": head, "base": base, "body": body,
        })
        return data["html_url"]
