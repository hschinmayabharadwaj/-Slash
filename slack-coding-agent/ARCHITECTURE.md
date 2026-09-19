# Slack Coding Agent - Architecture Overview

## Project Structure

```
slack-coding-agent/
├── README.md                      # Main documentation
├── pyproject.toml                 # Python package configuration
├── Dockerfile                     # Sandbox container image
├── config.yaml.example            # Configuration template
├── .gitignore                     # Git ignore rules
│
├── src/slackagent/               # Main application code
│   ├── __init__.py
│   ├── config.py                 # Configuration loading and validation
│   ├── security.py               # Security controls and sandboxing
│   ├── database.py               # SQLite database and task management
│   ├── github_ops.py             # GitHub App authentication and operations
│   ├── agent_runner.py           # Gemini agent with Docker sandbox
│   ├── slack_handler.py          # Slack event handlers and bot logic
│   ├── worker.py                 # Background worker for task processing
│   ├── main.py                   # Main entry point (Slack bot)
│   └── cli.py                    # CLI interface for local testing
│
├── tests/                        # Test suite
│   ├── test_config.py
│   ├── test_database.py
│   └── test_security.py
│
└── data/                         # Runtime data directory
    └── tasks.db                  # SQLite database (created at runtime)
```

## Component Architecture

### 1. Configuration System (`config.py`)
- YAML-based configuration with environment variable expansion
- Dataclass-based configuration with type safety
- Validation on load (token formats, file existence, etc.)
- Sections: Slack, GitHub, Gemini, Security, Agent, Docker, Worker, Logging

### 2. Security Layer (`security.py`)
**PathGuard**: Prevents access to protected files
- Glob pattern matching (supports `**/*.pem`, `.env*`, etc.)
- Checks both full paths and basenames

**SecretScanner**: Detects secrets in code and diffs
- Pattern-based detection (API keys, tokens, passwords, etc.)
- Scans only added lines in diffs (ignores removed lines)
- Supports 13+ secret types (Google/Gemini, Anthropic, Slack, GitHub, AWS, etc.)

**UserValidator**: Controls who can use the bot
- Allowlist-based (empty = allow all)
- Per-user authorization checks

**InputSanitizer**: Prevents injection attacks
- Shell argument validation
- Path traversal detection

### 3. Database (`database.py`)
**Schema**:
- Tasks table with full lifecycle tracking
- Indexes on status, thread, and worker_id

**Task States**:
```
PENDING → PLANNING → AWAITING_PLAN_APPROVAL
                           ↓ (approve)
                     PLAN_APPROVED → IMPLEMENTING → AWAITING_IMPL_APPROVAL
                                                           ↓ (approve)
                                                     IMPL_APPROVED → CREATING_PR → COMPLETED
```

**Features**:
- Worker claiming (prevents concurrent processing)
- Thread-based task lookup for Slack integration
- Atomic status updates

### 4. GitHub Integration (`github_ops.py`)
**GitHubClient**: GitHub App authentication
- JWT generation for App authentication
- Installation token caching (1 hour expiry)
- Branch creation and PR creation via API

**GitOperations**: Git command execution
- Clone, branch, commit, push operations
- Hardened with timeouts and error handling
- Diff generation for change preview

### 5. Agent Runner (`agent_runner.py`)
**SandboxRunner**: Docker-based isolation
- Unprivileged execution (non-root user)
- Read-only root filesystem
- Memory and CPU limits
- Network isolation (configurable)
- Temporary writable `/tmp`

**AgentRunner**: Gemini integration
- Planning phase: Analyze and create implementation plan
- Implementation phase: Execute approved plan
- Checks: Run linters and tests in sandbox
- Token budget management (separate for plan/impl)

### 6. Slack Integration (`slack_handler.py`)
**Event Handlers**:
- `app_mention`: Create tasks from bot mentions
- `approve_plan`: Handle plan approval button
- `reject_plan`: Handle plan rejection button
- `approve_impl`: Handle implementation approval button
- `reject_impl`: Handle implementation rejection button

**Message Formatting**:
- Block Kit for rich interactive messages
- Approval buttons with task IDs
- Diff previews (truncated for display)

**Security**:
- User validation before task creation
- Only task creator can approve/reject
- Ephemeral messages for errors

### 7. Worker Pipeline (`worker.py`)
**Worker Loop**:
1. Poll database for pending tasks
2. Claim task (atomic operation)
3. Process based on status:
   - PENDING → Run planning phase
   - PLAN_APPROVED → Run implementation phase
   - IMPL_APPROVED → Create pull request
4. Post results to Slack
5. Release task

