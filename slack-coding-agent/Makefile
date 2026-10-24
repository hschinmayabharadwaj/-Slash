# Coding Agent: Go agent + Node/React web page.
CONFIG ?= config.json

# Load secrets from .env (if present) into the environment of every recipe.
-include .env
export

.PHONY: install build test run run-service run-web sandbox-image clean

install:
	cd web/server && npm ci --no-audit --no-fund
	cd web/client && npm ci --no-audit --no-fund

build:
	cd agent && CGO_ENABLED=0 go build -trimpath -o bin/ ./cmd/...
	cd web/client && npm run build

test:
	cd agent && go vet ./... && go test ./...
	cd web/server && npm test

sandbox-image:
	docker build -t coding-agent-sandbox:latest .

run-service: build
	./agent/bin/agent-service -config $(CONFIG)

run-web:
	cd web/server && node src/index.js ../../$(CONFIG)

# Runs both; Ctrl-C stops both.
run: build
	@trap 'kill 0' INT TERM; \
	./agent/bin/agent-service -config $(CONFIG) & \
	(cd web/server && CONFIG_PATH=../../$(CONFIG) node src/index.js) & \
	wait

clean:
	rm -rf agent/bin web/client/dist
