# Sandbox container for agent execution
# This container runs with minimal privileges and no network access by default

FROM python:3.11-slim

# Install git and basic tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for execution
RUN useradd -m -u 1000 -s /bin/bash sandbox

# Set up working directory
WORKDIR /workspace

# Install Python dependencies for common dev tasks
# This can be extended based on the languages/frameworks you support
RUN pip install --no-cache-dir \
    pytest \
    black \
    ruff \
    mypy \
    pylint

# Switch to non-root user
USER sandbox

# Default command (will be overridden by agent runner)
CMD ["/bin/bash"]
