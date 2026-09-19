"""UDS HTTP client. CLI and future consumers share this; no resolver copy."""
from __future__ import annotations

import json
import os
import socket
from http.client import HTTPConnection
from pathlib import Path
from time import time
from typing import Any

from .http_api import unavailable
from .types import iso_from


class UDSConnection(HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock = sock
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)


def default_socket_path() -> str:
    override = os.environ.get("SPECTRE_WORKER_STATE_SOCK", "").strip()
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        return str(Path(runtime) / "spectre-worker-state.sock")
    uid = os.getuid()
    return f"/run/user/{uid}/spectre-worker-state.sock"


def default_db_path() -> str:
    override = os.environ.get("SPECTRE_WORKER_STATE_DB", "").strip()
    if override:
        return override
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(state) if state else Path.home() / ".local" / "state"
    return str(root / "remote-agent" / "worker-state.sqlite")


class StateClient:
    def __init__(self, socket_path: str | None = None, timeout: float = 5.0):
        self.socket_path = socket_path or default_socket_path()
        self.timeout = timeout

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        conn = UDSConnection(self.socket_path, timeout=self.timeout)
        headers = {"Host": "localhost", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                parsed = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                parsed = {"ok": False, "error": "bad_response", "detail": raw[:200].decode("utf-8", "replace")}
            if not isinstance(parsed, dict):
                parsed = {"ok": False, "error": "bad_response"}
            return resp.status, parsed
        finally:
            conn.close()

    def snapshot(self, worker: str, now: float | None = None) -> dict[str, Any]:
        stamp = time() if now is None else now
        try:
            status, payload = self.request("GET", f"/v1/workers/{worker}/snapshot")
        except (OSError, TimeoutError, socket.error) as exc:
            return unavailable(worker, str(exc), stamp)
        if status != 200:
            return unavailable(worker, payload.get("detail") or payload.get("error") or str(status), stamp)
        return payload

    def health(self) -> tuple[int, dict[str, Any]]:
        try:
            return self.request("GET", "/v1/health")
        except (OSError, TimeoutError, socket.error) as exc:
            return 503, {"ok": False, "error": "unavailable", "detail": str(exc), "server_time": iso_from(time())}

    def evidence(self, event: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self.request("POST", "/v1/evidence", event)

    def claim(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self.request("POST", "/v1/actions/claim", payload)

    def result(self, action_id: str, ok: bool, error: str | None = None) -> tuple[int, dict[str, Any]]:
        body: dict[str, Any] = {"ok": ok}
        if error:
            body["error"] = error
        return self.request("POST", f"/v1/actions/{action_id}/result", body)

    def reconcile(self, worker: str) -> tuple[int, dict[str, Any]]:
        return self.request("POST", "/v1/reconcile", {"worker": worker})
