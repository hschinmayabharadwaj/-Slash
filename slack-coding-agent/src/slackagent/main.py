"""Entry point: Slack bot + worker(s) in one local process.

    python -m slackagent.main [config.yaml]
"""

import logging
import signal
import sys
import threading

from .config import LoggingConfig, load_config
from .database import Database
from .sandbox import DockerSandbox
from .slack_handler import SlackHandler
from .worker import Worker


def setup_logging(config_logging: LoggingConfig) -> None:
    """Configure root logging."""
    log_config = {
        "level": getattr(logging, config_logging.level.upper(), logging.INFO),
        "format": config_logging.format,
    }
    if config_logging.file:
        log_config["filename"] = config_logging.file
    logging.basicConfig(**log_config)


def main() -> None:
    """Load config, run preflight checks, start workers and the Slack bot."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"❌ Failed to load configuration: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.logging)
    logger = logging.getLogger("slackagent")

    sandbox = DockerSandbox(
        config.docker,
        network_mode=config.security.network_mode,
        agent_network=config.security.agent_network,
        ai_provider=config.ai.provider,
    )
    problems = sandbox.preflight([config.docker.image])
    if problems:
        for problem in problems:
            logger.error("Preflight: %s", problem)
        sys.exit(1)

    database = Database(config.database.path)
    interrupted = database.recover_interrupted()
    if interrupted:
        logger.warning("Marked interrupted tasks as failed: %s", interrupted)

    slack_handler = SlackHandler(config, database)

    stop = threading.Event()
    workers = []
    for i in range(max(1, config.worker.num_workers)):
        worker = Worker(config, database, slack_handler, worker_id=f"w{i}", sandbox=sandbox)
        thread = threading.Thread(target=worker.run_forever, args=(stop,), name=f"worker-{i}", daemon=True)
        thread.start()
        workers.append(thread)

    def shutdown(*_):
        logger.info("Shutting down...")
        stop.set()
        for thread in workers:
            thread.join()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    logger.info("Slack Coding Agent running (ai=%s, workers=%d)", config.ai.provider, len(workers))
    try:
        slack_handler.start()
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
