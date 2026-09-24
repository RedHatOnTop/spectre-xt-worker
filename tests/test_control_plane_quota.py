from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import tempfile
import threading
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_plane import quota


class HealthHandler(BaseHTTPRequestHandler):
    payload = {}

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class FreeQuotaTest(unittest.TestCase):
    def test_proxy_429_marks_packet_model_exhausted_durably(self):
        now = 1_800_000_000.0
        HealthHandler.payload = {'ok': True, 'providers': ['cline-free', 'cline-paid'],
                                 'stats': {'attempts': {quota.PACKET_ATTEMPT: {
                                     'last_429_at': int(now - 5), 'rate_limits': 1}}}}
        server = HTTPServer(('127.0.0.1', 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'packet-usage.json'
                env = {'SPECTRE_OMNI_ENDPOINT': f'http://127.0.0.1:{server.server_port}/v1'}
                status = quota.free_status_now(now, path, env)
                self.assertFalse(status['ok'])
                self.assertTrue(status['exhausted'])
                self.assertTrue(path.exists())
                self.assertFalse(quota.free_status_now(now + 10, path, env)['ok'])
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_proxy_unavailable_never_selects_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = quota.free_status_now(1_800_000_000.0, Path(tmp) / 'usage.json',
                                           {'SPECTRE_OMNI_ENDPOINT': 'http://example.com/v1'})
            self.assertFalse(status['ok'])
            self.assertEqual(status['reason'], 'proxy_unavailable')
            status = quota.free_status_now(1_800_000_000.0, Path(tmp) / 'usage.json',
                                           {'SPECTRE_OMNI_ENDPOINT': 'http://[bad/v1'})
            self.assertEqual(status['reason'], 'proxy_unavailable')

    def test_malformed_proxy_health_fails_closed(self):
        HealthHandler.payload = {'ok': True, 'providers': ['cline-free', 'cline-paid'],
                                 'stats': []}
        server = HTTPServer(('127.0.0.1', 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env = {'SPECTRE_OMNI_ENDPOINT': f'http://127.0.0.1:{server.server_port}/v1'}
                status = quota.free_status_now(1_800_000_000.0,
                                               Path(tmp) / 'usage.json', env)
                self.assertEqual(status['reason'], 'proxy_unavailable')
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_free_auth_rejection_routes_to_paid_without_retrying(self):
        HealthHandler.payload = {'ok': True, 'providers': ['cline-free', 'cline-paid'],
                                 'stats': {'attempts': {quota.PACKET_ATTEMPT: {
                                     'last_status': 401, 'last_429_at': 0}}}}
        server = HTTPServer(('127.0.0.1', 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env = {'SPECTRE_OMNI_ENDPOINT': f'http://127.0.0.1:{server.server_port}/v1'}
                status = quota.free_status_now(1_800_000_000.0,
                                               Path(tmp) / 'usage.json', env)
                self.assertFalse(status['ok'])
                self.assertEqual(status['reason'], 'free_credentials_unavailable')
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == '__main__':
    unittest.main()
