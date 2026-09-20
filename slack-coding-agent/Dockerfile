# Sandbox container for agent execution
# This container runs with minimal privileges. No GitHub token and no provider
# API key are provisioned here: the agent authenticates to Amazon Bedrock via
# the task's IAM role (CLAUDE_CODE_USE_BEDROCK=1). If Claude/Bedrock is not
# reachable (or the `claude` binary is absent) the container falls back to a
# deterministic SIM backend so the approval/diff/PR flow still works.

FROM python:3.11-slim

# Install git and basic tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for execution
RUN useradd -m -u 1000 -s /bin/bash sandbox

# Install the wheel-less code under /opt so `python -m slackagent` works
COPY src/ /opt/slackagent/

# Install Python dependencies for the S3 bridge + common dev tasks
RUN pip install --no-cache-dir \
    boto3 \
    pytest \
    black \
    ruff \
    mypy \
    pylint

# Set up working directory
WORKDIR /workspace

ENV PYTHONPATH=/opt/slackagent
ENV CLAUDE_CODE_USE_BEDROCK=1

# Switch to non-root user
USER sandbox

# Default command (will be overridden by the worker's RunTask overrides)
CMD ["python", "-m", "slackagent.fargate_entry", "checks", ":"]