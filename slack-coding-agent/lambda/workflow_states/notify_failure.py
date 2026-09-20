"""notify_failure state handler for the TaskWorkflow state machine."""
import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def handler(event, context):
    logger.info("notify_failure invoked: %s", json.dumps(event))
    return {"state": "notify_failure", "status": "OK", "input": event}
