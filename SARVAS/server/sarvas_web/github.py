"""GitHub sign-in (OAuth web flow for a GitHub App) and API calls made as the
signed-in user. User-to-server tokens can never do more than BOTH the App's
permissions (Contents + Pull requests) AND the person's own access, only on
repos where the App is installed, and they expire (8h) with a refresh token."""
from __future__ import annotations

import base64
import time
from urllib.parse import quote, urlencode

import httpx

GITHUB = "https://github.com"
API = "https://api.github.com"


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _enc(s: str) -> str:
    return quote(str(s), safe="")


def _token_set(data: dict) -> dict:
    if not data.get("access_token"):
        raise GitHubError(f"GitHub sign-in failed: {data.get('error_description') or data.get('error') or 'no token'}")
    expires_in = int(data.get("expires_in") or 0)
    return {
        "accessToken": data["access_token"],
        "refreshToken": data.get("refresh_token") or "",
        "expiresAt": int(time.time() * 1000) + expires_in * 1000 if expires_in else 0,
    }


def token_expired(t: dict) -> bool:
    return bool(t.get("expiresAt")) and time.time() * 1000 > t["expiresAt"] - 120_000


class GitHubOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, http: httpx.Client):
        self.client_id, self.client_secret, self.redirect_uri, self.http = client_id, client_secret, redirect_uri, http

    def authorize_url(self, state: str) -> str:
        q = urlencode({"client_id": self.client_id, "redirect_uri": self.redirect_uri,
                       "state": state, "allow_signup": "false"})
        return f"{GITHUB}/login/oauth/authorize?{q}"

    def _token(self, extra: dict) -> dict:
        res = self.http.post(f"{GITHUB}/login/oauth/access_token", headers={"Accept": "application/json"},
                             data={"client_id": self.client_id, "client_secret": self.client_secret, **extra})
        if res.is_error:
            raise GitHubError("GitHub sign-in failed.", res.status_code)
        return _token_set(res.json())

    def exchange_code(self, code: str) -> dict:
        return self._token({"code": code, "redirect_uri": self.redirect_uri})

    def refresh(self, refresh_token: str) -> dict:
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def revoke(self, access_token: str) -> None:
        """Invalidate the token at GitHub (best effort, on sign-out)."""
        try:
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            self.http.request("DELETE", f"{API}/applications/{_enc(self.client_id)}/token",
                              headers={"Authorization": f"Basic {basic}", "Accept": "application/vnd.github+json"},
                              json={"access_token": access_token})
        except httpx.HTTPError:
            pass


class GitHubUser:
    def __init__(self, token: str, http: httpx.Client):
        self.token, self.http = token, http

    def call(self, method: str, path: str, *, body=None, query=None, ok404=False):
        res = self.http.request(method, API + path, params=query, json=body, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if res.status_code == 404 and ok404:
            return None
        if res.status_code == 401:
            raise GitHubError("Your GitHub session expired. Please connect again.", 401)
        if res.status_code == 403:
            raise GitHubError("GitHub refused this action (no permission or rate limited).", 403)
        if res.status_code >= 400:
            try:
                message = str(res.json().get("message", ""))[:200]
            except ValueError:
                message = ""
            raise GitHubError(f"GitHub error {res.status_code}: {message}", res.status_code)
        if res.status_code == 204 or not res.content:
            return {}
        return res.json()

    def get_user(self) -> dict:
        return self.call("GET", "/user")

    def list_repos(self, limit: int = 300) -> list[dict]:
        """Repos where the App is installed AND this user has access."""
        repos: list[dict] = []
        installations = self.call("GET", "/user/installations", query={"per_page": 100}).get("installations", [])
        for inst in installations:
            page = 1
            while len(repos) < limit:
                data = self.call("GET", f"/user/installations/{inst['id']}/repositories",
                                 query={"per_page": 100, "page": page})
                batch = data.get("repositories") or []
                repos.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        return sorted(repos, key=lambda r: r["full_name"])[:limit]

    def get_repo(self, owner: str, name: str) -> dict:
        return self.call("GET", f"/repos/{_enc(owner)}/{_enc(name)}")

    def get_file(self, owner: str, name: str, file_path: str, ref: str) -> dict | None:
        """File text + blob sha, or None if it doesn't exist."""
        data = self.call("GET", f"/repos/{_enc(owner)}/{_enc(name)}/contents/{_enc(file_path)}",
                         query={"ref": ref}, ok404=True)
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        text = base64.b64decode(data.get("content") or "").decode("utf-8", "replace") \
            if data.get("encoding") == "base64" else ""
        return {"sha": data["sha"], "text": text}

    def list_root(self, owner: str, name: str, ref: str) -> list[str]:
        data = self.call("GET", f"/repos/{_enc(owner)}/{_enc(name)}/contents/", query={"ref": ref}) or []
        return [i["name"] + ("/" if i.get("type") == "dir" else "") for i in data[:200]]

    def branch_sha(self, owner: str, name: str, branch: str) -> str:
        return self.call("GET", f"/repos/{_enc(owner)}/{_enc(name)}/git/ref/heads/{_enc(branch)}")["object"]["sha"]

    def create_branch(self, owner: str, name: str, branch: str, sha: str):
        return self.call("POST", f"/repos/{_enc(owner)}/{_enc(name)}/git/refs",
                         body={"ref": f"refs/heads/{branch}", "sha": sha})

    def delete_branch(self, owner: str, name: str, branch: str):
        return self.call("DELETE", f"/repos/{_enc(owner)}/{_enc(name)}/git/refs/heads/{_enc(branch)}", ok404=True)

    def put_file(self, owner: str, name: str, file_path: str, text: str, message: str, branch: str):
        return self.call("PUT", f"/repos/{_enc(owner)}/{_enc(name)}/contents/{_enc(file_path)}", body={
            "message": message, "content": base64.b64encode(text.encode()).decode(), "branch": branch})

    def delete_file(self, owner: str, name: str, file_path: str, sha: str, message: str, branch: str):
        return self.call("DELETE", f"/repos/{_enc(owner)}/{_enc(name)}/contents/{_enc(file_path)}",
                         body={"message": message, "sha": sha, "branch": branch})

    def create_pull_request(self, owner: str, name: str, head: str, base: str, title: str, body: str) -> str:
        data = self.call("POST", f"/repos/{_enc(owner)}/{_enc(name)}/pulls",
                         body={"title": title, "head": head, "base": base, "body": body})
        return data["html_url"]
