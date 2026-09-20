# Sandbox container for agent execution
# This container runs with minimal privileges. No GitHub token and no provider
# API key are provisioned here: the agent authenticates to Amazon Bedrock via
# the task's IAM role (CLAUDE_CODE_USE_BEDROCK=1). If Claude/Bedrock is not
# reachable (or the `claude` binary is absent) the container falls back to a
# deterministic SIM backend so the approval/diff/PR flow still works.

FROM python:3.11-slim

# Install git, basic tools, and build dependencies for cryptography
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    python3-dev \
    cargo \
    rustc \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for execution
RUN useradd -m -u 1000 -s /bin/bash sandbox

# Install the wheel-less code under /opt so `python -m slackagent` works
COPY src/ /opt/slackagent/

# Install the project runtime dependencies required by the bot itself.
# The Slack app imports `slack_bolt`, and the agent code expects the Gemini SDK
# and the JWT crypto stack to be present in the container at runtime.
RUN pip install --no-cache-dir \
    boto3 \
    pyyaml \
    slack-bolt>=1.18.0 \
    google-generativeai>=0.3.0 \
    PyJWT>=2.8.0 \
    cryptography>=41.0.0 \
    requests>=2.31.0 \
    python-dotenv>=1.0.0 \
    pytest \
    black \
    ruff \
    mypy \
    pylint

# Set up working directory (owned by the non-root user so the checkout can be extracted)
WORKDIR /workspace
RUN mkdir -p /workspace /io && chown sandbox:sandbox /workspace /io

ENV PYTHONPATH=/opt/slackagent
ENV CLAUDE_CODE_USE_BEDROCK=1

# Switch to non-root user
USER sandbox

# Default command (will be overridden by the worker's RunTask overrides)
CMD ["python", "-m", "slackagent.fargate_entry", "checks", ":"]