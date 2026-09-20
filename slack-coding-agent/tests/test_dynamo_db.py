"""Tests for the DynamoDB task store (in-memory fake table, no AWS access)."""

import re

import pytest

from aws_stubs import install_stubs
from slackagent.database import TaskStatus
from slackagent.dynamo_db import DynamoDBStore

install_stubs()

from botocore.exceptions import ClientError


class ConditionalCheckFailedException(ClientError):
    def __init__(self, message="conditional request failed"):
        super().__init__({"Error": {"Code": "ConditionalCheckFailedException", "Message": message}})


class FakeTable:
    """Tiny in-memory DynamoDB table supporting the expressions this app uses."""

    def __init__(self, name="test-table"):
        self.name = name
        self.items = {}
        self.counter = 0

    # -- attribute helpers ------------------------------------------------ #

    @staticmethod
    def _store_key(key: dict) -> str:
        if "taskId" in key:
            return f"{key['taskId']}#{key.get('timestamp', 'now')}"
        return str(key.get("pk"))

    def _item_key(self, item: dict) -> str:
        if "taskId" in item:
            return f"{item['taskId']}#{item.get('timestamp', 'now')}"
        return str(item.get("pk"))

    def _get(self, key: dict) -> dict | None:
        return self.items.get(self._store_key(key))

    def _resolve(self, expr: str, values: dict, names: dict) -> str:
        if names:
            for k, v in names.items():
                expr = expr.replace(k, v)
        if values:
            for k, v in values.items():
                expr = re.sub(rf"(?<!:){k}", repr(v), expr)
        return expr

    def _attr_exists(self, item, attr: str, value=None) -> bool:
        val = item.get(attr) if item else None
        if value is None:
            return val is not None
        return val == value

    def _eval_condition(self, item: dict | None, condition: str, values: dict, names: dict) -> bool:
        if not condition:
            return True
        resolved = self._resolve(condition, values, names)
        predicates = [p.strip() for p in resolved.split("AND") if p.strip()]
        for pred in predicates:
            match = re.fullmatch(r"attribute_exists\((\w+)\)", pred)
            if match and not (item and match.group(1) in item and item[match.group(1)] is not None):
                return False
            match = re.fullmatch(r"attribute_not_exists\((\w+)\)", pred)
            if match and (item and match.group(1) in item and item[match.group(1)] is not None):
                return False
            match = re.fullmatch(r"(\w+) = (.*)", pred)
            if match and not (item and item.get(match.group(1)) == _lit(match.group(2))):
                return False
            match = re.fullmatch(r"(\w+) <> (.*)", pred)
            if match and not (item and item.get(match.group(1)) != _lit(match.group(2))):
                return False
            match = re.fullmatch(r"(\w+) IN \((.*)\)", pred)
            if match:
                options = [_lit(v) for v in match.group(2).split(",")]
                if not (item and item.get(match.group(1)) in options):
                    return False
        return True

    # -- API -------------------------------------------------------------- #

    def put_item(self, *, Item, ConditionExpression=None, **kwargs):
        self.counter += 1
        existing = self._get(Item)
        if not self._eval_condition(existing, ConditionExpression, {}, {}):
            raise ConditionalCheckFailedException("conditional request failed")
        self.items[self._item_key(Item)] = dict(Item)

    def get_item(self, *, Key, **kwargs):
        item = self._get(Key)
        return {"Item": dict(item) if item else None}

    def update_item(self, *, Key, UpdateExpression, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ReturnValues=None, **kwargs):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        item = self._get(Key)
        if not self._eval_condition(item, kwargs.get("ConditionExpression"), values, names):
            raise ConditionalCheckFailedException("conditional request failed")
        if item is None:
            item = {}

        set_re = re.findall(r"SET (.+)", UpdateExpression)
        if set_re:
            assignments = _split_top_level(self._resolve(set_re[0], values, names))
            for a in assignments:
                key, _, value = a.partition("=")
                item[key.strip()] = _lit(value.strip())
        add_re = re.findall(r"ADD (\w+) (\S+)", UpdateExpression)
        for attr, op in add_re:
            item[attr] = item.get(attr, 0) + int(values[op])
        if "REMOVE worker_id" in UpdateExpression:
            item.pop("worker_id", None)
        if "worker_id" in item and item["worker_id"] is None:
            item.pop("worker_id", None)

        self.items[self._store_key(Key)] = item
        if ReturnValues in ("UPDATED_NEW", "ALL_NEW"):
            return {"Attributes": dict(item)}
        if ReturnValues == "ALL_OLD":
            return {"Attributes": dict(item)}

    def query(self, *, IndexName=None, KeyConditionExpression=None,
              ExpressionAttributeValues=None, ExpressionAttributeNames=None,
              Limit=None, ScanIndexForward=True, **kwargs):
        values = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}
        resolved = self._resolve(KeyConditionExpression, values, names)
        attr, _, value = resolved.partition("=")
        attr, value = attr.strip(), _lit(value.strip())
        matches = [it for it in self.items.values() if it.get(attr) == value]
        if not ScanIndexForward:
            matches = list(reversed(matches))
        matches = list(matches)
        if Limit:
            matches = matches[:Limit]
        return {"Items": [dict(it) for it in matches]}

    def scan(self, *, FilterExpression=None, ExpressionAttributeValues=None,
             ExpressionAttributeNames=None, Limit=None, Select=None, **kwargs):
        values = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}
        resolvable = []
        if FilterExpression:
            resolved = self._resolve(FilterExpression, values, names)
            resolvable = [p.strip() for p in resolved.split("AND") if p.strip()]
        matches = []
        for item in self.items.values():
            ok = True
            for pred in resolvable:
                m = re.fullmatch(r"(\w+) = (.*)", pred)
                if not m or item.get(m.group(1)) != _lit(m.group(2)):
                    ok = False
                    break
            if ok:
                matches.append(item)
        if Limit:
            matches = matches[:Limit]
        return {"Items": [dict(it) for it in matches]}


