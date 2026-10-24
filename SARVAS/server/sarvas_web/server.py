"""Wire config, storage and the agent client into the app, and serve it."""
from __future__ import annotations

import json
import os
import sys

import uvicorn

from .agent_client import AgentClient
from .app import create_app
from .config import load_config
from .crypto import load_or_create_key
from .store import Store


def build(config_path: str):
    config = load_config(config_path)
    web = config["web"]
    store = Store(web["dataDir"], load_or_create_key(web["dataDir"]), web["sessionHours"])
    agent = AgentClient(config["agentService"]["url"], config["agentService"]["token"])
    return config, create_app(config, store, agent)


def main(argv: list[str]) -> None:
    config_path = argv[1] if len(argv) > 1 else os.environ.get("CONFIG_PATH", "../config.json")
    try:
        config, app = build(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    web = config["web"]
    if web["host"] not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠️  web.host={web['host']} exposes the page beyond this machine; it must sit behind HTTPS.", flush=True)
    print(f"🤖 SARVAS running at {web['baseUrl']}", flush=True)
    # No access log: the OAuth callback URL carries the one-time code.
    uvicorn.run(app, host=web["host"], port=web["port"], access_log=False,
                proxy_headers=False, server_header=False)
