"""Pytest fixtures for testing."""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

from slackagent.config import Config, load_config
from slackagent.sandbox import SandboxResult


def git(repo: Path, *args: str) -> str:
    """Run git command in repo."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True
    ).stdout


CONFIG_YAML = """
slack:
  bot_token: xoxb-test-token
  app_token: xapp-test-token
  signing_secret: test-secret

github:
  app_id: "123456"
  private_key_path: "{tmp}/test_key.pem"
  installation_id: "789"
  default_owner: "testorg"

gemini:
  api_key: AIzaSyTestKey123456789

security:
  allowed_users: []
  protected_paths:
    - ".git/**"
    - ".env*"
  max_file_size_mb: 10
  network_mode: "none"
  max_files: 20
  max_changed_lines: 500

agent:
  model: "models/gemini-2.5-flash"
  max_tokens: 100000
  planning_budget: 20000
  implementation_budget: 80000
  max_turns: 10

database:
  path: "{tmp}/test.db"

docker:
  image: "slack-coding-agent-sandbox:latest"
  memory_limit: "1g"
  cpu_limit: "1.0"
  timeout: 300

repos:
  testorg/testrepo:
    lint_command: "ruff check ."
    test_command: "pytest -v"
    extra_deny_paths:
      - "src/admin/**"
"""


@pytest.fixture(autouse=True)
def _no_kill_switch(monkeypatch):
    """Disable kill switch for tests."""
    monkeypatch.delenv("BOT_DISABLED", raising=False)


@pytest.fixture
def tmp_path_custom(tmp_path):
    """Custom tmp_path that creates necessary subdirs."""
    (tmp_path / "work").mkdir()
    return tmp_path


@pytest.fixture
def test_config(tmp_path):
    """Create test configuration."""
    key_file = tmp_path / "test_key.pem"
    key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----")
    
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_YAML.format(tmp=tmp_path))
    
    return load_config(str(config_path))


@pytest.fixture
def test_repo(tmp_path) -> Path:
    """Create a test git repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    
    (repo / "README.md").write_text("# Test Repo\n")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('hello')\n")
    
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Initial commit")
    
    return repo


@pytest.fixture
def origin_repo(tmp_path) -> Path:
    """Create a bare git repository."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("# Origin\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "init")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    
    return bare


class FakeSlackHandler:
    """Fake Slack handler for testing."""
    
    def __init__(self):
        self.posts = []
        self.plans = []
        self.implementations = []
        self.prs = []
        self.errors = []
    
    def post_plan(self, task_id: int, plan_summary: str):
        self.plans.append((task_id, plan_summary))
    
    def post_implementation(self, task_id: int, summary: str, diff: str):
        self.implementations.append((task_id, summary, diff))
    
    def post_pr_created(self, task_id: int, pr_url: str):
        self.prs.append((task_id, pr_url))
    
    def post_error(self, task_id: int, error_message: str):
        self.errors.append((task_id, error_message))
    
    class App:
        class Client:
            @staticmethod
            def chat_postMessage(channel, thread_ts, text, blocks=None):
                return {"ts": "123.456"}
        client = Client()
    app = App()


class FakeGitHubClient:
    """Fake GitHub client."""
    
    def __init__(self, origin: Path):
        self.origin = origin
        self.prs = []
    
    def get_clone_url(self, owner: str, name: str) -> str:
        return str(self.origin)
    
    def create_pull_request(self, repo_owner, repo_name, head_branch, title, body, base_branch="main"):
        self.prs.append({"head": head_branch, "base": base_branch, "title": title, "body": body})
        return f"https://github.com/{repo_owner}/{repo_name}/pull/1"


class FakeSandbox:
    """Fake sandbox that writes predefined output."""
    
    def __init__(self, plan=None, edit_func=None, checks_rc=0, timed_out=False):
        self.plan = plan or {
            "feasible": True,
            "summary": "Add a feature",
            "files": ["src/main.py"],
            "risk": "low",
            "risk_reason": "simple change",
            "questions": [],
            "how_to_verify": "Run the code"
        }
        self.edit_func = edit_func or (lambda repo_dir: None)
        self.checks_rc = checks_rc
        self.timed_out = timed_out
        self.runs = []
    
    def run_agent(self, *, mode, repo_dir, io_dir, image, timeout_s, api_key):
        self.runs.append({"mode": mode, "repo_dir": str(repo_dir)})
        
        if self.timed_out:
            return SandboxResult(-9, True, "", "")
        
        io_dir = Path(io_dir)
        io_dir.mkdir(parents=True, exist_ok=True)
        
        if mode == "plan":
            output = {"ok": True, "text": json.dumps(self.plan), "plan": self.plan, "cost_usd": 0.1, "num_turns": 1}
        else:
            self.edit_func(Path(repo_dir))
            output = {"ok": True, "text": "Made changes", "cost_usd": 0.2, "num_turns": 3}
        
        (io_dir / "output.json").write_text(json.dumps(output))
        return SandboxResult(0, False, "", "")
    
    def run_checks(self, *, repo_dir, image, command, timeout_s):
        return SandboxResult(self.checks_rc, False, "test output", "")


@pytest.fixture
def fake_slack():
    return FakeSlackHandler()


@pytest.fixture
def fake_github(origin_repo):
    return FakeGitHubClient(origin_repo)


@pytest.fixture
def fake_sandbox():
    return FakeSandbox()


def make_edit(filepath: str, content: str):
    """Create an edit function."""
    def edit(repo_dir: Path):
        (repo_dir / filepath).write_text(content)
    return edit