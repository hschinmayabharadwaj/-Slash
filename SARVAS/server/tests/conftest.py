import os

import pytest

# `make test` exports the developer's .env; tests must not see real logins or keys.
_PREFIXES = ("SARVAS_", "GITHUB_", "AI_", "MODAL_", "SANDBOX_")
_NAMES = {"AGENT_SERVICE_TOKEN", "WEB_ENCRYPTION_KEY", "BOT_DISABLED", "CONFIG_PATH"}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(_PREFIXES) or name in _NAMES:
            monkeypatch.delenv(name)
