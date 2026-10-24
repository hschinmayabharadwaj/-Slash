"""Tests for Docker argument hardening and credential-free git handling."""

from unittest.mock import patch

from slackagent.config import DockerConfig, GitHubConfig
from slackagent.github_ops import GitHubClient, GitOperations, _redact


class _FakePopen:
    calls = []

    def __init__(self, args, env=None, **kwargs):
        _FakePopen.calls.append((args, env))
        self.returncode = 0

    def communicate(self, timeout=None):
        return "", ""


def _run(sandbox_kwargs, method, **kwargs):
    from slackagent.sandbox import DockerSandbox

    _FakePopen.calls = []
    with patch("slackagent.sandbox.subprocess.Popen", _FakePopen):
        getattr(DockerSandbox(DockerConfig(), **sandbox_kwargs), method)(**kwargs)
    return _FakePopen.calls[0]


def _value_after(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def test_api_key_never_in_argv(tmp_path):
    args, env = _run(
        {"agent_network": "bridge", "ai_provider": "gemini"}, "run_agent",
        mode="plan", repo_dir=tmp_path, io_dir=tmp_path, image="img", timeout_s=5, api_key="AIzaSECRET",
    )
    assert not any("AIzaSECRET" in a for a in args)
    assert env["GEMINI_API_KEY"] == "AIzaSECRET"
    assert "GEMINI_API_KEY" in _value_after(args, "-e")


def test_hardening_flags_and_readonly_plan(tmp_path):
    (tmp_path / ".git").mkdir()
    args, _ = _run({}, "run_agent", mode="plan", repo_dir=tmp_path, io_dir=tmp_path,
                   image="img", timeout_s=5, api_key="")
    for flag in ("--read-only", "--cap-drop", "--security-opt", "--pids-limit", "--memory"):
        assert flag in args
    mounts = _value_after(args, "-v")
    assert f"{tmp_path.resolve()}:/workspace:ro" in mounts
    assert f"{tmp_path.resolve() / '.git'}:/workspace/.git:ro" in mounts


def test_checks_and_sim_have_no_network(tmp_path):
    args, _ = _run({"network_mode": "none"}, "run_checks",
                   repo_dir=tmp_path, image="img", command="pytest", timeout_s=5)
    assert _value_after(args, "--network") == ["none"]
    args, _ = _run({"agent_network": "bridge", "ai_provider": "sim"}, "run_agent",
                   mode="implement", repo_dir=tmp_path, io_dir=tmp_path, image="img", timeout_s=5, api_key="")
    assert _value_after(args, "--network") == ["none"]


def test_clone_url_has_no_credentials(tmp_path):
    client = GitHubClient(GitHubConfig(app_id="1", private_key_path="x", installation_id="2"))
    with patch.object(client, "_get_installation_token", return_value="ghs_abcdefghijklmnop"):
        url = client.get_clone_url("org", "repo")
        env = client.git_auth_env("org", "repo")
    assert "ghs_" not in url and "@" not in url
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "ghs_" not in env["GIT_CONFIG_VALUE_0"]  # base64-encoded


def test_clone_does_not_persist_auth(origin_repo, tmp_path):
    dest = tmp_path / "clone"
    GitOperations.clone_repository(
        str(origin_repo), dest,
        env={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader",
             "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic c2VjcmV0"},
    )
    assert "c2VjcmV0" not in (dest / ".git" / "config").read_text()


def test_invalid_repo_names_rejected():
    import pytest
    from slackagent.github_ops import GitHubOperationError, validate_repo_name

    for owner, name in [("..", "x"), ("a b", "c"), ("org", "repo;rm"), ("", "x")]:
        with pytest.raises(GitHubOperationError):
            validate_repo_name(owner, name)


def test_redact_tokens():
    assert "ghs_" not in _redact("fatal: https://x-access-token:ghs_abcdefghijklmn@github.com")
