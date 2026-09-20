"""Tests for the dashboard API lambda (in-memory fake table + fake SSM)."""
import sys

import pytest

from aws_stubs import install_stubs
from test_dynamo_db import FakeTable, ConditionalCheckFailedException

install_stubs()

TASK_TABLE = "slackagent-tasks"
AUDIT_TABLE = "slackagent-audit-events"


class FakeSSM:
    def __init__(self):
        self.values = {"/slack-agent/kill-switch": "0"}

    def get_parameter(self, Name, WithDecryption=False):
        return {"Parameter": {"Value": self.values.get(Name, "0")}}

    def put_parameter(self, Name, Value, Type, Overwrite=True):
        self.values[Name] = Value


@pytest.fixture
def env(monkeypatch):
    from dashboard_api import dashboard

    tasks = FakeTable(TASK_TABLE)
    audit = FakeTable(AUDIT_TABLE)
    monkeypatch.setattr(dashboard, "TABLE_NAME", TASK_TABLE)
    monkeypatch.setattr(dashboard, "AUDIT_TABLE", AUDIT_TABLE)
    monkeypatch.setattr(dashboard, "_table", lambda: tasks)
    monkeypatch.setattr(dashboard, "_audit_table", lambda: audit)
    monkeypatch.setattr(dashboard, "QUEUE_URL", "")
    fakes = {"tasks": tasks, "audit": audit}
    class _Ctx:
        def __enter__(self): return fakes
        def __exit__(self, *a): pass
    return _Ctx()


@pytest.fixture
def dashboard(env, monkeypatch):
    from dashboard_api import dashboard

    monkeypatch.setattr(dashboard, "ssm", FakeSSM())
    return dashboard


def _event(method, path, task_id=None, body=None):
    return {
        "httpMethod": method,
        "path": path,
        "pathParameters": {"id": task_id} if task_id else None,
        "body": None if body is None else (body if isinstance(body, str) else __import__("json").dumps(body)),
    }


def _make(env):
    with env as f:
        f["tasks"].put_item(Item={
            "taskId": "task-abc123", "timestamp": "now", "status": "READY",
            "request": "Add tests", "repo": "org/repo", "baseBranch": "main",
            "createdAt": "2026-01-01T00:00:00+00:00",
        })


def test_list_tasks(dashboard, env):
    _make(env)
    resp = dashboard.handler(_event("GET", "/tasks"), None)
    assert resp["statusCode"] == 200
    body = __import__("json").loads(resp["body"])
    assert body["count"] == 1
    assert body["tasks"][0]["taskId"] == "task-abc123"


def test_get_task(dashboard, env):
    _make(env)
    resp = dashboard.handler(_event("GET", "/tasks", task_id="task-abc123"), None)
    assert resp["statusCode"] == 200
    body = __import__("json").loads(resp["body"])
    assert body["status"] == "READY"


def test_get_missing_task_404(dashboard, env):
    _make(env)
    resp = dashboard.handler(_event("GET", "/tasks", task_id="task-nope"), None)
    assert resp["statusCode"] == 404


def test_create_task_requires_request(dashboard, env):
    resp = dashboard.handler(_event("POST", "/tasks", body={}), None)
    assert resp["statusCode"] == 400


def test_create_task_requires_repo(dashboard, env):
    resp = dashboard.handler(_event("POST", "/tasks", body={"request": "x"}), None)
    assert resp["statusCode"] == 400


def test_create_task_success(dashboard, env):
    resp = dashboard.handler(_event("POST", "/tasks", body={"request": "Add tests", "repo": "org/repo"}), None)
    assert resp["statusCode"] == 202
    body = __import__("json").loads(resp["body"])
    assert body["taskId"].startswith("task-")
    with env as f:
        item = f["tasks"]._get({"taskId": body["taskId"], "timestamp": "now"})
        assert item["status"] == "READY"
        # model backend is recorded on the item (defaults to bedrock when SSM is invalid)
        assert item.get("modelBackend") == "bedrock"
        assert item.get("model") == "bedrock"


def test_create_task_idempotent_on_client_token(dashboard, env):
    req = lambda: _event("POST", "/tasks", body={"request": "x", "repo": "org/repo", "client_token": "tok-1"})
    first = dashboard.handler(req(), None)
    second = dashboard.handler(req(), None)
    assert __import__("json").loads(first["body"])["taskId"] == __import__("json").loads(second["body"])["taskId"]


def test_cancel_waiting_task(dashboard, env):
    _make(env)
    resp = dashboard.handler(_event("POST", "/tasks/task-abc123/cancel", task_id="task-abc123"), None)
    assert resp["statusCode"] == 200
    with env as f:
        assert f["tasks"]._get({"taskId": "task-abc123", "timestamp": "now"})["status"] == "CANCELLED"


def test_cancel_terminal_task_conflicts(dashboard, env):
    _make(env)
    with env as f:
        f["tasks"]._get({"taskId": "task-abc123", "timestamp": "now"})["status"] = "COMPLETED"
    resp = dashboard.handler(_event("POST", "/tasks/task-abc123/cancel", task_id="task-abc123"), None)
    assert resp["statusCode"] == 409


def test_stats_and_metrics(dashboard, env):
    _make(env)
    stats = __import__("json").loads(dashboard.handler(_event("GET", "/stats"), None)["body"])
    assert stats["total"] == 1
    metrics = __import__("json").loads(dashboard.handler(_event("GET", "/metrics"), None)["body"])
    assert metrics["counts"]["READY"] == 1


def test_stats_and_metrics_report_model_backend(dashboard, env):
    from dashboard_api import dashboard as d
    # SSM returns an invalid value for the model-backend param → falls back to bedrock
    d.ssm.values["/slack-agent/model-backend"] = "not-a-backend"
    for path in ("/stats", "/metrics"):
        d._model_backend_cache["value"] = None
        body = __import__("json").loads(d.handler(_event("GET", path), None)["body"])
        assert body["modelBackend"] == "bedrock"
        assert body["simMode"] is False
    # Valid SIM value propagates too
    d.ssm.values["/slack-agent/model-backend"] = "sim"
    d._model_backend_cache["value"] = None
    body = __import__("json").loads(d.handler(_event("GET", "/stats"), None)["body"])
    assert body["modelBackend"] == "sim"
    assert body["simMode"] is True


def test_audit_requires_events(dashboard, env):
    _make(env)
    resp = dashboard.handler(_event("GET", "/audit"), None)
    body = __import__("json").loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["count"] == 0


def test_kill_switch_get_and_set(dashboard, env):
    assert __import__("json").loads(dashboard.handler(_event("GET", "/admin/kill"), None)["body"])["killSwitch"] is False
    resp = dashboard.handler(_event("POST", "/admin/kill", body={"kill": True}), None)
    assert __import__("json").loads(resp["body"])["killSwitch"] is True
    assert __import__("json").loads(dashboard.handler(_event("GET", "/admin/kill"), None)["body"])["killSwitch"] is True


def test_kill_switch_reports_model_backend(dashboard, env):
    from dashboard_api import dashboard as d
    d.ssm.values["/slack-agent/model-backend"] = "sim"
    d._model_backend_cache["value"] = None
    body = __import__("json").loads(d.handler(_event("GET", "/admin/kill"), None)["body"])
    assert body["modelBackend"] == "sim"
    assert body["simMode"] is True