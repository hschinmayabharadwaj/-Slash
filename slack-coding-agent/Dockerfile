# Sandbox image for the coding agent. Built and run locally:
#
#   docker build -t slack-coding-agent-sandbox:latest .
#
# The worker starts one throwaway container per agent run or check, with a
# read-only root filesystem, all capabilities dropped, resource limits and
# (for checks) no network. Nothing in this image holds credentials: the only
# secret a container ever sees is the AI API key, passed at run time.

FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Agent runtime + common check tools. Add your repos' test dependencies here.
RUN pip install --no-cache-dir \
    "google-generativeai>=0.8.0" \
    pytest \
    ruff

# Only the agent code is needed inside the sandbox.
COPY src/slackagent /opt/slackagent/slackagent
ENV PYTHONPATH=/opt/slackagent \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Fallback non-root user (the worker normally runs containers as your own UID).
RUN useradd -m -u 1000 -s /usr/sbin/nologin sandbox \
    && mkdir -p /workspace /io && chown sandbox:sandbox /workspace /io
USER sandbox
WORKDIR /workspace

CMD ["python", "-m", "slackagent.agent_runner", "--help"]
