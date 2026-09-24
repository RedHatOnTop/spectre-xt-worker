"""Bounded I/O, provider and validation failure paths."""
from contextlib import redirect_stdout
import http.client
import io
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_plane import cli, providers, packets, astra
from control_plane.io import checked, read_json, run, write_json


class BoundaryTest(unittest.TestCase):
    def test_command_failures_and_output(self):
        self.assertFalse(run(['/nonexistent/spectre-command'])['ok'])
        self.assertTrue(run([sys.executable, '-c', 'print("ok")'])['ok'])
        self.assertEqual(run([sys.executable, '-c', 'print("[]")'])['parsed'], {})
        self.assertFalse(run([sys.executable, '-c', 'print(\'{"ok":false}\')'])['ok'])
        self.assertTrue(run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=.02)['uncertain'])
        with self.assertRaises(RuntimeError):
            checked((409, {'ok': False, 'error': 'conflict'}))

    def test_invalid_objects_and_packet_fields(self):
        value = {'request_id': 'r1', 'worker': 'minecraft', 'wake_reason': 'completed',
                 'packets': [{'id': 'p1', 'assignee': 'flash', 'kind': 'implement', 'goal': 'Fix',
                              'acceptance': ['Test'], 'requires_astra_review': False}]}
        for changed in ({'id': ''}, {'assignee': 'auto'}, {'kind': 'auto'}, {'goal': ''},
                        {'acceptance': []}, {'acceptance': [42]}, {'requires_astra_review': 'false'}):
            with self.assertRaises(ValueError):
                packets.validate({**value, 'packets': [{**value['packets'][0], **changed}]}, 'r1')
        mimo = {**value, 'packets': [{**value['packets'][0], 'assignee': 'mimo', 'kind': 'review'}]}
        self.assertEqual(packets.validate(mimo, 'r1')[0]['assignee'], 'mimo')
        with self.assertRaises(ValueError):
            packets.validate([], 'r1')
        with self.assertRaises(ValueError):
            packets.validate({**value, 'wake_reason': 'wrong'}, 'r1')
        self.assertIsNone(packets.next_goal('relative'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'value.json'
            path.write_text('[]')
            with self.assertRaises(ValueError):
                read_json(path)
            (Path(tmp) / 'next_goal.json').write_text('x' * (packets.FILE_MAX + 1))
            self.assertIsNone(packets.next_goal(tmp))
            with self.assertRaises(ValueError):
                packets.poll(Path(tmp), {'request_id': '../bad'})
            path = Path(tmp) / 'r1.json'
            path.write_text('{')
            first = packets.poll(Path(tmp), {'request_id': 'r1'})
            self.assertIn('error', packets.poll(Path(tmp), {'request_id': 'r1', **first}))
            path.write_text('x' * (packets.FILE_MAX + 1))
            self.assertIn('error', packets.poll(Path(tmp), {'request_id': 'r1'}))


class ProviderBoundaryTest(unittest.TestCase):
    def provider(self, **over):
        return {'id': 'anyrouter', 'base_url': 'https://relay.example/v1',
                'wire_api': 'responses', 'probe_model': 'cheap', **over}

    def test_probe_uses_smallest_valid_body_and_never_logs_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'keys').mkdir()
            key = root / 'keys/anyrouter'
            key.write_text('fixture-secret')
            key.chmod(0o600)
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.return_value = b'{"object":"response"}'
            opener = MagicMock()
            opener.open.return_value = response
            with patch('urllib.request.build_opener', return_value=opener):
                result = providers.probe(self.provider(), root)
            self.assertTrue(result['ok'])
            request = opener.open.call_args.args[0]
            self.assertEqual(request.headers['Authorization'], 'Bearer fixture-secret')
            self.assertEqual(json.loads(request.data)['max_output_tokens'], 16)
            self.assertNotIn('fixture-secret', json.dumps(result))
            response.read.return_value = b'<html>busy</html>'
            with patch('urllib.request.build_opener', return_value=opener):
                self.assertEqual(providers.probe(self.provider(), root)['failure'], 'invalid_response')
            opener.open.side_effect = urllib.error.HTTPError('https://relay.example', 429, 'busy', {}, None)
            with patch('urllib.request.build_opener', return_value=opener):
                result = providers.probe(self.provider(), root)
            self.assertEqual((result['status'], result['failure']), (429, 'no_serving_channel'))
            opener.open.side_effect = urllib.error.HTTPError(
                'https://relay.example', 404, 'nf', {},
                io.BytesIO(b'{"error":{"message":"Invalid URL (POST /v1/v1/responses)"}}'))
            with patch('urllib.request.build_opener', return_value=opener):
                result = providers.probe(self.provider(), root)
            self.assertEqual(result['failure'], 'bad_route')
            self.assertNotIn('Invalid URL', json.dumps(result))
            for failure in (OSError('offline'), http.client.IncompleteRead(b'')):
                opener.open.side_effect = failure
                with patch('urllib.request.build_opener', return_value=opener):
                    self.assertEqual(providers.probe(self.provider(), root)['failure'], 'unreachable')
            key.chmod(0o644)
            self.assertEqual(providers.probe(self.provider(), root)['reason'], 'key_unusable')
            self.assertEqual(providers.probe(self.provider(id='other'), root)['reason'],
                             'relay_unknown')

    def test_chat_request_invalid_url_wire_and_cooldown(self):
        url, body = providers.probe_request(self.provider(wire_api='chat'))
        self.assertTrue(url.endswith('/chat/completions'))
        self.assertEqual(body['max_tokens'], 1)
        for changed in ({'base_url': 'http://relay.example'}, {'wire_api': 'unknown'}):
            with self.assertRaises(ValueError):
                providers.probe_request(self.provider(**changed))
        registry = {'providers': [{'id': name} for name in providers.RELAYS]}
        result = providers.choose(registry, {}, 100,
                                  lambda row: {'ok': False, 'configured': True},
                                  kimi_fn=lambda: {'ok': False, 'configured': True, 'exhausted': True})
        self.assertEqual(result['id'], 'openai')
        self.assertIsNone(providers.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil'))


class ClineFreeTest(unittest.TestCase):
    def test_24h_window_first_429_and_status(self):
        from control_plane import cline_free
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.json'
            now = 1_000.0
            state = cline_free.record(path, now, 200)
            self.assertEqual(state['calls_today'], 1)
            state = cline_free.record(path, now + 5, 200)
            self.assertEqual(state['calls_today'], 2)
            state = cline_free.record(path, now + 9, 429)
            self.assertEqual(state['first_429_at'], now + 9)
            live = cline_free.status(state, now + 10)
            self.assertTrue(live['exhausted'])
            self.assertEqual(live['est_limit'], 2)
            fresh = cline_free.status(state, now + 24 * 3600 + 100)
            self.assertFalse(fresh['exhausted'])
            self.assertIsNone(fresh.get('first_429_at') or None)


class CliBoundaryTest(unittest.TestCase):
    def test_loop_disabled_fixture_and_bad_state(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as output:
            root = Path(tmp)
            workers = root / 'workers.json'
            snaps = root / 'snapshots.json'
            write_json(workers, {'workers': {'w': {'cwd': tmp}}})
            write_json(snaps, {'w': {'goal': {'state': 'UNKNOWN'}, 'policy': {}}})
            with patch.dict(os.environ, {'SPECTRE_LOOP': '0'}):
                self.assertEqual(cli.loop_main(['--workers-file', str(workers)]), 0)
            with patch.dict(os.environ, {'SPECTRE_LOOP': '1'}):
                self.assertEqual(cli.loop_main(['--workers-file', str(workers), '--dry-run', '--snapshot-file', str(snaps)]), 0)
                self.assertEqual(cli.loop_main(['--workers-file', str(root / 'missing')]), 1)
            self.assertIn('disabled', output.getvalue())

    def test_provider_cli_records_rate_limit_and_probe_result(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            root = Path(tmp)
            state = root / 'provider.json'
            write_json(root / 'providers.json', {'providers': [{'id': 'agentrouter'}]})
            args = ['--modes', tmp, '--state', str(state)]
            with (patch('control_plane.cli.providers.probe', return_value={'ok': True, 'status': 200}),
                  patch('control_plane.cli.runtime.notify', return_value={'ok': True}) as notify):
                self.assertEqual(cli.provider_main(args), 0)
                self.assertEqual(cli.provider_main(args), 0)
            self.assertEqual(read_json(state)['id'], 'agentrouter')
            notify.assert_called_once()
            self.assertIn('anyrouter:relay_missing', notify.call_args.args[3])
            self.assertEqual(read_json(state)['alerts_sent'], ['anyrouter:relay_missing'])
            self.assertEqual(cli.provider_main([*args, '--plus-rate-limited']), 1)
            self.assertGreater(read_json(state)['cooldown_until'], read_json(state)['checked_at'])
            (root / 'providers.json').write_text('{')
            self.assertEqual(cli.provider_main(args), 1)

    def test_provider_alert_retries_until_announced(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            root = Path(tmp)
            state = root / 'provider.json'
            write_json(root / 'providers.json', {'providers': [{'id': 'agentrouter'}]})
            args = ['--modes', tmp, '--state', str(state)]
            with (patch('control_plane.cli.providers.probe', return_value={'ok': True, 'status': 200}),
                  patch('control_plane.cli.runtime.notify',
                        side_effect=[{'ok': False}, {'ok': True}]) as notify):
                self.assertEqual(cli.provider_main(args), 0)
                self.assertEqual(read_json(state)['alerts_sent'], [])
                self.assertEqual(cli.provider_main(args), 0)
            self.assertEqual(read_json(state)['alerts_sent'], ['anyrouter:relay_missing'])
            self.assertEqual(notify.call_count, 2)
