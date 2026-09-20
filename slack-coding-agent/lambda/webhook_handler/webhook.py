"""Webhook Handler Lambda – validates Slack signatures and queues events."""
import hashlib
import hmac
import json
import os
import time
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

sqs = boto3.client("sqs")
secretsmanager = boto3.client("secretsmanager")

QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SECRET_ARN = os.environ.get("SLACK_SIGNING_SECRET_ARN", "")
_signing_secret: str | None = None


def get_signing_secret() -> str:
    global _signing_secret
    if _signing_secret:
        return _signing_secret
    resp = secretsmanager.get_secret_value(SecretId=SECRET_ARN)
    secret = json.loads(resp["SecretString"])
    _signing_secret = secret.get("signing_secret", "")
    return _signing_secret


def verify_slack_signature(headers: dict, body: str) -> bool:
    signing_secret = get_signing_secret()
    timestamp = headers.get("X-Slack-Request-Timestamp", "")
    signature = headers.get("X-Slack-Signature", "")
    if abs(time.time() - int(timestamp)) > 300:
        return False
    sig_basestring = f"v0:{timestamp}:{body}"
    computed = "v0=" + hmac.new(
        signing_secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def handler(event, context):
    logger.info("Webhook received")
    headers = event.get("headers", {})
    body = event.get("body", "")

    if not verify_slack_signature(headers, body or ""):
        return {"statusCode": 401, "body": json.dumps({"error": "Invalid signature"})}

    payload = json.loads(body) if body else {}

    # Handle Slack URL verification challenge
    if payload.get("type") == "url_verification":
        return {"statusCode": 200, "body": json.dumps({"challenge": payload["challenge"]})}

    # Queue the event for async processing
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(payload))
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
