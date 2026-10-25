# SARVAS: Go agent + FastAPI backend + React web page.
CONFIG ?= config.json
PY := server/.venv/bin/python

# Load secrets from .env (if present) into the environment of every recipe.
-include .env
export

.PHONY: install build test lint run run-service run-web sandbox-image setup check up down clean

install:
	uv venv --allow-existing --python 3.12 server/.venv
	uv pip install --python $(PY) -r server/requirements-dev.txt
	cd client && npm ci --no-audit --no-fund

build:
	cd agent && CGO_ENABLED=0 go build -trimpath -o bin/ ./cmd/...
	cd client && npm run build

test:
	cd agent && go vet ./... && go test ./...
	cd server && .venv/bin/python -m pytest -q tests
	cd client && npm run build

lint:
	cd agent && test -z "$$(gofmt -l .)" && go vet ./...
	cd server && .venv/bin/ruff check . ../scripts && .venv/bin/mypy

# --- Docker Compose (self-hosting)
setup:
	python3 scripts/setup.py

check:
	python3 scripts/setup.py --check

up:
	docker compose $(if $(filter ollama,$(AI_PROVIDER)),--profile local-ai) up -d --build

down:
	docker compose --profile local-ai down

sandbox-image:
	docker build -f sandbox/Dockerfile -t sarvas-sandbox:latest .

run-service: build
	./agent/bin/agent-service -config $(CONFIG)

run-web:
	cd server && .venv/bin/python -m sarvas_web ../$(CONFIG)

# Runs both; Ctrl-C stops both.
run: build
	@trap 'kill 0' INT TERM; \
	./agent/bin/agent-service -config $(CONFIG) & \
	(cd server && .venv/bin/python -m sarvas_web ../$(CONFIG)) & \
	wait

clean:
	rm -rf agent/bin client/dist
