"""Tests for Slack request intake and the approval workflow (fake Slack app)."""

import pytest

from slackagent.database import Database, TaskStatus
from slackagent.slack_handler import SlackHandler


class _Client:
    def __init__(self):
        self.messages, self.ephemeral, self.updates = [], [], []

    def chat_postMessage(self, **kw):
        self.messages.append(kw)

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)

    def chat_update(self, **kw):
        self.updates.append(kw)


class FakeApp:
    def __init__(self):
        self.client = _Client()

    def event(self, _name):
        return lambda fn: fn

    def action(self, _name):
        return lambda fn: fn


@pytest.fixture
def handler(test_config):
    return SlackHandler(test_config, Database(test_config.database.path), app=FakeApp())


def _mention(handler, text, user="U1", thread=None):
    replies = []
    event = {"user": user, "channel": "C1", "ts": "100.1", "text": f"<@UBOT> {text}"}
    if thread:
        event["thread_ts"] = thread
    handler._handle_mention(event, lambda msg, thread_ts: replies.append(msg))
    return replies


def _click(handler, action, task_id, user="U1"):
    body = {"user": {"id": user}, "channel": {"id": "C1"}, "actions": [{"value": str(task_id)}],
            "message": {"ts": "200.1", "blocks": [{"type": "section"}, {"type": "actions"}]}}
    handler._handle_decision(body, action)


def _awaiting_plan(handler):
    _mention(handler, "testorg/testrepo add a greeting")
    task = handler.database.get_task_by_thread("C1", "100.1")
    handler.database.update_task_status(task.id, TaskStatus.AWAITING_PLAN_APPROVAL)
    return task


def test_mention_creates_task(handler):
    replies = _mention(handler, "testorg/testrepo add a greeting")
    task = handler.database.get_task_by_thread("C1", "100.1")
    assert task.status == TaskStatus.PENDING and task.task_description == "add a greeting"
    assert "task #" in replies[0]


def test_default_owner(handler):
    _mention(handler, "testrepo fix typo")
    assert handler.database.get_task_by_thread("C1", "100.1").repo_owner == "testorg"


def test_user_allowlist(handler):
    handler.config.security.allowed_users = ["U9"]
    assert "not on the list" in _mention(handler, "testorg/testrepo x")[0]
    assert handler.database.get_task_by_thread("C1", "100.1") is None


def test_repo_allowlist_and_bad_names(handler):
    handler.config.security.allowed_repos = ["testorg/testrepo"]
    assert "not allowed" in _mention(handler, "evil/repo x")[0]
    assert "valid" in _mention(handler, "../etc x")[0] or "name the repository" in _mention(handler, "../etc x")[0]


def test_request_length_and_quota(handler):
    handler.config.security.max_request_chars = 10
    assert "too long" in _mention(handler, "testorg/testrepo " + "x" * 20)[0]
    handler.config.security.max_request_chars = 2000
    handler.config.security.max_active_tasks_per_user = 1
    for i in range(2):
        handler.database.create_task("U1", "C9", f"{i}", "o", "r", "", "t")
    assert "already have" in _mention(handler, "testorg/testrepo x")[0]


def test_only_requester_approves_by_default(handler):
    task = _awaiting_plan(handler)
    _click(handler, "approve_plan", task.id, user="U2")
    assert handler.database.get_task(task.id).status == TaskStatus.AWAITING_PLAN_APPROVAL
    assert handler.app.client.ephemeral

    _click(handler, "approve_plan", task.id, user="U1")
    assert handler.database.get_task(task.id).status == TaskStatus.PLAN_APPROVED
    # Buttons removed from the original message.
    assert all(b["type"] != "actions" for b in handler.app.client.updates[0]["blocks"])


def test_configured_approvers_replace_requester(handler):
    handler.config.security.approvers = ["ULEAD"]
    task = _awaiting_plan(handler)
    _click(handler, "approve_plan", task.id, user="U1")
    assert handler.database.get_task(task.id).status == TaskStatus.AWAITING_PLAN_APPROVAL
    _click(handler, "approve_plan", task.id, user="ULEAD")
    assert handler.database.get_task(task.id).status == TaskStatus.PLAN_APPROVED


def test_stale_button_cannot_skip_stages(handler):
    task = _awaiting_plan(handler)
    _click(handler, "approve_impl", task.id)  # wrong stage
    assert handler.database.get_task(task.id).status == TaskStatus.AWAITING_PLAN_APPROVAL
    _click(handler, "reject_plan", task.id)
    _click(handler, "approve_plan", task.id)  # second click after rejection
    assert handler.database.get_task(task.id).status == TaskStatus.PLAN_REJECTED


def test_cancel_and_status_in_thread(handler):
    task = _awaiting_plan(handler)
    assert "awaiting_plan_approval" in _mention(handler, "status", thread="100.1")[0]
    assert "Only the requester" in _mention(handler, "cancel", user="U2", thread="100.1")[0]
    _mention(handler, "cancel", thread="100.1")
    assert handler.database.get_task(task.id).status == TaskStatus.CANCELLED


def test_duplicate_request_in_same_thread(handler):
    _mention(handler, "testorg/testrepo add a greeting")
    assert "already working" in _mention(handler, "testorg/testrepo again")[0]


def test_model_text_is_escaped(handler):
    task = _awaiting_plan(handler)
    handler.post_plan(task.id, {"summary": "<!channel> hi", "files": [], "risk": "low"})
    text = handler.app.client.messages[-1]["blocks"][0]["text"]["text"]
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text
