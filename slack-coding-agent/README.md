# Slack Coding Agent

A security-hardened Slack bot that uses Google Gemini to help with coding tasks through GitHub integration, featuring sandboxed execution, approval workflows, and comprehensive safety controls.

## Features

- **Slack Integration**: Mention the bot in any channel to request coding assistance
- **GitHub App Authentication**: Secure repository access with fine-grained permissions
- **Sandboxed Execution**: Isolated Docker containers for all code execution
- **Google Gemini AI**: Powered by Gemini 1.5 Pro for intelligent code generation
- **Approval Workflows**: Human-in-the-loop review for sensitive operations
- **Security Hardening**:
  - Secret scanning and sanitization
  - Path-based access controls
  - Network isolation options
  - User allowlisting
  - Diff-based change detection
- **Multi-stage Pipeline**: Separate planning and implementation phases
- **Automated Checks**: Lint and test execution in isolated environments

## Architecture

```
┌─────────────┐
│   Slack     │
│   Events    │
└──────┬──────┘
       │
       ▼
┌─────────────────┐
│  Event Handler  │
│  (mentions,     │
│   approvals)    │
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│  Worker Queue   │
│  (plan → impl)  │
└──────┬──────────┘
       │
       ▼
┌─────────────────┐      ┌──────────────┐
│  Agent Runner   │─────▶│   Docker     │
│  (sandboxed)    │      │   Container  │
└──────┬──────────┘      └──────────────┘
       │
       ▼
┌─────────────────┐
│  GitHub Ops     │
│  (commit, PR)   │
└─────────────────┘
```

## Installation

### Prerequisites

- Python 3.9+
- Docker (for sandboxed execution)
- GitHub App credentials
- Slack App credentials
- Google Gemini API key

### Setup

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd slack-coding-agent
   ```

2. **Install dependencies**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -e .
   ```

3. **Configure the application**:
   
   Create `config.yaml`:
   ```yaml
   slack:
     bot_token: ${SLACK_BOT_TOKEN}
     app_token: ${SLACK_APP_TOKEN}
     signing_secret: ${SLACK_SIGNING_SECRET}
   
   github:
     app_id: ${GITHUB_APP_ID}
     private_key_path: ${GITHUB_PRIVATE_KEY_PATH}
     installation_id: ${GITHUB_INSTALLATION_ID}
   
   gemini:
     api_key: ${GEMINI_API_KEY}
   
   security:
     allowed_users: []  # Empty = all users allowed
     protected_paths:
       - ".git/**"
       - ".env*"
       - "**/secrets/**"
     max_file_size_mb: 10
     network_mode: "none"  # Docker network isolation
   
   agent:
     model: "gemini-1.5-pro"
     max_tokens: 100000
     planning_budget: 20000
     implementation_budget: 80000
   
   database:
     path: "./data/tasks.db"
   ```

4. **Set environment variables**:
   ```bash
   export SLACK_BOT_TOKEN="xoxb-..."
   export SLACK_APP_TOKEN="xapp-..."
   export SLACK_SIGNING_SECRET="..."
   export GITHUB_APP_ID="123456"
   export GITHUB_PRIVATE_KEY_PATH="./github-app-key.pem"
   export GITHUB_INSTALLATION_ID="..."
   export GEMINI_API_KEY="AIza..."
   ```

5. **Build the Docker image**:
   ```bash
   docker build -t slack-coding-agent-sandbox .
   ```

## Usage

### Start the Slack bot

```bash
python -m slackagent.main
```

### Interact via Slack

Mention the bot in any channel:

```
@codingbot can you add input validation to the user registration endpoint?
```

The bot will:
1. Create a planning task
2. Present a plan in the thread
3. Wait for approval (thumbs up reaction)
4. Execute the implementation
5. Show a diff and wait for final approval
6. Create a pull request

### Local CLI Mode

For testing without Slack:

```bash
python -m slackagent.cli --repo /path/to/repo --task "Add logging to API endpoints"
```

## Configuration

### Security Settings

- **allowed_users**: List of Slack user IDs permitted to use the bot (empty = all users)
- **protected_paths**: Glob patterns for files the agent cannot modify
- **max_file_size_mb**: Maximum file size the agent can read/write
- **network_mode**: Docker network isolation ("none" recommended)

### Agent Settings

- **model**: Gemini model to use
- **max_tokens**: Total token budget per task
- **planning_budget**: Tokens allocated for planning phase
- **implementation_budget**: Tokens allocated for implementation phase

## Development

### Running Tests

```bash
pytest tests/
```

### Project Structure

```
slack-coding-agent/
├── src/slackagent/
│   ├── __init__.py
│   ├── main.py              # Main entry point
│   ├── config.py            # Configuration loading
│   ├── security.py          # Security controls
│   ├── github_ops.py        # GitHub integration
│   ├── agent_runner.py      # Sandboxed agent execution
│   ├── database.py          # Task state management
│   ├── slack_handler.py     # Slack event handling
│   ├── worker.py            # Task worker pipeline
│   ├── tools.py             # Agent tool definitions
│   └── cli.py               # CLI entry point
├── tests/
│   ├── test_config.py
│   ├── test_security.py
│   ├── test_github_ops.py
│   └── test_worker.py
├── data/
│   └── .gitkeep
├── Dockerfile               # Sandbox container
├── pyproject.toml          # Dependencies
├── config.yaml.example     # Example configuration
└── README.md
```

## Safety and Security

This project implements defense-in-depth security:

1. **Input Validation**: All user input is validated and sanitized
2. **Path Restrictions**: Protected files cannot be accessed
3. **Secret Scanning**: Diffs are scanned for leaked credentials
4. **Container Isolation**: All code runs in unprivileged containers
5. **Network Isolation**: Optional network blocking for containers
6. **Approval Gates**: Human review required for sensitive operations
7. **User Allowlisting**: Optional restriction to trusted users

## Troubleshooting

### Container fails to start

Check Docker is running and the image is built:
```bash
docker images | grep slack-coding-agent-sandbox
docker build -t slack-coding-agent-sandbox .
```

### GitHub authentication fails

Verify your GitHub App credentials and that the app is installed on the target repositories.

### Agent times out

Increase token budgets in `config.yaml` or simplify the task scope.

## License

MIT

## Contributing

Contributions welcome! Please open an issue before submitting large changes.
