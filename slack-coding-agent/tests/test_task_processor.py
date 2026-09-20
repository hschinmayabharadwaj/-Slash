"""Tests for the task processor lambda (fake tables + no queue)."""
import json

import pytest

from aws_stubs import fake_dynamodb_resource, install_stubs
from test_dynamo_db import FakeTable

install_stubs()

TASK_TABLE = "slackagent-tasks"
AUDIT_TABLE = "slackagent-audit-events"


@pytest.fixture
def env(monkeypatch):
    from task_processor import index as tp

    tasks = FakeTable(TASK_TABLE)
    audit = FakeTable(AUDIT_TABLE)
    fake_dynamodb_resource().tables[TASK_TABLE] = tasks
    fake_dynamodb_resource().tables[AUDIT_TABLE] = audit
    monkeypatch.setattr(tp, "TABLE_NAME", TASK_TABLE)
    monkeypatch.setattr(tp, "AUDIT_TABLE", AUDIT_TABLE)
    monkeypatch.setattr(tp, "QUEUE_URL", "")
    return {"tasks": tasks, "audit": audit}


def _seed(env, status="AWAITING_APPROVAL", task_id="task-123"):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    env["tasks"].put_item(Item={
        "taskId": task_id, "timestamp": "now", "status": status,
        "request": "Do a thing", "repo": "org/repo",
        "createdAt": now, "updatedAt": now,
    })


def _event(task_id, action, body=None):
    return {
        "httpMethod": "POST",
        "path": f"/tasks/{task_id}/{action}",
        "pathParameters": {"id": task_id},
        "body": json.dumps(body or {}),
    }


def test_approve_awaiting_plan(env, monkeypatch):
    from task_processor import index as tp

    _seed(env, "AWAITING_APPROVAL")
    resp = tp.handler(_event("task-123", "approve", {"note": "looks good"}), None)
    assert resp["statusCode"] == 200
    item = env["tasks"]._get({"taskId": "task-123", "timestamp": "now"})
    assert item["status"] == "PLAN_APPROVED"
    assert item["approvedAt"]


def test_approve_awaiting_impl(env, monkeypatch):
    from task_processor import index as tp

    _seed(env, "AWAITING_IMPL_APPROVAL")
    resp = tp.handler(_event("task-123", "approve"), None)
    assert resp["statusCode"] == 200
    item = env["tasks"]._get({"taskId": "task-123", "timestamp": "now"})
    assert item["status"] == "IMPL_APPROVED"


def test_approve_wrong_state_conflicts(env, monkeypatch):
    from task_processor import index as tp

    _seed(env, "COMPLETED")
    resp = tp.handler(_event("task-123", "approve"), None)
    assert resp["statusCode"] == 409


def test_reject(env, monkeypatch):
    from task_processor import index as tp

    _seed(env, "AWAITING_APPROVAL")
    resp = tp.handler(_event("task-123", "reject", {"reason": "wrong scope"}), None)
    assert resp["statusCode"] == 200
    item = env["tasks"]._get({"taskId": "task-123", "timestamp": "now"})
    assert item["status"] == "REJECTED"
    assert item["rejectionReason"] == "wrong scope"


def test_cancel(env, monkeypatch):
    from task_processor import index as tp

    _seed(env, "AWAITING_APPROVAL")
    resp = tp.handler(_event("task-123", "cancel"), None)
    assert resp["statusCode"] == 200
    assert env["tasks"]._get({"taskId": "task-123", "timestamp": "now"})["status"] == "CANCELLED"


def test_expire_stale(env, monkeypatch):
    from task_processor import index as tp

    import datetime as _dt
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=30)).isoformat()
    env["tasks"].put_item(Item={
        "taskId": "task-old", "timestamp": "now", "status": "AWAITING_APPROVAL",
        "request": "r", "repo": "o/r", "createdAt": old, "updatedAt": old,
    })
    _seed(env, "AWAITING_IMPL_APPROVAL")  # fresh, must NOT expire
    resp = tp.handler({"action": "expire_stale"}, None)
    assert resp["expired"] == 1
    assert env["tasks"]._get({"taskId": "task-old", "timestamp": "now"})["status"] == "EXPIRED"
    assert env["tasks"]._get({"taskId": "task-123", "timestamp": "now"})["status"] == "AWAITING_IMPL_APPROVAL"


def test_audit_records_decision(env, monkeypatch):
    from task_processor import index as tp

    _seed(env, "AWAITING_APPROVAL")
    tp.handler(_event("task-123", "approve"), None)
    events = [e for e in env["audit"].items.values() if e.get("action") == "approved"]
    assert len(events) == 1
    assert events[0]["taskId"] == "task-123"