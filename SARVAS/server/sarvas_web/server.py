"""Wire config, storage and the agent client into the app, and serve it."""
from __future__ import annotations

import json
import logging
import os
import re
import sys

import uvicorn

from .agent_client import AgentClient
from .app import create_app
from .config import load_config
from .crypto import load_or_create_key
from .store import Store


class RedactSecrets(logging.Filter):
    """Last line of defence: scrub token-shaped strings from every log record."""
    PATTERNS = re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,}|AIza[0-9A-Za-z_-]{20,})"
                          r"|((?:Bearer|token|basic)\s+)[A-Za-z0-9._~+/=-]{8,}", re.I)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        clean = self.PATTERNS.sub(lambda m: (m.group(2) or "") + "[redacted]", message)
        if clean != message:
            record.msg, record.args = clean, ()
        return True


def install_log_redaction() -> None:
    redact = RedactSecrets()
    for name in ("", "sarvas", "uvicorn", "uvicorn.error", "httpx"):
        logging.getLogger(name).addFilter(redact)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)


def build(config_path: str):
    config = load_config(config_path)
    web = config["web"]
    store = Store(web["dataDir"], load_or_create_key(web["dataDir"]), web["sessionHours"])
    agent = AgentClient(config["agentService"]["url"], config["agentService"]["token"])
    return config, create_app(config, store, agent)


def main(argv: list[str]) -> None:
    config_path = argv[1] if len(argv) > 1 else os.environ.get("CONFIG_PATH", "../config.json")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs can carry OAuth codes
    install_log_redaction()
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
