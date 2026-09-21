"""Tests for configuration loading and validation."""

import os
import sys
import tempfile
from pathlib import Path

import pytest  # type: ignore[import-not-found]

# Make the source package importable when tests are run directly from the
# repository without an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slackagent.config import Config, load_config  # type: ignore[import-not-found]


def test_load_config_with_env_vars(tmp_path: Path) -> None:
    """Test loading configuration with environment variables."""
    # Create a test config file
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
slack:
  bot_token: ${TEST_SLACK_BOT_TOKEN}
  app_token: ${TEST_SLACK_APP_TOKEN}
  signing_secret: ${TEST_SLACK_SIGNING_SECRET}

github:
  app_id: "12345"
  private_key_path: ${TEST_GITHUB_KEY_PATH}
  installation_id: "67890"

gemini:
  api_key: ${TEST_GEMINI_API_KEY}
"""
    )

    # Create a temporary private key file
    key_file = tmp_path / "test_key.pem"
    key_file.write_text("fake key")

    # Set environment variables
    os.environ["TEST_SLACK_BOT_TOKEN"] = "xoxb-test-token"
    os.environ["TEST_SLACK_APP_TOKEN"] = "xapp-test-token"
    os.environ["TEST_SLACK_SIGNING_SECRET"] = "test-secret"
    os.environ["TEST_GITHUB_KEY_PATH"] = str(key_file)
    os.environ["TEST_GEMINI_API_KEY"] = "AIzaSyTest123456789"

    try:
        config = load_config(str(config_file))

        assert config.slack.bot_token == "xoxb-test-token"
        assert config.slack.app_token == "xapp-test-token"
        assert config.slack.signing_secret == "test-secret"
        assert config.github.app_id == "12345"
        assert config.github.private_key_path == str(key_file)
        assert config.gemini.api_key == "AIzaSyTest123456789"

    finally:
        # Clean up environment variables
        for key in [
            "TEST_SLACK_BOT_TOKEN",
            "TEST_SLACK_APP_TOKEN",
            "TEST_SLACK_SIGNING_SECRET",
            "TEST_GITHUB_KEY_PATH",
            "TEST_GEMINI_API_KEY",
        ]:
            os.environ.pop(key, None)


def test_load_config_missing_file() -> None:
    """Test loading non-existent config file."""
    with pytest.raises(FileNotFoundError):
        load_config("nonexistent.yaml")


def test_load_config_from_environment_when_file_missing(tmp_path: Path) -> None:
    """Test loading config from env fallback when no config file is present."""
    temp_key = tmp_path / "gha.pem"
    temp_key.write_text("fake key")

    env_map = {
        "SLACK_BOT_TOKEN": "xoxb-test-token",
        "SLACK_APP_TOKEN": "xapp-test-token",
        "SLACK_SIGNING_SECRET": "test-secret",
        "GITHUB_APP_ID": "12345",
        "GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nfake key\n-----END RSA PRIVATE KEY-----\n",
        "GITHUB_INSTALLATION_ID": "67890",
        "GEMINI_API_KEY": "AIzaSyTest123456789",
    }

    prior = {key: os.environ.get(key) for key in env_map}
    try:
        for key, value in env_map.items():
            os.environ[key] = value

        config = load_config("nonexistent.yaml")

        assert config.slack.bot_token == "xoxb-test-token"
        assert config.slack.app_token == "xapp-test-token"
        assert config.slack.signing_secret == "test-secret"
        assert config.github.app_id == "12345"
        assert config.github.installation_id == "67890"
        assert config.gemini.api_key == "AIzaSyTest123456789"
        assert Path(config.github.private_key_path).exists()
        assert "BEGIN RSA PRIVATE KEY" in Path(config.github.private_key_path).read_text()
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        env_path = Path("/tmp/slack-agent-github-private-key.pem")
        if env_path.exists():
            env_path.unlink()


def test_config_validation_invalid_slack_token(tmp_path: Path) -> None:
    """Test config validation with invalid Slack token."""
    config_file = tmp_path / "config.yaml"
    key_file = tmp_path / "test_key.pem"
    key_file.write_text("fake key")

    config_file.write_text(
        f"""
slack:
  bot_token: invalid-token
  app_token: xapp-test-token
  signing_secret: test-secret

github:
  app_id: "12345"
  private_key_path: {key_file}
  installation_id: "67890"

gemini:
  api_key: sk-ant-test-key
"""
    )

    with pytest.raises(ValueError, match="Slack bot token must start with 'xoxb-'"):
        load_config(str(config_file))


def test_config_validation_missing_key_file(tmp_path: Path) -> None:
    """Test config validation with missing private key file."""
    config_file = tmp_path / "config.yaml"

    config_file.write_text(
        """
slack:
  bot_token: xoxb-test-token
  app_token: xapp-test-token
  signing_secret: test-secret

github:
  app_id: "12345"
  private_key_path: /nonexistent/key.pem
  installation_id: "67890"

gemini:
  api_key: AIzaSyTest123456789
"""
    )

    with pytest.raises(ValueError, match="GitHub private key file not found"):
        load_config(str(config_file))


def test_default_values(tmp_path: Path) -> None:
    """Test that default values are set correctly."""
    config_file = tmp_path / "config.yaml"
    key_file = tmp_path / "test_key.pem"
    key_file.write_text("fake key")

    config_file.write_text(
        f"""
slack:
  bot_token: xoxb-test-token
  app_token: xapp-test-token
  signing_secret: test-secret

github:
  app_id: "12345"
  private_key_path: {key_file}
  installation_id: "67890"

gemini:
  api_key: AIzaSyTest123456789
"""
    )

    config = load_config(str(config_file))

    # Check default values
    assert config.agent.model == "gemini-1.5-pro"
    assert config.agent.max_tokens == 100000
    assert config.security.network_mode == "none"
    assert config.security.require_approval is True
    assert config.docker.image == "slack-coding-agent-sandbox:latest"
