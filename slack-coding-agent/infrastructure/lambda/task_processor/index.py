"""Task Processor Lambda - validates, routes, and acts on task approvals.

Sources:
  - API Gateway POST /tasks/{id}/approve  -> action: approve
  - API Gateway POST /tasks/{id}/reject   -> action: reject
  - API Gateway POST /tasks/{id}/cancel   -> action: cancel
  - EventBridge (StaleApprovalSweeper)    -> action: expire_stale
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")

TABLE_NAME = os.environ.get("SLACKAGENT_TASKS_TABLE", os.environ.get("DYNAMODB_TABLE", "TaskState"))
AUDIT_TABLE = os.environ.get("SLACKAGENT_AUDIT_TABLE", os.environ.get("AUDIT_TABLE", "slackagent-audit-events"))
QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _table():
    return dynamodb.Table(TABLE_NAME)


def _audit():
    return dynamodb.Table(AUDIT_TABLE)


def _write_audit(task_id, action, detail, status):
    try:
        _audit().put_item(Item={
            "pk": f"audit#{uuid.uuid4().hex}",
            "ts": _now(),
            "taskId": task_id or "",
            "action": action,
            "detail": detail,
            "status": status or "",
        })
    except Exception as exc:
        logger.warning("audit write failed: %s", exc)


def _json(status_code, body):
    return {"statusCode": status_code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, default=str)}


def _route_to_worker(task_id, message):
    """Wake the worker for this task by enqueueing a control message."""
    if not QUEUE_URL:
        return
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps({
        "taskId": task_id,
        "control": message,
    }))


def approve(task_id, note):
    key = {"taskId": task_id, "timestamp": "now"}
    current = _table().get_item(Key=key).get("Item")
    if current and current.get("status") == "AWAITING_APPROVAL":
        target = "PLAN_APPROVED"
    elif current and current.get("status") == "AWAITING_IMPL_APPROVAL":
        target = "IMPL_APPROVED"
    else:
        return _json(409, {"error": "Task is not awaiting approval"})
    try:
        resp = _table().update_item(
            Key=key,
            UpdateExpression="SET #s = :target, approvedAt = :ts, approvalNote = :note, updatedAt = :ts",
            ConditionExpression="#s = :cur",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":ts": _now(),
                ":note": note or "",
                ":target": target,
                ":cur": current["status"],
            },
            ReturnValues="ALL_NEW",
        )
    except Exception as exc:
        if _conditional_failure(exc):
            return _json(409, {"error": "Task is not awaiting approval"})
        raise
    item = resp.get("Attributes", {})
    _write_audit(task_id, "approved", {"note": note or ""}, item.get("status"))
    _route_to_worker(task_id, "approved")
    return _json(200, {"taskId": task_id, "reason": "approved", "status": item.get("status")})


def reject(task_id, reason):
    key = {"taskId": task_id, "timestamp": "now"}
    try:
        resp = _table().update_item(
            Key=key,
            UpdateExpression="SET #s = :rejected, rejectedAt = :ts, rejectionReason = :reason, updatedAt = :ts",
            ConditionExpression="attribute_exists(taskId) AND #s IN (:plan, :impl)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":ts": _now(),
                ":reason": reason or "No reason given",
                ":rejected": "REJECTED",
                ":plan": "AWAITING_APPROVAL",
                ":impl": "AWAITING_IMPL_APPROVAL",
            },
            ReturnValues="ALL_NEW",
        )
    except Exception as exc:
        if _conditional_failure(exc):
            return _json(409, {"error": "Task is not awaiting approval"})
        raise
    item = resp.get("Attributes", {})
    _write_audit(task_id, "rejected", {"reason": reason or "No reason given"}, item.get("status"))
    return _json(200, {"taskId": task_id, "reason": "rejected", "status": item.get("status")})


def cancel(task_id):
    key = {"taskId": task_id, "timestamp": "now"}
    try:
        resp = _table().update_item(
            Key=key,
            UpdateExpression="SET #s = :cancelled, cancelledAt = :ts, updatedAt = :ts",
            ConditionExpression="attribute_exists(taskId) AND #s <> :terminal",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":ts": _now(),
                ":cancelled": "CANCELLED",
                ":terminal": "COMPLETED",
            },
            ReturnValues="ALL_NEW",
        )
    except Exception as exc:
        if _conditional_failure(exc):
            return _json(409, {"error": "Task is already terminal"})
        raise
    item = resp.get("Attributes", {})
    _write_audit(task_id, "cancelled", {}, item.get("status"))
    _route_to_worker(task_id, "cancelled")
    return _json(200, {"taskId": task_id, "reason": "cancelled", "status": item.get("status")})


def expire_stale():
    """Mark tasks stuck awaiting approval for > 24h as EXPIRED."""
    table = _table()
    expired = 0
    for item in table.scan().get("Items", []):
        if item.get("status") not in ("AWAITING_APPROVAL", "AWAITING_IMPL_APPROVAL"):
            continue
        updated = item.get("updatedAt") or item.get("createdAt") or ""
        if not updated:
            continue
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds()
        except ValueError:
            continue
        if age > 24 * 3600:
            table.update_item(
                Key={"taskId": item["taskId"], "timestamp": "now"},
                UpdateExpression="SET #s = :expired, expiredAt = :ts",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":expired": "EXPIRED", ":ts": _now()},
            )
            _write_audit(item["taskId"], "expired", {"reason": "approval wait > 24h"}, "EXPIRED")
            _route_to_worker(item["taskId"], "expired")
            expired += 1
    return {"expired": expired}


def handle_api_action(event):
    path = (event.get("path") or "").rstrip("/")
    path_params = event.get("pathParameters") or {}
    task_id = path_params.get("id")
    values = event.get("body") and json.loads(event["body"]) or {}
    if not task_id:
        return _json(400, {"error": "task id is required"})

    if path.endswith("/approve"):
        return approve(task_id, values.get("note") or values.get("comment"))
    if path.endswith("/reject"):
        return reject(task_id, values.get("reason") or values.get("comment"))
    if path.endswith("/cancel"):
        return cancel(task_id)
    return _json(404, {"error": "unknown action"})


def _conditional_failure(exc):
    import botocore.exceptions
    if isinstance(exc, botocore.exceptions.ClientError):
        return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    return False


def handler(event, context):
    logger.info("TaskProcessor invoked: %s", json.dumps(event)[:1000])
    body = event.get("body")
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            if parsed.get("action") == "expire_stale":
                return expire_stale()
        except json.JSONDecodeError:
            pass
    if event.get("action") == "expire_stale":
        return expire_stale()
    return handle_api_action(event)