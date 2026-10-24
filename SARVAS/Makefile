# SARVAS: Go agent + FastAPI backend + React web page.
CONFIG ?= config.json
PY := server/.venv/bin/python

# Load secrets from .env (if present) into the environment of every recipe.
-include .env
export

.PHONY: install build test run run-service run-web sandbox-image clean

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
