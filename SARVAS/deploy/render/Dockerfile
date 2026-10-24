# SARVAS backend on Render (free web service): FastAPI + Go agent-service.
# The React page is deployed separately on Vercel, which forwards /api and
# /auth here (see client/vercel.json).
#
# Render settings: Root Directory = SARVAS, Dockerfile Path = deploy/render/Dockerfile,
# Docker Build Context = . (the SARVAS folder). Local check:
#   docker build -f deploy/render/Dockerfile -t sarvas-api .

# --- Go agent-service (stdlib only, static binary)
FROM golang:1.24-bookworm AS agent
WORKDIR /src
COPY agent/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/agent-service ./cmd/agent-service

# --- Runtime: FastAPI + agent-service, as a non-root user
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server/sarvas_web ./sarvas_web
COPY --from=agent /out/agent-service ./bin/agent-service
COPY deploy/render/app.py deploy/render/config.json ./
RUN useradd -m -u 1000 app && mkdir data && chown app:app data
USER app
CMD ["python", "app.py"]
