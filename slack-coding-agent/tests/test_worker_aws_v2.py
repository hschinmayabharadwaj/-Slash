"""Tests for the AWS worker (fake store tables, fake sandbox, fake GitHub ops)."""
import json
import os

import pytest

from slackagent.worker_aws_v2 import TaskAbort, WorkerAWS
from test_dynamo_db import FakeTable

TASK_TABLE = "slackagent-tasks"
AUDIT_TABLE = "slackagent-audit-events"


class FakeSQS:
    def __init__(self):
        self.messages = []
        self.deleted = []

    def send_message(self, *, QueueUrl, MessageBody):
        self.messages.append(MessageBody)

    def receive_message(self, *, QueueUrl, MaxNumberOfMessages, WaitTimeSeconds, VisibilityTimeout):
        if not self.messages:
            return {"Messages": []}
        msg = self.messages.pop(0)
        return {"Messages": [{"MessageId": "m1", "ReceiptHandle": "r1", "Body": msg}]}

    def delete_message(self, *, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


class FakeSSM:
    def __init__(self, value="0"):
        self.value = value

    def get_parameter(self, Name):
        return {"Parameter": {"Value": self.value}}


class FakeSandbox:
    def __init__(self, plan=None, impl_text="Changes made"):
        self.calls = []
        self.plan = plan or {
            "feasible": True,
            "summary": "Add tests for the API",
            "files": ["src/app.py"],
            "risk": "low",
            "how_to_verify": "pytest",
        }
        self.impl_text = impl_text

    def run_agent(self, *, mode, repo_dir, io_dir, image, timeout_s, api_key):
        self.calls.append(mode)
        (io_dir / "output.json").write_text(json.dumps({
            "ok": True,
            "plan": self.plan if mode == "plan" else None,
            "text": self.impl_text,
        }))
        return type("Result", (), {"timed_out": False, "returncode": 0, "stdout": "", "stderr": ""})()


class FakeGit:
    def __init__(self):
        self.pulls = []

    def get_clone_url(self, owner, name):
        return f"https://x-access-token:tok@github.com/{owner}/{name}.git"

    def create_pull_request(self, owner, name, branch, title, body):
        url = f"https://github.com/{owner}/{name}/pull/7"
        self.pulls.append((owner, name, branch, title))
        return url


@pytest.fixture
def worker(monkeypatch):
    tasks = FakeTable(TASK_TABLE)
    audit = FakeTable(AUDIT_TABLE)
    store = __import__("slackagent.dynamo_db", fromlist=["DynamoDBStore"]).DynamoDBStore(
        table_name=TASK_TABLE, audit_table_name=AUDIT_TABLE, table=tasks, audit_table=audit,
    )
    fake_git = FakeGit()

    import slackagent.worker_aws_v2 as w
    original_clone = w.WorkerAWS.clone

    ww = WorkerAWS(
        store=store,
        sandbox=FakeSandbox(),
        github=fake_git,
        queue_url="https://sqs.us-east-2.amazonaws.com/958357664308/tasks",
        worker_id="w-test",
        sqs=FakeSQS(),
        ssm=FakeSSM(),
    )

    def stub_clone(self, task, dest, base="main"):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "pyproject.toml").write_text("[project]\n")
        (dest / "src").mkdir()
        (dest / "src" / "app.py").write_text("def main():\n    pass\n")

    monkeypatch.setattr(ww, "clone", stub_clone.__get__(ww, WorkerAWS))

    import slackagent.github_ops as go
    from slackagent import guards as g

    monkeypatch.setattr(go.GitOperations, "create_branch", staticmethod(lambda repo_path, branch_name: None))
    monkeypatch.setattr(go.GitOperations, "push_branch", staticmethod(lambda repo_path, branch: None))
    monkeypatch.setattr(go.GitOperations, "commit_changes", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(go.GitOperations, "has_changes", staticmethod(lambda repo_path: True))
    monkeypatch.setattr(go.GitOperations, "get_diff", staticmethod(lambda repo_path, cached=False: "--- a/src/app.py\n+++ b/src/app.py\n+print('hi')\n"))
    monkeypatch.setattr(
        g, "inspect_worktree",
        staticmethod(lambda repo_dir: g.DiffReport(files=[g.FileChange("src/app.py", added=1)]))
    )
    monkeypatch.setattr(g, "check_policy", staticmethod(lambda report, **kw: []))
    monkeypatch.setattr(g, "scan_secrets", staticmethod(lambda added_lines: []))

    import slackagent.worker_aws_v2 as w
    monkeypatch.setattr(w, "_apply_patch", staticmethod(lambda repo_dir, diff: None))
    return ww


def _seed(worker, task_id="task-1", status="READY", with_plan=False):
    item = {
        "taskId": task_id, "timestamp": "now", "status": status,
        "request": "Add tests for the API", "repo": "org/repo", "baseBranch": "main",
        "createdAt": "2026-01-01T00:00:00+00:00",
    }
    if with_plan:
        item["plan"] = json.dumps({"feasible": True, "summary": "Add tests", "files": ["src/app.py"], "risk": "low"})
    worker.store._table().put_item(Item=item)


def test_readB_empty_ok(worker):
    payload = json.dumps({"taskId": "task-missing"})
    assert worker.handle_message(payload) is False


def test_plan_phase_posts_plan_for_approval(worker):
    _seed(worker, "task-1", "READY")
    assert worker.handle_message(json.dumps({"taskId": "task-1"})) is True
    item = worker.store.get_item_raw("task-1")
    assert item["status"] == "AWAITING_APPROVAL"
    assert json.loads(item["plan"])["feasible"] is True
    events = worker.store.list_events("task-1")
    assert {e["event_type"] for e in events} == {"plan_ready"}


def test_plan_phase_skips_unfeasible(worker, monkeypatch):
    _seed(worker, "task-1", "READY")
    worker.sandbox.plan = {"feasible": False, "summary": "cannot", "files": [], "risk": "medium"}
    worker.handle_message(json.dumps({"taskId": "task-1"}))
    assert worker.store.get_item_raw("task-1")["status"] == "DECLINED"


def test_plan_phase_skips_high_risk(worker, monkeypatch):
    _seed(worker, "task-1", "READY")
    worker.sandbox.plan = {**worker.sandbox.plan, "risk": "high", "risk_reason": "danger"}
    worker.handle_message(json.dumps({"taskId": "task-1"}))
    assert worker.store.get_item_raw("task-1")["status"] == "DECLINED"


def test_implement_phase_posts_for_impl_approval(worker):
    _seed(worker, "task-1", "PLAN_APPROVED", with_plan=True)
    worker.handle_message(json.dumps({"taskId": "task-1"}))
    item = worker.store.get_item_raw("task-1")
    assert item["status"] == "AWAITING_IMPL_APPROVAL"
    assert "diff" in item


def test_pr_phase_completes(worker):
    _seed(worker, "task-1", "IMPL_APPROVED", with_plan=True)
    worker.store.set_task_implementation("task-1", "done", "--- a/x\n+++ b/x\n")
    worker.handle_message(json.dumps({"taskId": "task-1"}))
    item = worker.store.get_item_raw("task-1")
    assert item["status"] == "COMPLETED"
    assert item["pr_url"].endswith("/pull/7")


def test_task_not_in_runnable_status_is_skipped(worker):
    _seed(worker, "task-1", "COMPLETED")
    assert worker.handle_message(json.dumps({"taskId": "task-1"})) is False


def test_claim_prevents_double_processing(worker):
    _seed(worker, "task-1", "READY")
    assert worker.claim("task-1") is True
    assert worker.handle_message(json.dumps({"taskId": "task-1"})) is False


def test_kill_switch_on(worker):
    assert worker.kill_switch_on() is False
    worker._ssm.value = "1"
    assert worker.kill_switch_on() is True


def test_resolve_model_backend_falls_back_to_bedrock(worker, monkeypatch):
    # FakeSSM returns "0" (invalid) and no env var → bedrock
    monkeypatch.delenv("MODEL_BACKEND", raising=False)
    assert worker.resolve_model_backend() == "bedrock"


def test_resolve_model_backend_env_wins(worker, monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "sim")
    assert worker.resolve_model_backend() == "sim"
    assert os.environ.get("MODEL_BACKEND") == "sim"


def test_resolve_model_backend_from_ssm(worker, monkeypatch):
    monkeypatch.delenv("MODEL_BACKEND", raising=False)
    worker._ssm.value = "anthropic"
    assert WorkerAWS.resolve_model_backend(worker) == "anthropic"