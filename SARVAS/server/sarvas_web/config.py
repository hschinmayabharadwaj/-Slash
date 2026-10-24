"""Loads the shared config.json (also read by the Go agent). String values may
reference environment variables as ${NAME}."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

DEFAULTS: dict = {
    "web": {
        "host": "127.0.0.1",
        "port": 8765,
        "baseUrl": "http://localhost:8765",
        "sessionHours": 8,
        "dataDir": "./data",
        "clientDist": "./client/dist",  # "" = API only (page hosted elsewhere)
        "extraHosts": [],  # more Host headers to accept, e.g. the backend's own address behind a proxy
    },
    "github": {"clientId": "", "clientSecret": ""},
    "agentService": {"url": "http://127.0.0.1:8766", "token": ""},
    "security": {
        "allowedUsers": [],
        "allowedRepos": [],
        "maxChangesPerHour": 10,
        "maxDraftsPerHour": 20,
        "killSwitchFile": "./data/KILL_SWITCH",
    },
    "repos": {},
}

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def _expand(value):
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _merge(base: dict, extra: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _as_list(value) -> list[str]:
    """Lists may also be given as a comma-separated string (handy for env vars)."""
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return [str(s) for s in value or []]


def load_config(config_path: str | os.PathLike = "config.json") -> dict:
    path = Path(config_path)
    raw = json.loads(path.read_text("utf-8")) if path.exists() else {}
    config = _merge(DEFAULTS, _expand(raw))
    gh, sec = config["github"], config["security"]
    gh["clientId"] = gh.get("clientId") or os.environ.get("GITHUB_CLIENT_ID", "")
    gh["clientSecret"] = gh.get("clientSecret") or os.environ.get("GITHUB_CLIENT_SECRET", "")
    config["agentService"]["token"] = config["agentService"].get("token") or os.environ.get("AGENT_SERVICE_TOKEN", "")
    config["web"]["port"] = int(config["web"]["port"] or DEFAULTS["web"]["port"])
    config["web"]["extraHosts"] = _as_list(config["web"].get("extraHosts"))
    sec["allowedUsers"] = _as_list(sec.get("allowedUsers"))
    sec["allowedRepos"] = _as_list(sec.get("allowedRepos"))
    # Relative paths are relative to the config file, not the working directory.
    base = path.resolve().parent
    for section, key in (("web", "dataDir"), ("web", "clientDist"), ("security", "killSwitchFile")):
        if config[section][key]:
            config[section][key] = str((base / config[section][key]).resolve())
    validate(config)
    return config


def is_local(config: dict) -> bool:
    return urlparse(config["web"]["baseUrl"]).hostname in LOCAL_HOSTS


def validate(config: dict) -> None:
    if not config["github"]["clientId"] or not config["github"]["clientSecret"]:
        raise ValueError("github.clientId and github.clientSecret (your GitHub App OAuth credentials) are required")
    base = urlparse(config["web"]["baseUrl"])
    if not base.hostname or not (base.scheme == "https" or (base.scheme == "http" and is_local(config))):
        raise ValueError("web.baseUrl must be https://, or http://localhost / http://127.0.0.1")
    if len(config["agentService"].get("token") or "") < 32:
        raise ValueError("agentService.token (AGENT_SERVICE_TOKEN) must be a random value of at least 32 characters")
    # A public page with nobody listed would let any GitHub user spend your AI key.
    if not is_local(config) and not config["security"]["allowedUsers"]:
        raise ValueError("security.allowedUsers (SARVAS_ALLOWED_USERS) must list the GitHub logins "
                         "allowed to sign in when the page is reachable beyond this machine")


def repo_allowed(config: dict, full_name: str) -> bool:
    repo = (config.get("repos") or {}).get(full_name)
    if repo and repo.get("enabled") is False:
        return False
    allowed = config["security"]["allowedRepos"]
    return not allowed or any(r.lower() == full_name.lower() for r in allowed)


def user_allowed(config: dict, login: str) -> bool:
    allowed = config["security"]["allowedUsers"]
    return not allowed or any(u.lower() == login.lower() for u in allowed)
