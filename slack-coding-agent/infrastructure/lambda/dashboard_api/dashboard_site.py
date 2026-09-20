"""Site Lambda for the HTTPS dashboard (API Gateway HTTP API, payload v2).

One Lambda serves the whole dashboard:

* SPA static assets from ``spa/`` (the built ``dashboard/build`` output) with
  correct content types and a SPA fallback to ``index.html``;
* the dashboard REST endpoints in-process by reusing the payload-v1 router in
  :mod:`dashboard` (``/tasks``, ``/tasks/{id}``, ``/tasks/{id}/cancel``,
  ``/stats``, ``/metrics``, ``/audit``, ``/admin/kill``);
* ``POST /tasks/{id}/approve`` and ``POST /tasks/{id}/reject`` are forwarded to
  the REST API (``REST_API_URL``) so the approval state machine stays the single
  source of truth.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

from dashboard import handler as dashboard_handler  # noqa: E402

SPA_DIR = Path(__file__).resolve().parent / "spa"
REST_API_URL = (os.environ.get("REST_API_URL") or "").rstrip("/")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject",
}

TEXT_TYPES = {
    ".html", ".js", ".css", ".json", ".map", ".txt", ".svg",
}


def _json(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
        "isBase64Encoded": False,
    }


def _serve_file(path: Path) -> dict:
    """Return a static asset (or the SPA fallback) with the right content type."""
    suffix = path.suffix.lower()
    content_type = CONTENT_TYPES.get(suffix, "application/octet-stream")
    body = path.read_bytes()
    if suffix in TEXT_TYPES:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": content_type, "Cache-Control": "public, max-age=300"},
            "body": body.decode("utf-8", errors="replace"),
            "isBase64Encoded": False,
        }
    return {
        "statusCode": 200,
        "headers": {"Content-Type": content_type, "Cache-Control": "public, max-age=300"},
        "body": base64.b64encode(body).decode("ascii"),
        "isBase64Encoded": True,
    }


def _resolve_static(raw_path: str) -> Path | None:
    """Safely resolve a request path to a file under the SPA dir (no traversal)."""
    rel = raw_path.lstrip("/")
    if not rel:
        rel = "index.html"
    candidate = (SPA_DIR / rel).resolve()
    try:
        candidate.relative_to(SPA_DIR.resolve())
    except ValueError:
        return None
    if candidate.is_file():
        if candidate.suffix:
            return candidate
        return None
    return None


def _forward_approval(method: str, task_id: str, action: str, body: dict) -> dict:
    """Forward an approve/reject POST to the REST API (REST_API_URL)."""
    if not REST_API_URL:
        return _json(503, {"error": "REST_API_URL not configured for approvals"})
    url = f"{REST_API_URL}tasks/{task_id}/{action}"
    payload = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"body": raw}
            return _json(resp.status, parsed)
    except urllib.error.HTTPError as e:
        return _json(e.code, {"error": e.read().decode("utf-8", errors="replace")[:500]})
    except Exception as exc:  # pragma: no cover - network fallback
        logger.exception("approval forwarding failed")
        return _json(502, {"error": f"could not reach approval API: {exc}"})


def _to_v1_event(event: dict) -> dict:
    """Translate an HTTP-API payload v2 event into the payload v1 shape."""
    method = (event.get("requestContext") or {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath") or event.get("path") or "/"
    if isinstance(event.get("body"), str) and event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(event["body"]).decode("utf-8", errors="replace")
        except Exception:
            raw_body = ""
    else:
        raw_body = event.get("body") or ""

    segments = [s for s in path.split("/") if s]
    path_params = {}
    if len(segments) >= 2 and segments[0] == "tasks":
        path_params["id"] = segments[1]

    return {
        "httpMethod": method,
        "path": path,
        "pathParameters": path_params,
        "queryStringParameters": _parse_qs(event.get("rawQueryString") or ""),
        "headers": event.get("headers") or {},
        "body": raw_body,
    }


def _parse_qs(raw: str) -> dict:
    from urllib.parse import parse_qs

    parsed = parse_qs(raw)
    return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}


def handler(event, context):
    logger.info("Dashboard site invoked: %s", json.dumps(event)[:1000])
    raw_path = event.get("rawPath") or event.get("path") or "/"
    method = (event.get("requestContext") or {}).get("http", {}).get("method", "GET")
    path = raw_path.split("?")[0].rstrip("/") or "/"

    # Health check used by load balancers / consoles
    if path == "/health":
        return _json(200, {"status": "ok", "service": "slack-agent-dashboard"})

    # Dashboard REST endpoints — delegate to the in-process router
    api_prefixes = ("/tasks", "/stats", "/metrics", "/audit", "/admin")
    if path.startswith(api_prefixes):
        # Approve/reject live in the REST API's task processor lambda
        if method in ("POST",) and path.endswith(("/approve", "/reject")):
            segments = [s for s in path.split("/") if s]  # ['tasks', '<id>', 'approve']
            if len(segments) >= 3 and segments[0] == "tasks":
                action = segments[-1]
                body = event.get("body") or "{}"
                if isinstance(body, str) and event.get("isBase64Encoded"):
                    body = base64.b64decode(body).decode("utf-8", errors="replace")
                try:
                    parsed_body = json.loads(body) if body else {}
                except ValueError:
                    parsed_body = {}
                return _forward_approval(method, segments[1], action, parsed_body)

        v1 = _to_v1_event(event)
        resp = dashboard_handler(v1, context)
        # Keep the payload-v2 field names the SPA already expects
        return resp

    # Static asset with an extension
    resolved = _resolve_static(raw_path)
    if resolved is not None:
        return _serve_file(resolved)

    # SPA fallback: unknown deep links render the React app
    index = SPA_DIR / "index.html"
    if not index.exists():
        return _json(500, {"error": "dashboard SPA not bundled (missing spa/index.html)"})
    return _serve_file(index)