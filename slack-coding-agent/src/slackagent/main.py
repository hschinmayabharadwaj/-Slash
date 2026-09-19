"""Main entry point for Slack bot."""

import logging
import sys

from .config import load_config
from .database import Database
from .slack_handler import SlackHandler


def setup_logging(config_logging: "LoggingConfig") -> None:
    """Set up logging configuration.

    Args:
        config_logging: Logging configuration
    """
    log_config = {
        "level": getattr(logging, config_logging.level),
        "format": config_logging.format,
    }

    if config_logging.file:
        log_config["filename"] = config_logging.file

    logging.basicConfig(**log_config)


def main() -> None:
    """Main entry point."""
    # Load configuration
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"

    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"❌ Failed to load configuration: {e}", file=sys.stderr)
        sys.exit(1)

    # Set up logging
    setup_logging(config.logging)
    logger = logging.getLogger(__name__)

    logger.info("Starting Slack Coding Agent")

    # Initialize database
    try:
        database = Database(config.database.path)
        logger.info(f"Database initialized at {config.database.path}")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        sys.exit(1)

    # Initialize Slack handler
    try:
        slack_handler = SlackHandler(config, database)
        logger.info("Slack handler initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Slack handler: {e}")
        sys.exit(1)

    # Start the bot
    try:
        logger.info("Starting Slack bot...")
        slack_handler.start()
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
    except Exception as e:
        logger.error(f"Bot error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
