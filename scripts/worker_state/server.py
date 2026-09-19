"""Unix-domain HTTP server plus optional jsonl poll / shadow loop."""
from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time
from typing import Any

from . import process, qoder_jsonl
from .http_api import MAX_BODY, handle
from .shadow import compare, load_old_classifier
from .store import Store
from .types import iso_from

_POLL_LOG = Path("/work/logs/worker-state.log")


def _poll_log(text: str) -> None:
    line = f"{iso_from(time())} {text}\n"
    for path in (_POLL_LOG, Path.home() / ".local/state/remote-agent/worker-state.log"):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            return
        except OSError:
            continue


class UnixHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        path = self.server_address
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        super().server_bind()
        os.chmod(path, 0o600)

    def get_request(self):
        request, _client = self.socket.accept()
        return request, ("unix", 0)

    def server_close(self) -> None:
        path = self.server_address
        super().server_close()
        if isinstance(path, str) and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    store: Store
    lock: threading.Lock

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, {"ok": False, "error": "body", "detail": "request body too large"})
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET", b"")

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        if self.headers.get("Content-Length") and int(self.headers.get("Content-Length") or 0) > MAX_BODY:
            return
        self._dispatch("POST", body)

    def _dispatch(self, method: str, body: bytes) -> None:
        code, payload = handle(self.store, method, self.path, body, time())
        self._send(code, payload)


def ingest_workers(
    store: Store,
    workers: dict[str, dict],
    sessions_root: Path,
    now: float,
    *,
    shadow: bool = True,
    shadow_log: Path | None = None,
) -> dict[str, Any]:
    old = load_old_classifier() if shadow else None
    results: dict[str, Any] = {}
    for name, entry in workers.items():
        for event in qoder_jsonl.collect_worker_evidence(name, entry, sessions_root):
            store.ingest(event, now)
        for event in process.evidence_for_worker(name, entry, now):
            store.ingest(event, now)
        try:
            store.reconcile(name, now)
        except Exception:
            pass
        snapshot = store.snapshot(name, now)
        comparison = None
        if old is not None:
            comparison = _shadow_one(store, old, name, entry, sessions_root, snapshot, now, shadow_log)
        results[name] = {"snapshot": snapshot, "shadow": comparison}
    return results


def _shadow_one(
    store: Store,
    old_mod: Any,
    name: str,
    entry: dict,
    sessions_root: Path,
    snapshot: dict,
    now: float,
    shadow_log: Path | None,
) -> dict[str, Any]:
    cwd = str(entry.get("cwd") or "")
    segment = qoder_jsonl.newest_segment(sessions_root, cwd) if cwd else None
    records: list[dict] = []
    if segment is not None:
        try:
            records = qoder_jsonl.tail_records(segment, count=3)
        except OSError:
            records = []
    old_pos = old_mod.position(records, now)
    result = compare(old_pos, snapshot)
    if result["kinds"]:
        for kind in result["kinds"]:
            store.record_shadow(name, kind, old_pos, snapshot, now)
        if shadow_log is not None:
            try:
                shadow_log.parent.mkdir(parents=True, exist_ok=True)
                with shadow_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"worker": name, "ts": iso_from(now), **result}) + "\n")
            except OSError:
                pass
    return result


def serve_forever(
    socket_path: str,
    store: Store,
    *,
    workers_file: Path | None = None,
    sessions_dir: Path | None = None,
    poll_interval: float = 2.0,
    shadow: bool = True,
    shadow_log: Path | None = None,
    stop: threading.Event | None = None,
) -> None:
    Handler.store = store
    server = UnixHTTPServer(socket_path, Handler)
    halt = stop or threading.Event()

    def poll() -> None:
        while not halt.is_set():
            try:
                workers = qoder_jsonl.load_workers(workers_file)
                ingest_workers(
                    store,
                    workers,
                    sessions_dir or qoder_jsonl.SESSIONS_DIR,
                    time(),
                    shadow=shadow,
                    shadow_log=shadow_log,
                )
            except Exception as exc:
                _poll_log(f"poll_failed {type(exc).__name__}: {exc}")
            halt.wait(poll_interval)

    thread = threading.Thread(target=poll, name="worker-state-poll", daemon=True)
    if poll_interval > 0:
        thread.start()
    try:
        server.serve_forever()
    finally:
        halt.set()
        server.server_close()
