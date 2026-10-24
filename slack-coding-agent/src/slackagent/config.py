"""Configuration loading and validation.

Which sections are required depends on the entry point (``require``):
``web`` for the browser app, ``slack``/``github`` for the optional Slack bot.

Everything runs locally: a YAML file (``config.yaml``) with ``${ENV_VAR}``
expansion, or -- when no file exists -- environment variables (e.g. from a
local ``.env`` file).
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml  # type: ignore[reportMissingModuleSource]

AI_PROVIDERS = ("gemini", "sim")
NETWORK_MODES = ("none", "bridge")


@dataclass
class SlackConfig:
    """Slack API configuration (Socket Mode -- no public endpoint needed)."""

    bot_token: str
    app_token: str
    signing_secret: str = ""


@dataclass
class GitHubConfig:
    """GitHub App configuration."""

    app_id: str = ""
    private_key_path: str = ""
    installation_id: str = ""
    default_owner: str = ""
    # OAuth credentials of the same GitHub App, used by the web page to let a
    # person sign in and act with *their* permissions (user-to-server token).
    client_id: str = ""
    client_secret: str = ""
    commit_author_name: str = "slack-coding-agent[bot]"
    commit_author_email: str = "slack-coding-agent@users.noreply.github.com"


@dataclass
class AIConfig:
    """AI backend configuration.

    provider:
      ``gemini`` -- Google Gemini (needs ``api_key``)
      ``sim``    -- deterministic offline stand-in; no API key, no network.
    """

    provider: str = "gemini"
    api_key: str = ""
    model: str = "gemini-2.5-flash"


@dataclass
class SecurityConfig:
    """Security and sandbox settings."""

    allowed_users: List[str] = field(default_factory=list)  # empty = everyone
    approvers: List[str] = field(default_factory=list)  # empty = task requester
    allowed_repos: List[str] = field(default_factory=list)  # empty = any installed repo
    protected_paths: List[str] = field(
        default_factory=lambda: [
            ".git/**",
            ".github/workflows/**",
            ".env*",
            "**/secrets/**",
            "**/*.pem",
            "**/*.key",
        ]
    )
    max_file_size_mb: int = 10
    # Network for the agent container. It only needs to reach the AI API;
    # repository code never runs there. Checks (tests/lint) always run with
    # network "none".
    agent_network: str = "bridge"
    network_mode: str = "none"  # network for check containers
    scan_secrets: bool = True
    require_approval: bool = True
    kill_switch_file: str = "./data/KILL_SWITCH"
    max_files: int = 20
    max_changed_lines: int = 500
    max_request_chars: int = 2000
    max_active_tasks_per_user: int = 2
    block_on_failed_checks: bool = True
    max_changes_per_hour: int = 10  # web page: proposals per signed-in user


@dataclass
class AgentConfig:
    """Agent settings."""

    model: str = ""  # legacy; prefer ai.model
    max_tokens: int = 100000
    planning_budget: int = 20000
    implementation_budget: int = 80000
    system_prompt_suffix: str = ""
    max_turns: int = 15
    max_budget_usd: float = 0.50
    check_timeout_s: int = 600
    plan_timeout_s: int = 300
    implement_timeout_s: int = 900


@dataclass
class DatabaseConfig:
    """Database configuration."""

    path: str = "./data/tasks.db"


@dataclass
class DockerConfig:
    """Docker sandbox settings."""

    image: str = "slack-coding-agent-sandbox:latest"
    memory_limit: str = "1g"
    cpu_limit: str = "1.0"
    pids_limit: int = 128
    timeout: int = 300


@dataclass
class WorkerConfig:
    """Worker settings."""

    num_workers: int = 1
    poll_interval: int = 3
    max_retries: int = 3


@dataclass
class WebConfig:
    """Local web page (the primary interface)."""

    host: str = "127.0.0.1"  # keep it on loopback; there is no other login wall
    port: int = 8765
    base_url: str = "http://localhost:8765"  # must match the GitHub App callback URL
    session_hours: int = 8
    encryption_key_file: str = "./data/web_secret.key"  # encrypts stored GitHub tokens


@dataclass
class LoggingConfig:
    """Logging configuration."""

    level: str = "INFO"
    file: str = ""
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


@dataclass
class RepoConfig:
    """Per-repository configuration."""

    lint_command: Optional[str] = None
    test_command: Optional[str] = None
    extra_deny_paths: List[str] = field(default_factory=list)
    default_branch: str = "main"
    enabled: bool = True
    max_files_override: Optional[int] = None
    max_lines_override: Optional[int] = None


@dataclass
class Config:
    """Full application configuration."""

    slack: SlackConfig
    github: GitHubConfig
    ai: AIConfig = field(default_factory=AIConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    web: WebConfig = field(default_factory=WebConfig)
    repos: Dict[str, RepoConfig] = field(default_factory=dict)

    def repo(self, repo_full_name: str) -> RepoConfig:
        """Configuration for ``owner/name`` (defaults if not listed)."""
        return self.repos.get(repo_full_name, RepoConfig())

    def deny_for(self, repo_full_name: str) -> List[str]:
        """Global protected paths plus the repo's extra deny paths."""
        return self.security.protected_paths + self.repo(repo_full_name).extra_deny_paths

    def repo_allowed(self, repo_full_name: str) -> bool:
        """Whether the bot may work on this repository."""
        if not self.repo(repo_full_name).enabled:
            return False
        allowed = self.security.allowed_repos
        return not allowed or repo_full_name.lower() in {r.lower() for r in allowed}