**Error Handling**:
- Task failures update status and notify Slack
- Worker crashes release claimed tasks
- Retry logic configurable

### 8. CLI Mode (`cli.py`)
Local testing without Slack:
- Clone repository or use local path
- Interactive approval prompts
- Diff and check result display
- Optional commit after implementation

## Security Architecture

### Defense in Depth

1. **Input Validation**
   - User authorization (allowlist)
   - Shell argument sanitization
   - Path traversal detection

2. **File Access Control**
   - Protected path patterns (`.git/**`, `.env*`, etc.)
   - File size limits
   - Glob-based matching

3. **Secret Detection**
   - Pre-commit diff scanning
   - 13+ secret pattern types
   - Blocks commits with detected secrets

4. **Container Isolation**
   - Unprivileged user (UID 1000)
   - Read-only root filesystem
   - Network isolation (none/bridge)
   - Resource limits (memory, CPU)
   - Temporary `/tmp` (100MB, noexec)

5. **Code Review Gates**
   - Plan approval before implementation
   - Implementation approval before PR
   - Human-in-the-loop for all changes

## Workflow Example

### User Request
```
@codingbot myorg/myrepo Add input validation to the login endpoint
```

### System Flow

1. **Slack Handler** receives mention
   - Validates user authorization
   - Creates task in database (status: PENDING)
   - Posts acknowledgment to thread

2. **Worker** picks up task
   - Updates status to PLANNING
   - Clones repository to temp directory
   - Runs Claude planning phase
   - Posts plan to Slack with approval buttons
   - Updates status to AWAITING_PLAN_APPROVAL

3. **User** reviews plan and clicks "Approve"
   - Slack Handler updates status to PLAN_APPROVED

4. **Worker** continues processing
   - Updates status to IMPLEMENTING
   - Creates new branch
   - Runs Claude implementation phase
   - Commits changes and gets diff
   - Scans diff for secrets
   - Runs lint and test checks
   - Posts results to Slack with approval buttons
   - Updates status to AWAITING_IMPL_APPROVAL

5. **User** reviews changes and clicks "Approve & Create PR"
   - Slack Handler updates status to IMPL_APPROVED

6. **Worker** creates pull request
   - Updates status to CREATING_PR
   - Re-applies changes to branch
   - Pushes branch to GitHub
   - Creates pull request via API
   - Posts PR link to Slack
   - Updates status to COMPLETED

## Configuration

### Required Secrets
- `SLACK_BOT_TOKEN`: Bot User OAuth Token (xoxb-*)
- `SLACK_APP_TOKEN`: App-Level Token (xapp-*)
- `SLACK_SIGNING_SECRET`: Request verification
- `GITHUB_APP_ID`: GitHub App ID
- `GITHUB_PRIVATE_KEY_PATH`: Path to .pem file
- `GITHUB_INSTALLATION_ID`: Installation ID
- `GEMINI_API_KEY`: Google Gemini API key (AIza...)

### Security Settings
```yaml
security:
  allowed_users: []           # Empty = allow all
  protected_paths:
    - ".git/**"
    - ".env*"
    - "**/secrets/**"
  max_file_size_mb: 10
  network_mode: "none"        # Docker network isolation
  scan_secrets: true
  require_approval: true
```

### Agent Settings
```yaml
agent:
  model: "claude-sonnet-4"
  max_tokens: 100000
  planning_budget: 20000
  implementation_budget: 80000
```

## Deployment

### Prerequisites
1. Docker installed and running
2. GitHub App created and installed
3. Slack App created with Socket Mode

### Setup Steps
1. Clone repository
2. Install dependencies: `pip install -e .`
3. Copy `config.yaml.example` to `config.yaml`
4. Set environment variables
5. Build Docker image: `docker build -t slack-coding-agent-sandbox .`
6. Start bot: `python -m slackagent.main`
7. Start worker: `python -m slackagent.worker` (in separate terminal)

### Production Considerations
- Run multiple worker processes for concurrency
- Use process manager (systemd, supervisord)
- Set up log rotation
- Monitor database size
- Implement cleanup job for old tasks
- Consider container registry for sandbox image
- Set up health checks and alerting

## Testing

### Unit Tests
```bash
pytest tests/
```

### Local CLI Testing
```bash
python -m slackagent.cli --repo /path/to/repo --task "Add feature X"
```

## Extension Points

1. **Custom Tools**: Add agent tools in `agent_runner.py`
2. **Language Support**: Extend checks in `agent_runner.py`
3. **Custom Patterns**: Add secret patterns in `security.py`
4. **Notifications**: Add channels in `slack_handler.py`
5. **Metrics**: Add instrumentation throughout

## License

MIT
