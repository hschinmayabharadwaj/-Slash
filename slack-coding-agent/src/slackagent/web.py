"""Start the local web page (primary interface).

    python -m slackagent.web [config.yaml]       # or: coding-agent
"""

import ipaddress
import logging
import sys

from .config import load_config
from .webapp import create_app


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    try:
        config = load_config(config_path, require=("web",))
    except Exception as e:
        sys.exit(f"❌ Failed to load configuration: {e}")

    logging.basicConfig(level=config.logging.level.upper(), format=config.logging.format)
    logger = logging.getLogger("slackagent.web")
    try:
        loopback = ipaddress.ip_address(config.web.host).is_loopback
    except ValueError:
        loopback = config.web.host == "localhost"
    if not loopback:
        logger.warning("web.host=%s exposes the page beyond this machine; put it behind HTTPS.", config.web.host)

    app = create_app(config)
    print(f"🤖 Coding Agent running at {config.web.base_url}  (AI: {config.ai.provider})")
    try:
        from waitress import serve

        serve(app, host=config.web.host, port=config.web.port, threads=4)
    except ImportError:
        app.run(host=config.web.host, port=config.web.port)


if __name__ == "__main__":
    main()
