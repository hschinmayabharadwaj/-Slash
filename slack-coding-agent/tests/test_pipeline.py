"""End-to-end pipeline tests: real git + local bare "origin", fake sandbox/GitHub."""

import subprocess
from pathlib import Path

import pytest

from conftest import FakeSandbox, make_edit
from slackagent.database import Database, TaskStatus
from slackagent.worker import Worker, sha256


def _task(db: Database):
    return db.create_task(
        slack_user_id="U1", slack_channel_id="C1", slack_thread_ts="1.0",
        repo_owner="testorg", repo_name="testrepo", branch_name="",
        task_description="Add a greeting to the README",
    )


def _worker(test_config, fake_slack, fake_github, sandbox):
    db = Database(test_config.database.path)
    return db, Worker(test_config, db, fake_slack, worker_id="t", github=fake_github, sandbox=sandbox)


def _plan(**overrides):
    plan = {
        "feasible": True, "summary": "Add a greeting", "files": ["README.md"],
        "risk": "low", "risk_reason": "docs", "questions": [], "how_to_verify": "Read it",
    }
    plan.update(overrides)
    return plan


def _run_to_impl_approval(db, worker, task):
    assert worker.process_one()
    assert db.get_task(task.id).status == TaskStatus.AWAITING_PLAN_APPROVAL
    assert db.transition_status(task.id, TaskStatus.AWAITING_PLAN_APPROVAL, TaskStatus.PLAN_APPROVED)
    assert worker.process_one()


def test_full_flow_opens_pr_with_exact_approved_diff(test_config, fake_slack, fake_github, origin_repo):
    sandbox = FakeSandbox(plan=_plan(), edit_func=make_edit("README.md", "# Origin\nHello!\n"))
    db, worker = _worker(test_config, fake_slack, fake_github, sandbox)
    task = _task(db)

    _run_to_impl_approval(db, worker, task)
    after_impl = db.get_task(task.id)
    assert after_impl.status == TaskStatus.AWAITING_IMPL_APPROVAL
    assert after_impl.diff_sha256 == sha256(after_impl.diff)
    assert "+Hello!" in after_impl.diff
    assert fake_slack.plans and fake_slack.implementations
    # Trusted checks from config ran (conftest config defines lint + test).
    assert [r["command"] for r in sandbox.runs if r["mode"] == "checks"] == ["ruff check .", "pytest -v"]

    assert db.transition_status(task.id, TaskStatus.AWAITING_IMPL_APPROVAL, TaskStatus.IMPL_APPROVED)
    assert worker.process_one()

    done = db.get_task(task.id)
    assert done.status == TaskStatus.COMPLETED
    assert done.pr_url and fake_github.prs[0]["head"] == done.branch_name
    # The agent ran exactly twice (plan + implement) -- never re-run after approval.
    assert [r["mode"] for r in sandbox.runs if r["mode"] != "checks"] == ["plan", "implement"]
    pushed = subprocess.run(
        ["git", "--git-dir", str(origin_repo), "show", f"{done.branch_name}:README.md"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert pushed == "# Origin\nHello!\n"
    events = [e["event"] for e in db.list_events(task.id)]
    assert "plan_generated" in events and "pr_created" in events


def test_tampered_diff_is_refused(test_config, fake_slack, fake_github):
    sandbox = FakeSandbox(plan=_plan(), edit_func=make_edit("README.md", "changed\n"))
    db, worker = _worker(test_config, fake_slack, fake_github, sandbox)
    task = _task(db)
    _run_to_impl_approval(db, worker, task)

    with db._get_connection() as conn:
        conn.execute("UPDATE tasks SET diff = diff || '+evil' WHERE id = ?", (task.id,))
    db.transition_status(task.id, TaskStatus.AWAITING_IMPL_APPROVAL, TaskStatus.IMPL_APPROVED)
    worker.process_one()

    assert db.get_task(task.id).status == TaskStatus.DECLINED
    assert not fake_github.prs


@pytest.mark.parametrize("path,content,needle", [
    (".env", "API=1\n", "protected"),
    ("config.py", "token = 'abcdefghijklmnopqrstuvwxyz'\n", "secret"),
])
def test_policy_blocks_before_approval(test_config, fake_slack, fake_github, path, content, needle):
    sandbox = FakeSandbox(plan=_plan(), edit_func=make_edit(path, content))
    db, worker = _worker(test_config, fake_slack, fake_github, sandbox)
    task = _task(db)
    _run_to_impl_approval(db, worker, task)

    final = db.get_task(task.id)
    assert final.status == TaskStatus.DECLINED
    assert needle in final.error_message
    assert not fake_slack.implementations


def test_failed_checks_block(test_config, fake_slack, fake_github):
    sandbox = FakeSandbox(plan=_plan(), edit_func=make_edit("README.md", "x\n"), checks_rc=1)
    db, worker = _worker(test_config, fake_slack, fake_github, sandbox)
    task = _task(db)
    _run_to_impl_approval(db, worker, task)
    assert db.get_task(task.id).status == TaskStatus.FAILED
    assert not fake_slack.implementations


def test_high_risk_plan_declined(test_config, fake_slack, fake_github):
    db, worker = _worker(test_config, fake_slack, fake_github, FakeSandbox(plan=_plan(risk="high")))
    task = _task(db)
    worker.process_one()
    assert db.get_task(task.id).status == TaskStatus.DECLINED
    assert not fake_slack.plans


def test_plan_touching_protected_path_declined(test_config, fake_slack, fake_github):
    sandbox = FakeSandbox(plan=_plan(files=["src/admin/users.py"]))  # repo extra_deny_paths
    db, worker = _worker(test_config, fake_slack, fake_github, sandbox)
    task = _task(db)
    worker.process_one()
    assert db.get_task(task.id).status == TaskStatus.DECLINED


def test_cancel_during_implementation_stops_quietly(test_config, fake_slack, fake_github):
    holder = {}

    def cancel_midway(repo_dir: Path):
        (repo_dir / "README.md").write_text("x\n")
        holder["db"].transition_status(holder["id"], TaskStatus.IMPLEMENTING, TaskStatus.CANCELLED)

    sandbox = FakeSandbox(plan=_plan(), edit_func=cancel_midway)
    db, worker = _worker(test_config, fake_slack, fake_github, sandbox)
    task = _task(db)
    holder.update(db=db, id=task.id)
    _run_to_impl_approval(db, worker, task)

    assert db.get_task(task.id).status == TaskStatus.CANCELLED
    assert not fake_slack.implementations


def test_disallowed_repo_declined(test_config, fake_slack, fake_github):
    test_config.security.allowed_repos = ["someone/else"]
    db, worker = _worker(test_config, fake_slack, fake_github, FakeSandbox())
    task = _task(db)
    worker.process_one()
    assert db.get_task(task.id).status == TaskStatus.DECLINED


def test_recover_interrupted(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    task = _task(db)
    db.claim_task(task.id, "dead-worker")
    db.update_task_status(task.id, TaskStatus.IMPLEMENTING)
    assert db.recover_interrupted() == [task.id]
    recovered = db.get_task(task.id)
    assert recovered.status == TaskStatus.FAILED and recovered.worker_id is None


def test_transition_is_atomic(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    task = _task(db)
    assert db.transition_status(task.id, TaskStatus.PENDING, TaskStatus.PLANNING)
    assert not db.transition_status(task.id, TaskStatus.PENDING, TaskStatus.PLANNING)


def test_quota_counts_only_active(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    _task(db)
    second = _task(db)
    db.update_task_status(second.id, TaskStatus.COMPLETED)
    assert db.count_active_tasks("U1") == 1