def _expand_env_vars(value: Any) -> Any:
    """Recursively expand ``${VAR}`` references from the environment."""
    if isinstance(value, str):
        for var_name in re.findall(r"\$\{([^}]+)\}", value):
            value = value.replace(f"${{{var_name}}}", os.environ.get(var_name, ""))
        return value
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    return value


def _env_fallback_config() -> Dict[str, Any]:
    """Build a config dict from environment variables (no config.yaml)."""
    return {
        "slack": {
            "bot_token": os.environ.get("SLACK_BOT_TOKEN", ""),
            "app_token": os.environ.get("SLACK_APP_TOKEN", ""),
            "signing_secret": os.environ.get("SLACK_SIGNING_SECRET", ""),
        },
        "github": {
            "app_id": os.environ.get("GITHUB_APP_ID", ""),
            "private_key_path": os.environ.get("GITHUB_PRIVATE_KEY_PATH", ""),
            "installation_id": os.environ.get("GITHUB_INSTALLATION_ID", ""),
            "default_owner": os.environ.get("GITHUB_DEFAULT_OWNER", ""),
            "client_id": os.environ.get("GITHUB_CLIENT_ID", ""),
            "client_secret": os.environ.get("GITHUB_CLIENT_SECRET", ""),
        },
        "ai": {
            "provider": os.environ.get("AI_PROVIDER", "gemini"),
            "api_key": os.environ.get("GEMINI_API_KEY", ""),
            "model": os.environ.get("AI_MODEL", AIConfig.model),
        },
    }


def _has_required_env(env_cfg: Dict[str, Any], require: tuple) -> bool:
    """Whether the env-based fallback has every required value."""
    needed = {
        "slack": ("slack", ["bot_token", "app_token"]),
        "github": ("github", ["app_id", "private_key_path", "installation_id"]),
        "web": ("github", ["client_id", "client_secret"]),
    }
    required: Dict[str, List[str]] = {}
    for name in require:
        section, keys = needed[name]
        required.setdefault(section, []).extend(keys)
    if env_cfg["ai"]["provider"] == "gemini":
        required["ai"] = ["api_key"]
    return all(
        str(env_cfg[section].get(key, "")).strip()
        for section, keys in required.items()
        for key in keys
    )


def _ai_section(raw: Dict[str, Any]) -> Dict[str, Any]:
    """``ai:`` section, accepting the legacy ``gemini: {api_key}`` block."""
    ai = dict(raw.get("ai") or {})
    legacy = raw.get("gemini") or {}
    if legacy.get("api_key") and not ai.get("api_key"):
        ai["api_key"] = legacy["api_key"]
    legacy_model = (raw.get("agent") or {}).get("model")
    if legacy_model and not ai.get("model"):
        ai["model"] = legacy_model
    return ai


