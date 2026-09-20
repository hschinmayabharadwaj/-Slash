"""Dashboard API Lambda - console backend.

Routes (API Gateway proxy):
  GET    /tasks              list tasks (+ summary)
  POST   /tasks              create a task             {request, repo, baseBranch?}
  GET    /tasks/{id}         task detail
  POST   /tasks/{id}/cancel  cancel a waiting task
  GET    /stats              status counts + totals
  GET    /metrics            time-series-able metrics snapshot
  GET    /audit              recent audit events
  GET    /admin/kill         current kill-switch value
  POST   /admin/kill         set kill switch          {kill: true|false}
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
ssm = boto3.client("ssm")

TABLE_NAME = os.environ.get("SLACKAGENT_TASKS_TABLE", os.environ.get("DYNAMODB_TABLE", "TaskState"))
AUDIT_TABLE = os.environ.get("SLACKAGENT_AUDIT_TABLE", os.environ.get("AUDIT_TABLE", "slackagent-audit-events"))
QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
KILL_SWITCH_PARAM = os.environ.get("KILL_SWITCH_PARAM", "/slack-agent/kill-switch")

CANCELLABLE = {"READY", "RUNNING", "AWAITING_APPROVAL", "AWAITING_IMPL_APPROVAL"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _table():
    return dynamodb.Table(TABLE_NAME)


def _audit_table():
    return dynamodb.Table(AUDIT_TABLE)


def _item_to_public(item):
    return item


def _write_audit(task_id, action, detail, task=None):
    event = {
        "pk": f"audit#{uuid.uuid4().hex}",
        "ts": _now(),
        "taskId": task_id or "",
        "action": action,
        "detail": detail,
    }
    try:
        task = task or {}
        event["status"] = task.get("status")
        _audit_table().put_item(Item=event)
    except Exception as exc:  # audit must never break the primary path
        logger.warning("failed to write audit event: %s", exc)


def _json(status_code, body):
    return {"statusCode": status_code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, default=str)}


def list_tasks():
    resp = _table().scan(Limit=100)
    items = resp.get("Items", [])
    items.sort(key=lambda i: str(i.get("createdAt", "")), reverse=True)
    return _json(200, {"tasks": items, "count": len(items)})


def get_task(task_id):
    resp = _table().get_item(Key={"taskId": task_id, "timestamp": "now"})
    item = resp.get("Item")
    if not item:
        return _json(404, {"error": "Task not found"})
    return _json(200, item)


def create_task(body):
    if not body.get("request") or not str(body.get("request", "")).strip():
        return _json(400, {"error": "request is required"})
    request = str(body["request"]).strip()
    repo = str(body.get("repo", "")).strip()
    if not repo:
        return _json(400, {"error": "repo (owner/name) is required"})
    base_branch = str(body.get("baseBranch", "").strip() or "main")
    branch = str(body.get("branch", "").strip() or None)
    client_token = str(body.get("client_token", "").strip() or None)

    if client_token:
        existing = _table().query(
            IndexName="ClientTokenIndex",
            KeyConditionExpression="client_token = :ct",
            ExpressionAttributeValues={":ct": client_token},
            Limit=1,
        ).get("Items")
        if existing:
            task = _table().get_item(
                Key={"taskId": existing[0]["taskId"], "timestamp": "now"}
            ).get("Item", {})
            return _json(200, {"taskId": task.get("taskId"), "status": task.get("status"), "duplicate": True})

    task_id = f"task-{uuid.uuid4().hex[:12]}"
    now = _now()
    item = {
        "taskId": task_id,
        "timestamp": "now",
        "status": "READY",
        "request": request,
        "repo": repo,
        "baseBranch": base_branch,
        "createdAt": now,
        "source": "dashboard",
    }
    if branch:
        item["branch"] = branch
    if client_token:
        item["client_token"] = client_token
    _table().put_item(Item=item)
    _write_audit(task_id, "task_created", {"request": request[:200], "repo": repo}, item)

    if QUEUE_URL:
        sqs = boto3.client("sqs")
        sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps({
            "taskId": task_id,
            "request": request,
            "repo": repo,
            "baseBranch": base_branch,
            "branch": branch,
            "client_token": client_token,
        }))
    return _json(202, {"taskId": task_id, "status": "READY"})


def cancel_task(task_id):
    key = {"taskId": task_id, "timestamp": "now"}
    try:
        resp = _table().update_item(
            Key=key,
            UpdateExpression="SET #s = :cancelled, cancelledAt = :ts",
            ConditionExpression="attribute_exists(taskId) AND #s IN (:ready, :running, :awaiting, :awaiting_impl)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":cancelled": "CANCELLED",
                ":ts": _now(),
                ":ready": "READY",
                ":running": "RUNNING",
                ":awaiting": "AWAITING_APPROVAL",
                ":awaiting_impl": "AWAITING_IMPL_APPROVAL",
            },
            ReturnValues="ALL_NEW",
        )
    except Exception as exc:
        if boto3_error(exc):
            raise ConditionalFailure() from None
        raise
    item = resp.get("Attributes", {})
    _write_audit(task_id, "task_cancelled", {}, item)
    return _json(200, {"taskId": task_id, "status": item.get("status")})


def get_stats():
    items = _table().scan().get("Items", [])
    counts = {}
    for item in items:
        counts[item.get("status", "UNKNOWN")] = counts.get(item.get("status", "UNKNOWN"), 0) + 1
    return _json(200, {"counts": counts, "total": len(items)})


def get_metrics():
    items = _table().scan().get("Items", [])
    by_status = {}
    for item in items:
        status = item.get("status", "UNKNOWN")
        by_status.setdefault(status, []).append(item)
    summary = {
        "updatedAt": _now(),
        "counts": {k: len(v) for k, v in by_status.items()},
        "total": len(items),
        "inFlight": len(by_status.get("RUNNING", [])) + len(by_status.get("AWAITING_APPROVAL", [])) + len(by_status.get("AWAITING_IMPL_APPROVAL", [])),
    }
    return _json(200, summary)


def get_audit():
    resp = _audit_table().scan(Limit=100)
    events = resp.get("Items", [])
    events.sort(key=lambda e: str(e.get("ts", "")), reverse=True)
    return _json(200, {"events": events, "count": len(events)})


def get_kill():
    try:
        value = ssm.get_parameter(Name=KILL_SWITCH_PARAM, WithDecryption=False)["Parameter"]["Value"]
    except Exception:
        value = "0"
    return _json(200, {"killSwitch": value == "1", "value": value})


def set_kill(body):
    kill = bool(body.get("kill"))
    value = "1" if kill else "0"
    ssm.put_parameter(Name=KILL_SWITCH_PARAM, Value=value, Type="String", Overwrite=True)
    _write_audit("", f"kill_switch_{'on' if kill else 'off'}", {"value": value})
    return _json(200, {"killSwitch": kill, "value": value})


class ConditionalFailure(Exception):
    """Raised internally when a DynamoDB conditional check fails."""


def boto3_error(exc):
    """Whether an exception is a real DynamoDB conditional-check failure."""
    import botocore.exceptions
    if isinstance(exc, botocore.exceptions.ClientError):
        return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    return False


def handler(event, context):
    logger.info("Dashboard API invoked: %s", json.dumps(event)[:1000])
    method = (event.get("httpMethod") or "GET").upper()
    path = (event.get("path") or "/tasks").rstrip("/")
    path_params = event.get("pathParameters") or {}
    task_id = path_params.get("id")

    try:
        if method == "GET":
            if task_id:
                return get_task(task_id)
            if path == "/tasks":
                return list_tasks()
            if path == "/stats":
                return get_stats()
            if path == "/metrics":
                return get_metrics()
            if path == "/audit":
                return get_audit()
            if path == "/admin/kill":
                return get_kill()
            return _json(404, {"error": f"unknown resource {path}"})

        if method == "POST":
            raw = event.get("body") or "{}"
            try:
                body = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                body = {}
            if task_id and path.endswith("/cancel"):
                return cancel_task(task_id)
            if path == "/tasks":
                return create_task(body)
            if path == "/admin/kill":
                return set_kill(body)
            return _json(404, {"error": f"unknown resource {path}"})

        return _json(405, {"error": "method not allowed"})
    except ConditionalFailure:
        return _json(409, {"error": "Task is not cancellable (already terminal)"})
    except Exception as exc:
        logger.exception("dashboard error")
        return _json(500, {"error": "internal error"})