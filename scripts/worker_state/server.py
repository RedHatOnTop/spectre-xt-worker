"""Unix-domain HTTP server plus optional jsonl poll / shadow loop."""
from __future__ import annotations

import json
import os
import socket
import stat
from socketserver import TCPServer
import threading
from weakref import WeakKeyDictionary
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time
from typing import Any

from . import dsh_jsonl, process, qoder_jsonl
from .batch import ingest_batch
from .http_api import MAX_BODY, handle
from .shadow import compare, load_old_classifier
from .store import Store
from .types import Event, iso_from

_POLL_LOG = Path("/work/logs/worker-state.log")
_PACKETS = Path.home() / ".local/state/remote-agent/packets"
_TERMINAL = frozenset({"COMPLETED", "FAILED", "IDLE"})
_RECONCILED: WeakKeyDictionary[Store, dict[str, float]] = WeakKeyDictionary()
RECONCILE_INTERVAL_SEC = 60


def _flash_died_at(events: list[Event], dispatch_id: str, now: float) -> float | None:
    """First dead process.sample after an alive sample in this dispatch epoch.

    Never-seen pid (no alive sample after inject) returns None — not gone.
    Still-alive after an alive sample also returns None.
    """
    seen_alive = False
    died_at: float | None = None
    in_epoch = False
    for event in events:
        payload = event.payload or {}
        if event.kind == "terminal_write.succeeded" and (
            event.dispatch_id == dispatch_id or payload.get("dispatch_id") == dispatch_id
        ):
            in_epoch = True
            seen_alive = False
            died_at = None
            continue
        if not in_epoch or event.kind != "process.sample":
            continue
        ts = event.source_ts
        if ts is None:
            ts = now
        if payload.get("alive"):
            seen_alive = True
            died_at = None
        elif seen_alive and died_at is None:
            died_at = ts
    if not seen_alive:
        return None
    return died_at


def _ingest_dsh_exit(
    store: Store,
    name: str,
    now: float,
    *,
    packets_dir: Path | None = None,
) -> None:
    snap = store.snapshot(name, now, rebuild=False)
    execution = snap.get("execution") or {}
    goal = snap.get("goal") or {}
    dispatch_id = str(goal.get("dispatch_id") or "")
    if not dispatch_id or str(execution.get("target") or "") != "flash":
        return
    if str(goal.get("state") or "") in _TERMINAL:
        return
    died_at = _flash_died_at(store.events_for(name), dispatch_id, now)
    if died_at is None:
        return
    path = (packets_dir or _PACKETS) / f"{dispatch_id}.exit"
    code = dsh_jsonl.parse_exit_file(path)
    if code is None and now - float(died_at) < 30:
        return
    if code is None:
        code = 1
    store.ingest(dsh_jsonl.dsh_exit_event(name, dispatch_id, code, snap, now), now)


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
        try:
            try:
                existing = os.lstat(path)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if not stat.S_ISSOCK(existing.st_mode):
                    raise OSError(f"refusing to replace non-socket path: {path}")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    try:
                        probe.connect(path)
                    except ConnectionRefusedError:
                        if os.lstat(path).st_ino == existing.st_ino:
                            os.unlink(path)
                    else:
                        raise OSError(f"worker-state listener already active: {path}")
            TCPServer.server_bind(self)
            self.server_name = "localhost"
            self.server_port = 0
            os.chmod(path, 0o600)
            self._bound_inode = os.lstat(path).st_ino
        except BaseException:
            self.socket.close()
            raise

    def get_request(self):
        request, _client = self.socket.accept()
        return request, ("unix", 0)

    def server_close(self) -> None:
        path = self.server_address
        super().server_close()
        if isinstance(path, str):
            try:
                current = os.lstat(path)
                if stat.S_ISSOCK(current.st_mode) and current.st_ino == getattr(self, "_bound_inode", None):
                    os.unlink(path)
            except FileNotFoundError:
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
    proc_root: Path | None = None,
    packets_dir: Path | None = None,
) -> dict[str, Any]:
    old = load_old_classifier() if shadow else None
    proc = proc_root or Path("/proc")
    packets = packets_dir or _PACKETS
    results: dict[str, Any] = {}
    for name, entry in workers.items():
        prior = store.snapshot(name, now, rebuild=False)
        if prior["goal"]["state"] == "UNKNOWN":
            prior = store.reconcile(name, now)
        target = str((prior.get("execution") or {}).get("target") or "efficient")
        cwd = str(entry.get("cwd") or "")
        events = process.evidence_for_worker(
            name, entry, now, target=target, proc_root=proc
        )
        if target == "flash":
            injected = (prior.get("goal") or {}).get("injected_at")
            inj_ts = None
            if isinstance(injected, (int, float)):
                inj_ts = float(injected)
            events = [*events, *dsh_jsonl.collect_dsh_evidence(
                name, cwd, now, injected_at=inj_ts)]
        else:
            events = [*events, *qoder_jsonl.collect_worker_evidence(name, entry, sessions_root)]
        added = ingest_batch(store, name, events, now)["added"]
        if target == "flash":
            _ingest_dsh_exit(store, name, now, packets_dir=packets)
        reconciled = _RECONCILED.get(store, {})
        due = now - reconciled.get(name, 0) >= RECONCILE_INTERVAL_SEC
        if due:
            store.reconcile(name, now)
            _RECONCILED[store] = {**reconciled, name: now}
        snapshot = store.snapshot(name, now, rebuild=False)
        comparison = None
        if old is not None and (added or due):
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
