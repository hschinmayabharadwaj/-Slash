"""create_pr state handler for the TaskWorkflow state machine."""
import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def handler(event, context):
    logger.info("create_pr invoked: %s", json.dumps(event))
    return {"state": "create_pr", "status": "OK", "input": event}
