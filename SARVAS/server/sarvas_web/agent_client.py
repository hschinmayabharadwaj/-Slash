"""Client for the Go agent-service (AI drafting + content checks).
It sends repository facts and proposed content only -- never a GitHub token."""
from __future__ import annotations

import httpx


class AgentServiceError(Exception):
    pass


class AgentClient:
    def __init__(self, url: str, token: str, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self.http = http or httpx.Client(timeout=100)

    def _post(self, path: str, body: dict) -> dict:
        try:
            res = self.http.post(f"{self.url}{path}", json=body, headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError:
            raise AgentServiceError("The agent service is not reachable. Is agent-service running?") from None
        try:
            data = res.json()
        except ValueError:
            data = {}
        if res.is_error:
            raise AgentServiceError(f"The agent service failed: {data.get('error') or res.status_code}")
        return data

    def draft_readme(self, facts: dict) -> str:
        return self._post("/v1/readme/draft", facts)["markdown"]

    def check_readme(self, content: str) -> list[str]:
        """Problems (empty = OK). Fails closed if unreachable."""
        return self._post("/v1/readme/check", {"content": content}).get("problems") or []
