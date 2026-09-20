"""Configuration loading and validation."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Optional

import yaml  # type: ignore[reportMissingModuleSource]


@dataclass
class SlackConfig:
    """Slack API configuration."""

    bot_token: str
    app_token: str
    signing_secret: str


@dataclass
class GitHubConfig:
    """GitHub App configuration."""

    app_id: str
    private_key_path: str
    installation_id: str
    default_owner: str = ""


@dataclass
class GeminiConfig:
    """Google Gemini API configuration."""

    api_key: str


@dataclass
class SecurityConfig:
    """Security and sandbox settings."""

    allowed_users: List[str] = field(default_factory=list)
    protected_paths: List[str] = field(
        default_factory=lambda: [
            ".git/**",
            ".env*",
            "**/secrets/**",
            "**/*.pem",
            "**/*.key",
        ]
    )
    max_file_size_mb: int = 10
    network_mode: str = "none"
    scan_secrets: bool = True
    require_approval: bool = True
    kill_switch_file: str = "./data/KILL_SWITCH"  # Kill switch file path
    max_files: int = 20  # Max files changed per task
    max_changed_lines: int = 500  # Max lines changed per task


@dataclass
class AgentConfig:
    """Gemini agent settings."""

    model: str = "gemini-1.5-pro"
    max_tokens: int = 100000
    planning_budget: int = 20000
    implementation_budget: int = 80000
    system_prompt_suffix: str = ""
    max_turns: int = 10  # Maximum conversation turns
    max_budget_usd: float = 0.50  # Cost cap per task
    check_timeout_s: int = 600  # Timeout for checks


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
    timeout: int = 300


@dataclass
class WorkerConfig:
    """Worker process settings."""

    num_workers: int = 2
    poll_interval: int = 5
    max_retries: int = 3


@dataclass
class LoggingConfig:
    """Logging configuration."""

    level: str = "INFO"
    file: str = ""
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


@dataclass
class RepoConfig:
    """Per-repository configuration."""
    
    lint_command: Optional[str] = None  # Custom lint command
    test_command: Optional[str] = None  # Custom test command
    extra_deny_paths: List[str] = field(default_factory=list)  # Additional protected paths
    default_branch: str = "main"  # Default branch for PRs
    enabled: bool = True  # Whether bot is enabled for this repo
    max_files_override: Optional[int] = None  # Override global max_files
    max_lines_override: Optional[int] = None  # Override global max_lines


@dataclass
class Config:
    """Full application configuration."""

    slack: SlackConfig
    github: GitHubConfig
    gemini: GeminiConfig
    security: SecurityConfig = field(default_factory=SecurityConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    repos: Dict[str, RepoConfig] = field(default_factory=dict)  # Per-repo settings
    
    def repo(self, repo_full_name: str) -> RepoConfig:
        """Get configuration for a specific repository.
        
        Args:
            repo_full_name: Repository in format "owner/name"
            
        Returns:
            RepoConfig for the repository (or default)
        """
        return self.repos.get(repo_full_name, RepoConfig())
    
    def deny_for(self, repo_full_name: str) -> List[str]:
        """Get deny patterns for a repository.
        
        Args:
            repo_full_name: Repository in format "owner/name"
            
        Returns:
            Combined list of global and repo-specific deny patterns
        """
        repo_cfg = self.repo(repo_full_name)
        return self.security.protected_paths + repo_cfg.extra_deny_paths


def _expand_env_vars(value: Any) -> Any:
    """Recursively expand environment variables in configuration values."""
    if isinstance(value, str):
        # Replace ${VAR_NAME} with environment variable value
        pattern = r"\$\{([^}]+)\}"
        matches = re.findall(pattern, value)
        for var_name in matches:
            env_value = os.environ.get(var_name, "")
            value = value.replace(f"${{{var_name}}}", env_value)
        return value
    elif isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    else:
        return value


def load_config(config_path: str = "config.yaml") -> Config:
    """Load and validate configuration from YAML file.

    Args:
        config_path: Path to configuration file

    Returns:
        Validated Config object

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If configuration is invalid
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(path, "r") as f:
        raw_config = yaml.safe_load(f)

    # Expand environment variables
    raw_config = _expand_env_vars(raw_config)

    # Validate required sections
    required_sections = ["slack", "github", "gemini"]
    for section in required_sections:
        if section not in raw_config:
            raise ValueError(f"Missing required configuration section: {section}")

    # Parse configuration sections
    try:
        slack_config = SlackConfig(**raw_config["slack"])
        github_config = GitHubConfig(**raw_config["github"])
        gemini_config = GeminiConfig(**raw_config["gemini"])

        # Optional sections with defaults
        security_config = SecurityConfig(**raw_config.get("security", {}))
        agent_config = AgentConfig(**raw_config.get("agent", {}))
        database_config = DatabaseConfig(**raw_config.get("database", {}))
        docker_config = DockerConfig(**raw_config.get("docker", {}))
        worker_config = WorkerConfig(**raw_config.get("worker", {}))
        logging_config = LoggingConfig(**raw_config.get("logging", {}))
        
        # Per-repo configurations
        repos_config = {}
        if "repos" in raw_config and isinstance(raw_config["repos"], dict):
            for repo_name, repo_data in raw_config["repos"].items():
                if isinstance(repo_data, dict):
                    repos_config[repo_name] = RepoConfig(**repo_data)

        config = Config(
            slack=slack_config,
            github=github_config,
            gemini=gemini_config,
            security=security_config,
            agent=agent_config,
            database=database_config,
            docker=docker_config,
            worker=worker_config,
            logging=logging_config,
            repos=repos_config,
        )

        # Validate configuration
        _validate_config(config)

        return config

    except TypeError as e:
        raise ValueError(f"Invalid configuration: {e}")


def _validate_config(config: Config) -> None:
    """Validate configuration values.

    Args:
        config: Configuration to validate

    Raises:
        ValueError: If configuration is invalid
    """
    # Validate Slack tokens
    if not config.slack.bot_token.startswith("xoxb-"):
        raise ValueError("Slack bot token must start with 'xoxb-'")
    if not config.slack.app_token.startswith("xapp-"):
        raise ValueError("Slack app token must start with 'xapp-'")

    # Validate GitHub private key path
    key_path = Path(config.github.private_key_path)
    if not key_path.exists():
        raise ValueError(f"GitHub private key file not found: {config.github.private_key_path}")

    # Validate Gemini API key (basic check - not empty)
    if not config.gemini.api_key or len(config.gemini.api_key) < 20:
        raise ValueError("Gemini API key appears to be invalid")

    # Validate token budgets
    if config.agent.planning_budget + config.agent.implementation_budget > config.agent.max_tokens:
        raise ValueError(
            "Sum of planning and implementation budgets exceeds max_tokens"
        )

    # Validate network mode
    if config.security.network_mode not in ["none", "bridge", "host"]:
        raise ValueError(
            f"Invalid network_mode: {config.security.network_mode}. "
            "Must be 'none', 'bridge', or 'host'"
        )

    # Validate database path directory exists
    db_path = Path(config.database.path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
