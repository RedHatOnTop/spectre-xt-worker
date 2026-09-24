#!/usr/bin/env python3
"""In-process HTTP API + UDS client fail-closed tests."""
from __future__ import annotations

import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worker_state.cli import main as cli_main
from worker_state.client import StateClient
from worker_state.http_api import handle
from worker_state.server import Handler, UnixHTTPServer
from worker_state.store import Store
from worker_state.types import iso_from

NOW = 1_800_000_000.0


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_health_and_empty_snapshot(self):
        code, body = handle(self.store, "GET", "/v1/health", None, NOW)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        code, body = handle(self.store, "GET", "/v1/workers/pugc/snapshot", None, NOW)
        self.assertEqual(code, 200)
        self.assertEqual(body["goal"]["state"], "UNKNOWN")
        self.assertFalse(body["policy"]["can_dispatch_goal"])

    def test_post_evidence(self):
        code, body = handle(
            self.store,
            "POST",
            "/v1/evidence",
            json.dumps(
                {
                    "event_id": "e1",
                    "worker": "pugc",
                    "kind": "model.request.started",
                    "source": "session_jsonl",
                    "turn_id": "t1",
                    "source_timestamp": iso_from(NOW),
                }
            ),
            NOW,
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["goal"]["state"], "RUNNING")

    def test_claim_result_roundtrip(self):
        self.store.snapshot("pugc", NOW, rebuild=True)
        handle(self.store, "GET", "/v1/workers/pugc/snapshot", None, NOW)
        code, claim = handle(
            self.store,
            "POST",
            "/v1/actions/claim",
            json.dumps(
                {
                    "worker": "pugc",
                    "action": "dispatch_goal",
                    "expected_snapshot_version": 0,
                    "idempotency_key": "k1",
                }
            ),
            NOW,
        )
        self.assertEqual(code, 200, claim)
        code, result = handle(
            self.store,
            "POST",
            f"/v1/actions/{claim['action_id']}/result",
            json.dumps({"ok": True}),
            NOW + 1,
        )
        self.assertEqual(code, 200)
        self.assertEqual(result["snapshot"]["goal"]["state"], "INJECTED")

    def test_unknown_path(self):
        code, body = handle(self.store, "GET", "/nope", None, NOW)
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])

    def test_invalid_worker(self):
        code, body = handle(self.store, "GET", "/v1/workers/bad!/snapshot", None, NOW)
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "worker")

    def test_uds_client_fail_closed_when_missing(self):
        client = StateClient(str(Path(self.tmp.name) / "missing.sock"))
        snap = client.snapshot("pugc", NOW)
        self.assertEqual(snap["goal"]["state"], "UNKNOWN")
        self.assertFalse(snap["policy"]["can_dispatch_goal"])
        self.assertFalse(snap["ok"])

    def test_uds_roundtrip(self):
        sock = str(Path(self.tmp.name) / "state.sock")
        Handler.store = self.store
        server = UnixHTTPServer(sock, Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = StateClient(sock)
            status, health = client.health()
            self.assertEqual(status, 200)
            self.assertTrue(health["ok"])
            status, _ = client.evidence(
                {
                    "event_id": "e-uds",
                    "worker": "qoder",
                    "kind": "turn.ended",
                    "source": "session_jsonl",
                    "turn_id": "t9",
                    "payload": {"reason": "end_turn"},
                }
            )
            self.assertEqual(status, 200)
            snap = client.snapshot("qoder")
            self.assertEqual(snap["goal"]["state"], "IDLE")
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_help_exits_zero(self):
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            cli_main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("health", buf.getvalue())

    def test_cli_health_without_socket_fails_closed(self):
        missing = str(Path(self.tmp.name) / "no.sock")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["--socket", missing, "health"])
        self.assertEqual(code, 1)
        self.assertIn("unavailable", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

class UnixBindTest(unittest.TestCase):
    def test_unix_bind_never_resolves_a_hostname(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            with patch('socket.getfqdn', side_effect=AssertionError('UDS bind performed DNS')):
                server = UnixHTTPServer(str(Path(tmp) / 'state.sock'), Handler)
                server.server_close()

    def test_second_server_cannot_replace_live_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'state.sock')
            first = UnixHTTPServer(path, Handler)
            try:
                with self.assertRaises(OSError):
                    UnixHTTPServer(path, Handler)
                self.assertTrue(Path(path).exists())
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(1)
                    client.connect(path)
            finally:
                first.server_close()

    def test_stale_socket_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'state.sock')
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(path)
            stale.close()
            server = UnixHTTPServer(path, Handler)
            try:
                self.assertTrue(Path(path).exists())
            finally:
                server.server_close()

    def test_close_does_not_unlink_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sock'
            server = UnixHTTPServer(str(path), Handler)
            path.unlink()
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            replacement.bind(str(path))
            try:
                server.server_close()
                self.assertTrue(path.exists())
            finally:
                replacement.close()
