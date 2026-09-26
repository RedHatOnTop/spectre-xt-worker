"""HTTP request handler over a Store. No sockets here so tests stay in-process."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

from .policy import fail_closed_snapshot
from .store import Store, StoreError
from .types import iso_from

MAX_BODY = 64 * 1024
WORKER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PATH_SNAPSHOT = re.compile(r"^/v1/workers/([^/]+)/snapshot$")
PATH_SHADOW = re.compile(r"^/v1/workers/([^/]+)/shadow$")
PATH_RESULT = re.compile(r"^/v1/actions/([^/]+)/result$")


def handle(
    store: Store,
    method: str,
    path: str,
    body: bytes | str | None,
    now: float,
) -> tuple[int, dict[str, Any]]:
    method = method.upper()
    path = path.split("?", 1)[0]
    try:
        payload = _parse_body(body) if method == "POST" else {}
    except StoreError as exc:
        return exc.http, {"ok": False, "error": exc.code, "detail": exc.message}

    try:
        if method == "GET" and path == "/v1/health":
            return 200, store.health(now)
        snap_match = PATH_SNAPSHOT.match(path)
        if method == "GET" and snap_match:
            worker = _worker(unquote(snap_match.group(1)))
            return 200, store.snapshot(worker, now, rebuild=False)
        shadow_match = PATH_SHADOW.match(path)
        if method == "GET" and shadow_match:
            worker = _worker(unquote(shadow_match.group(1)))
            rows = store.shadow_rows(worker)
            return 200, {"ok": True, "worker": worker, "divergences": rows}
        if method == "POST" and path == "/v1/evidence":
            snapshot = store.ingest(payload, now)
            return 200, snapshot
        if method == "POST" and path == "/v1/actions/claim":
            worker = _worker(str(payload.get("worker") or ""))
            extra = {}
            if payload.get("target"):
                extra["target"] = str(payload.get("target"))
            if payload.get("terminal"):
                extra["terminal"] = str(payload.get("terminal"))
            if payload.get("pin"):
                extra["pin"] = str(payload.get("pin"))
            claim = store.claim(
                worker,
                str(payload.get("action") or ""),
                int(payload.get("expected_snapshot_version")),
                str(payload.get("idempotency_key") or ""),
                now,
                extra or None,
            )
            return 200, {"ok": True, **claim}
        result_match = PATH_RESULT.match(path)
        if method == "POST" and result_match:
            out = store.action_result(
                result_match.group(1),
                bool(payload.get("ok")),
                now,
                payload.get("error"),
            )
            return 200, {"ok": True, **out}
        if method == "POST" and path == "/v1/reconcile":
            worker = _worker(str(payload.get("worker") or ""))
            return 200, store.reconcile(worker, now)
    except StoreError as exc:
        return exc.http, {"ok": False, "error": exc.code, "detail": exc.message}
    except (TypeError, ValueError) as exc:
        return 400, {"ok": False, "error": "bad_request", "detail": str(exc)}
    return 404, {"ok": False, "error": "not_found", "detail": path}


def unavailable(worker: str, error: str, now: float) -> dict[str, Any]:
    return fail_closed_snapshot(worker, error, iso_from(now))


def _worker(name: str) -> str:
    worker = name.strip().lower()
    if not WORKER_RE.match(worker):
        raise StoreError("worker", f"invalid worker {name!r}")
    return worker


def _parse_body(body: bytes | str | None) -> dict[str, Any]:
    if body is None or body == b"" or body == "":
        return {}
    if isinstance(body, bytes):
        if len(body) > MAX_BODY:
            raise StoreError("body", "request body too large", 413)
        text = body.decode("utf-8")
    else:
        if len(body) > MAX_BODY:
            raise StoreError("body", "request body too large", 413)
        text = body
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StoreError("json", f"invalid json: {exc}") from exc
    if not isinstance(payload, dict):
        raise StoreError("json", "body must be an object")
    return payload
