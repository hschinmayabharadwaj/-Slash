# Sandbox image: the Go agent-runner plus tools for running repository checks.
#
#   docker build -t coding-agent-sandbox:latest .
#
# The pipeline starts one throwaway container per agent run or check with a
# read-only root filesystem, all capabilities dropped, resource limits and
# (for checks) no network. No credentials are baked in; the only secret a
# container ever sees is the AI API key, passed at run time.

FROM golang:1.24-bookworm AS build
WORKDIR /src
COPY agent/go.mod ./
COPY agent/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/agent-runner ./cmd/agent-runner

FROM debian:bookworm-slim
# git for diffs; add the toolchains your repositories' lint/test commands need
# (e.g. python3 + pytest, nodejs + npm, golang) in a derived image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/agent-runner /usr/local/bin/agent-runner
RUN useradd -m -u 1000 -s /usr/sbin/nologin sandbox \
    && mkdir -p /workspace /io && chown sandbox:sandbox /workspace /io
USER sandbox
WORKDIR /workspace
ENTRYPOINT []
CMD ["/usr/local/bin/agent-runner", "--help"]
