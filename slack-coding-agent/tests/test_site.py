"""Tests for the site Lambda that serves the HTTPS dashboard (payload v2)."""
import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from aws_stubs import install_stubs

install_stubs()

DIR = Path(__file__).resolve().parent.parent / "infrastructure" / "lambda" / "dashboard_api"

if str(DIR) not in sys.path:
    sys.path.insert(0, str(DIR))

spec = importlib.util.spec_from_file_location("dashboard_site_test", DIR / "dashboard_site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


@pytest.fixture(autouse=True)
def _patch_env(monkeypatch, tmp_path):
    monkeypatch.setattr(site, "SPA_DIR", tmp_path)
    monkeypatch.setattr(site, "REST_API_URL", "https://execute-api.test/prod/")
    (tmp_path / "index.html").write_text("<!doctype html><title>Dashboard</title>")
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "app.js").write_text("console.log('hi');")


def _evt(method, path, body=None, is_b64=False, qs=""):
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": qs,
        "headers": {"content-type": "application/json"},
        "requestContext": {"http": {"method": method, "path": path}},
        "body": body,
    }


def test_health():
    resp = site.handler(_evt("GET", "/health"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "ok"


def test_serves_index_at_root():
    resp = site.handler(_evt("GET", "/"), None)
    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"].startswith("text/html")
    assert "Dashboard" in resp["body"]


def test_serves_static_asset_and_blocks_traversal(tmp_path):
    resp = site.handler(_evt("GET", "/static/app.js"), None)
    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"].startswith("application/javascript")
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nope")
    resp = site.handler(_evt("GET", "/../secret.txt"), None)
    assert resp["statusCode"] == 500 or resp["body"] != "nope"
    resp = site.handler(_evt("GET", "/%2e%2e/secret.txt"), None)
    assert "nope" not in getattr(resp, "body", "")


def test_spa_fallback_for_deep_links():
    resp = site.handler(_evt("GET", "/some/deep/link"), None)
    assert resp["statusCode"] == 200
    assert "Dashboard" in resp["body"]


def test_task_route_delegates_even_when_missing(monkeypatch):
    captured = {}

    def fake_handler(v1, context):
        captured["v1"] = v1
        return {"statusCode": 404, "body": json.dumps({"error": "not found"})}

    monkeypatch.setattr(site, "dashboard_handler", fake_handler)
    resp = site.handler(_evt("GET", "/tasks/task-missing"), None)
    assert resp["statusCode"] == 404
    assert captured["v1"]["pathParameters"] == {"id": "task-missing"}


def test_binary_asset_is_base64(tmp_path):
    (tmp_path / "static" / "logo.png").write_bytes(b"\x89PNG\x0d\x0a\x1a\x0a")
    resp = site.handler(_evt("GET", "/static/logo.png"), None)
    assert resp["statusCode"] == 200
    assert resp["isBase64Encoded"] is True
    assert base64.b64decode(resp["body"])[:4] == b"\x89PNG"


def test_api_prefixes_delegate_to_dashboard_handler(monkeypatch):
    captured = {}

    def fake_handler(v1, context):
        captured["v1"] = v1
        return {"statusCode": 200, "body": json.dumps({"tasks": []})}

    monkeypatch.setattr(site, "dashboard_handler", fake_handler)
    resp = site.handler(_evt("POST", "/tasks", body=json.dumps({"request": "x"}), is_b64=False), None)
    assert resp["statusCode"] == 200
    v1 = captured["v1"]
    assert v1["httpMethod"] == "POST"
    assert v1["path"] == "/tasks"
    assert v1["pathParameters"] == {}
    assert json.loads(v1["body"]) == {"request": "x"}


def test_v1_event_path_params_for_task_routes(monkeypatch):
    captured = {}

    def fake_handler(v1, context):
        captured["v1"] = v1
        return {"statusCode": 200, "body": "{}"}

    monkeypatch.setattr(site, "dashboard_handler", fake_handler)
    site.handler(_evt("GET", "/tasks/task-abc123", qs="page=2&page=3"), None)
    v1 = captured["v1"]
    assert v1["pathParameters"] == {"id": "task-abc123"}
    assert v1["queryStringParameters"] == {"page": ["2", "3"]}


def test_approve_forwarded_to_rest_api(monkeypatch):
    import urllib.request

    def fake_open(req, timeout):
        class FakeResp:
            status = 202
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"statusCode": 202, "body": json.dumps({"ok": True})}).encode()
        assert req.full_url == "https://execute-api.test/prod/tasks/task-1/approve"
        assert req.method == "POST"
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    resp = site.handler(_evt("POST", "/tasks/task-1/approve", body=json.dumps({"x": 1})), None)
    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["body"]


def test_approve_without_rest_api_url(monkeypatch):
    monkeypatch.setattr(site, "REST_API_URL", "")
    resp = site.handler(_evt("POST", "/tasks/task-1/approve", body="{}"), None)
    assert resp["statusCode"] == 503


def test_missing_spa_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(site, "SPA_DIR", tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    resp = site.handler(_evt("GET", "/"), None)
    assert resp["statusCode"] == 500
    assert "spa/index.html" in resp["body"]