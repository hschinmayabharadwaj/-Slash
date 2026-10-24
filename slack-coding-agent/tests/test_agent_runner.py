"""Tests for the in-sandbox agent (sim provider) and its confined file tools."""

import os

import pytest

from slackagent.agent_runner import Workspace, parse_reply, run_agent


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "README.md").write_text("# Hi\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / ".env").write_text("SECRET=1\n")
    return root


def test_sim_plan_and_implement(workspace, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "sim")
    inp = {"request": "say hello", "deny_patterns": [".env*"], "max_turns": 5}

    plan = run_agent(inp, "plan", workspace)
    assert plan["ok"] and plan["plan"]["feasible"] and plan["sim"]
    assert not (workspace / "AGENT_SIM.md").exists()

    impl = run_agent({**inp, "plan": plan["plan"]}, "implement", workspace)
    assert impl["ok"] and impl["changed_files"] == ["AGENT_SIM.md"]
    assert "say hello" in (workspace / "AGENT_SIM.md").read_text()


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", ".git/config", ".env", "sub/../../x"])
def test_workspace_refuses_escapes_and_protected(workspace, path):
    ws = Workspace(workspace, [".env*"], writable=True, max_file_bytes=1000)
    assert ws.run({"tool": "read_file", "path": path}).startswith(("denied", "error"))
    assert ws.run({"tool": "write_file", "path": path, "content": "x"}).startswith("denied")
    assert not (workspace.parent / "outside.txt").exists()


def test_workspace_refuses_symlink_escape(workspace, tmp_path):
    (tmp_path / "secret.txt").write_text("top secret")
    os.symlink(tmp_path / "secret.txt", workspace / "link.txt")
    ws = Workspace(workspace, [], writable=True, max_file_bytes=1000)
    assert "top secret" not in ws.run({"tool": "read_file", "path": "link.txt"})
    assert ws.run({"tool": "write_file", "path": "link.txt", "content": "x"}).startswith("denied")
    assert (tmp_path / "secret.txt").read_text() == "top secret"


def test_plan_mode_is_read_only(workspace):
    ws = Workspace(workspace, [], writable=False, max_file_bytes=1000)
    assert ws.run({"tool": "write_file", "path": "a.txt", "content": "x"}).startswith("denied")
    assert ws.run({"tool": "delete_file", "path": "README.md"}).startswith("denied")


def test_write_size_limit(workspace):
    ws = Workspace(workspace, [], writable=True, max_file_bytes=10)
    assert ws.run({"tool": "write_file", "path": "big.txt", "content": "x" * 11}).startswith("denied")


def test_list_hides_git(workspace):
    ws = Workspace(workspace, [], writable=False, max_file_bytes=10)
    assert ".git" not in ws.run({"tool": "list_dir", "path": "."}).split("\n")


def test_parse_reply_variants():
    assert parse_reply('{"final": 1}') == {"final": 1}
    assert parse_reply('```json\n{"final": 2}\n```') == {"final": 2}
    assert parse_reply("not json") is None


def test_unknown_provider_errors(workspace, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "nope")
    from slackagent.agent_runner import AgentError

    with pytest.raises(AgentError):
        run_agent({"request": "x"}, "plan", workspace)