def load_config(
    config_path: str = "config.yaml", require: tuple = ("slack", "github")
) -> Config:
    """Load and validate configuration.

    Args:
        config_path: YAML file; if missing, environment variables are used
        require: Sections that must be present and valid. The local CLI
            working on a local checkout needs neither Slack nor GitHub.

    Raises:
        FileNotFoundError: If the file is missing and the environment is incomplete
        ValueError: If the configuration is invalid
    """
    path = Path(config_path)
    if path.exists():
        with open(path, "r") as f:
            raw_config = yaml.safe_load(f) or {}
    else:
        raw_config = _env_fallback_config()
        if require and not _has_required_env(raw_config, require):
            raise FileNotFoundError(f"Configuration file not found: {config_path}")

    raw_config = _expand_env_vars(raw_config)

    for section in require:
        section = "github" if section == "web" else section
        if section not in raw_config:
            raise ValueError(f"Missing required configuration section: {section}")

    try:
        repos_config = {
            name: RepoConfig(**data)
            for name, data in (raw_config.get("repos") or {}).items()
            if isinstance(data, dict)
        }
        config = Config(
            slack=SlackConfig(**(raw_config.get("slack") or {"bot_token": "", "app_token": ""})),
            github=GitHubConfig(**(raw_config.get("github") or {})),
            ai=AIConfig(**_ai_section(raw_config)),
            security=SecurityConfig(**raw_config.get("security", {})),
            agent=AgentConfig(**raw_config.get("agent", {})),
            database=DatabaseConfig(**raw_config.get("database", {})),
            docker=DockerConfig(**raw_config.get("docker", {})),
            worker=WorkerConfig(**raw_config.get("worker", {})),
            logging=LoggingConfig(**raw_config.get("logging", {})),
            web=WebConfig(**raw_config.get("web", {})),
            repos=repos_config,
        )
    except TypeError as e:
        raise ValueError(f"Invalid configuration: {e}") from e

    _validate_config(config, require)
    return config


def _validate_config(config: Config, require: tuple = ("slack", "github")) -> None:
    """Validate configuration values (raises ValueError)."""
    if "slack" in require:
        if not config.slack.bot_token.startswith("xoxb-"):
            raise ValueError("Slack bot token must start with 'xoxb-'")
        if not config.slack.app_token.startswith("xapp-"):
            raise ValueError("Slack app token must start with 'xapp-'")

    if "github" in require:
        key_path = Path(config.github.private_key_path)
        if not key_path.is_file():
            raise ValueError(f"GitHub private key file not found: {config.github.private_key_path}")

    if "web" in require:
        if not (config.github.client_id and config.github.client_secret):
            raise ValueError(
                "github.client_id and github.client_secret (your GitHub App's OAuth "
                "credentials) are required for the web page"
            )
        if not config.web.base_url.startswith(("http://localhost", "http://127.0.0.1", "https://")):
            raise ValueError("web.base_url must be https://, or http://localhost / 127.0.0.1")

    if config.ai.provider not in AI_PROVIDERS:
        raise ValueError(f"ai.provider must be one of {AI_PROVIDERS}, got {config.ai.provider!r}")
    if config.ai.provider == "gemini" and len(config.ai.api_key or "") < 10:
        raise ValueError("ai.api_key (Gemini API key) is missing or looks invalid")

    if config.agent.planning_budget + config.agent.implementation_budget > config.agent.max_tokens:
        raise ValueError("Sum of planning and implementation budgets exceeds max_tokens")

    for name in ("agent_network", "network_mode"):
        value = getattr(config.security, name)
        if value not in NETWORK_MODES:
            raise ValueError(
                f"Invalid security.{name}: {value!r}. Must be one of {NETWORK_MODES}"
            )

    Path(config.database.path).parent.mkdir(parents=True, exist_ok=True)
