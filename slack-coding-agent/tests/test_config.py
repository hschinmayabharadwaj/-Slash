"""Tests for configuration loading and validation."""

import os
import tempfile
from pathlib import Path

import pytest

from slackagent.config import Config, load_config


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

anthropic:
  api_key: ${TEST_ANTHROPIC_API_KEY}
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
    os.environ["TEST_ANTHROPIC_API_KEY"] = "sk-ant-test-key"

    try:
        config = load_config(str(config_file))

        assert config.slack.bot_token == "xoxb-test-token"
        assert config.slack.app_token == "xapp-test-token"
        assert config.slack.signing_secret == "test-secret"
        assert config.github.app_id == "12345"
        assert config.github.private_key_path == str(key_file)
        assert config.anthropic.api_key == "sk-ant-test-key"

    finally:
        # Clean up environment variables
        for key in [
            "TEST_SLACK_BOT_TOKEN",
            "TEST_SLACK_APP_TOKEN",
            "TEST_SLACK_SIGNING_SECRET",
            "TEST_GITHUB_KEY_PATH",
            "TEST_ANTHROPIC_API_KEY",
        ]:
            os.environ.pop(key, None)


def test_load_config_missing_file() -> None:
    """Test loading non-existent config file."""
    with pytest.raises(FileNotFoundError):
        load_config("nonexistent.yaml")


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

anthropic:
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

anthropic:
  api_key: sk-ant-test-key
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

anthropic:
  api_key: sk-ant-test-key
"""
    )

    config = load_config(str(config_file))

    # Check default values
    assert config.agent.model == "claude-sonnet-4"
    assert config.agent.max_tokens == 100000
    assert config.security.network_mode == "none"
    assert config.security.require_approval is True
    assert config.docker.image == "slack-coding-agent-sandbox:latest"
