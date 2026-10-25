"""Client for the Go agent-service (AI drafting + content checks).
It sends repository facts and proposed content only -- never a GitHub token."""
from __future__ import annotations

import httpx


class AgentServiceError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class AgentClient:
    def __init__(self, url: str, token: str, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self.http = http or httpx.Client(timeout=100)

    def _post(self, path: str, body: dict | None = None, method: str = "POST") -> dict:
        try:
            res = self.http.request(method, f"{self.url}{path}", json=body,
                                    headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError:
            raise AgentServiceError("The agent service is not reachable. Is agent-service running?") from None
        try:
            data = res.json()
        except ValueError:
            data = {}
        if res.is_error:
            message = f"The agent service failed: {data.get('error') or res.status_code}"
            raise AgentServiceError(message, res.status_code)
        return data

    def draft_readme(self, facts: dict) -> str:
        return self._post("/v1/readme/draft", facts)["markdown"]

    def check_readme(self, content: str) -> list[str]:
        """Problems (empty = OK). Fails closed if unreachable."""
        return self._post("/v1/readme/check", {"content": content}).get("problems") or []

    def capabilities(self) -> dict:
        """AI provider, whether code may be sent to it, and whether Build tasks can run."""
        return self._post("/v1/capabilities", method="GET")

    def ask(self, body: dict) -> dict:
        return self._post("/v1/ask", body)

    def create_task(self, body: dict) -> dict:
        return self._post("/v1/tasks", body)

    def get_task(self, agent_id: int) -> dict:
        return self._post(f"/v1/tasks/{int(agent_id)}", method="GET")

    def decide(self, agent_id: int, stage: str, approve: bool, by: str) -> dict:
        return self._post(f"/v1/tasks/{int(agent_id)}/decision", {"stage": stage, "approve": approve, "by": by})