def _split_top_level(expr: str):
    """Split on commas that are not inside single/double-quoted strings."""
    parts, cur, quote = [], [], None
    for ch in expr:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == ",":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p for p in parts if p.strip()]


def _lit(value: str):
    """Parse a Python literal-like repr produced by the fake resolve()."""
    if value in ("null", "None"):
        return None
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value in ("True", "False"):
        return value == "True"
    try:
        return int(value)
    except ValueError:
        return value


@pytest.fixture
def store():
    return DynamoDBStore(
        table_name="slackagent-tasks",
        audit_table_name="slackagent-audit-events",
        table=FakeTable("slackagent-tasks"),
        audit_table=FakeTable("slackagent-audit-events"),
    )


def make_task(store, description="Add tests"):
    return store.create_task(
        slack_user_id="U1", slack_channel_id="C1", slack_thread_ts="1.1",
        repo_owner="org", repo_name="repo", branch_name="feat", task_description=description,
    )


def test_create_and_get_task(store):
    task = make_task(store)
    assert task.id is not None
    fetched = store.get_task(task.id)
    assert fetched is not None
    assert fetched.status == TaskStatus.PENDING
    assert fetched.repo_name == "repo"


def test_create_returns_missing_task_none(store):
    assert store.get_task(99999) is None


def test_create_task_idempotent_on_client_token(store):
    first = store.create_task(
        slack_user_id="U1", slack_channel_id="C1", slack_thread_ts="1.1",
        repo_owner="org", repo_name="repo", branch_name="feat",
        task_description="d", client_token="tok-abc",
    )
    second = store.create_task(
        slack_user_id="U1", slack_channel_id="C1", slack_thread_ts="1.1",
        repo_owner="org", repo_name="repo", branch_name="feat",
        task_description="d", client_token="tok-abc",
    )
    assert first.id == second.id


def test_update_status_plan_impl_pr(store):
    task = make_task(store)
    store.update_task_status(task.id, TaskStatus.PLANNING)
    assert store.get_task(task.id).status == TaskStatus.PLANNING

    store.update_task_plan(task.id, "the plan")
    assert store.get_task(task.id).plan == "the plan"

    store.update_task_implementation(task.id, "done", "diff")
    got = store.get_task(task.id)
    assert got.implementation_summary == "done"
    assert got.diff == "diff"

    store.update_task_pr(task.id, "https://github.com/org/repo/pull/1")
    assert store.get_task(task.id).pr_url.endswith("/pull/1")


def test_claim_returns_false_when_already_claimed(store):
    task = make_task(store)
    assert store.claim_task(task.id, "worker-a") is True
    assert store.claim_task(task.id, "worker-b") is False
    store.release_task(task.id)
    assert store.claim_task(task.id, "worker-b") is True


def test_get_pending_tasks_only_returns_runnable_statuses(store):
    t1 = make_task(store, "a")
    t2 = make_task(store, "b")
    store.update_task_status(t1.id, TaskStatus.PLANNING)
    pending = store.get_pending_tasks()
    ids = {t.id for t in pending}
    assert t1.id not in ids
    assert t2.id in ids


def test_audit_events_roundtrip(store):
    task = make_task(store)
    store.record_event(task.id, "kill_switch", {"reason": "leak"}, actor="admin")
    events = store.list_events(task.id)
    assert len(events) == 1
    assert events[0]["event_type"] == "kill_switch"
    assert events[0]["actor"] == "admin"
    assert events[0]["detail"] == {"reason": "leak"}