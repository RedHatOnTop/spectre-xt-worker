"""Kimi handoff is explicit, visible, and committed only after observation."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests import test_control_plane as fixtures
from control_plane import kimi, planning, seat
from control_plane.io import read_json, write_json


class Client:
    def __init__(self):
        self.value = {'snapshot_version': 7, 'goal': {'state': 'COMPLETED',
            'goal_id': 'goal-7'}, 'policy': {}}

    def snapshot(self, worker):
        return self.value


class KimiHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cwd = self.root / 'minecraft'
        self.cwd.mkdir()
        self.client = Client()
        self.workers = self.root / 'workers.json'
        self.provider = self.root / 'provider.json'
        self.loop = self.root / 'loop.json'
        self.config = self.root / '.kimi-code/config.toml'
        self.config.parent.mkdir()
        self.config.write_text('default_model = "cline/kimi-k3"\n'
            '[providers.cline]\ntype = "openai"\n'
            'base_url = "http://127.0.0.1:8790/v1"\napi_key = "dummy"\n'
            '[models."cline/kimi-k3"]\nprovider = "cline"\n'
            'model = "cline-free/kimi-k3"\n')
        self.config.chmod(0o600)
        binary = self.config.parent / 'bin/kimi'
        binary.parent.mkdir()
        binary.write_text('')
        binary.chmod(0o700)
        write_json(self.workers, {'workers': {'minecraft': {
            'cwd': str(self.cwd), 'class': 'toplevel', 'planner': {'terminal': 'term_astra'}}}})
        write_json(self.provider, {'ok': True, 'id': 'kimi_free', 'checked_at': fixtures.NOW})
        brief = seat.brief_path(self.root)
        brief.parent.mkdir(parents=True)
        brief.write_text('# Goal\nContinue Nether work.\n')
        self.origin = planning.identity(self.client.snapshot('minecraft'))
        write_json(self.loop, {'workers': {'minecraft': {'handoff': {
            'to': 'kimi', 'origin': self.origin, 'brief': str(brief)}}}})
        self.calls = []
        self.processes = [{'model': 'efficient', 'cwd': str(self.cwd),
                           'handle': 'term_efficient'}]

    def tearDown(self):
        self.temp.cleanup()

    def fake_run(self, command, **_kwargs):
        self.calls.append(command)
        action = command[2]
        if action == 'list':
            terminals = [{'handle': 'term_efficient', 'worktreePath': str(self.cwd),
                          'connected': True, 'writable': True},
                         {'handle': 'term_kimi', 'worktreePath': str(self.cwd),
                          'connected': True, 'writable': True}]
            return {'ok': True, 'parsed': {'result': {'terminals': terminals}}}
        if action == 'create':
            return {'ok': True, 'parsed': {'result': {'terminal': {'handle': 'term_kimi'}}}}
        if action == 'send':
            return {'ok': True, 'parsed': {'ok': True}}
        raise AssertionError(command)

    def launch(self, **kwargs):
        return kimi.launch(self.workers, self.provider, self.loop, self.root,
                           self.config, self.client, now=fixtures.NOW,
                           env={'SPECTRE_KIMI_ENABLED': '1'}, run=self.fake_run,
                           scan=lambda: self.processes,
                           probe_fn=lambda *_: {'ok': True}, **kwargs)

    def prompted(self):
        launched = self.launch()
        self.assertTrue(launched['ok'])
        self.processes = [*self.processes, {'model': 'kimi', 'cwd': str(self.cwd),
                                           'handle': 'term_kimi'}]
        return kimi.send_prompt(self.workers, self.provider, self.loop, self.root,
                                self.client, 'term_kimi', now=fixtures.NOW + 1,
                                env={'SPECTRE_KIMI_ENABLED': '1'}, run=self.fake_run,
                                scan=lambda: self.processes)

    def test_launch_prompt_confirm_commits_only_after_observation(self):
        launched = self.launch()
        self.assertEqual(launched['next'], 'send_prompt')
        self.assertEqual(seat.load(seat.seat_path(self.root))['owner'], 'codex')
        self.assertEqual(read_json(self.loop)['workers']['minecraft']['handoff']['terminal'], 'term_kimi')
        self.processes = [*self.processes, {'model': 'kimi', 'cwd': str(self.cwd),
                                           'handle': 'term_kimi'}]
        sent = kimi.send_prompt(self.workers, self.provider, self.loop, self.root,
                                self.client, 'term_kimi', now=fixtures.NOW + 1,
                                env={'SPECTRE_KIMI_ENABLED': '1'}, run=self.fake_run,
                                scan=lambda: self.processes)
        self.assertTrue(sent['ok'])
        self.assertEqual(seat.load(seat.seat_path(self.root))['owner'], 'codex')
        with self.assertRaisesRegex(ValueError, 'observed-ready'):
            kimi.confirm(self.workers, self.provider, self.loop, self.root,
                         self.client, 'term_kimi')
        result = kimi.confirm(self.workers, self.provider, self.loop, self.root,
                              self.client, 'term_kimi', observed_ready=True,
                              now=fixtures.NOW + 2, env={'SPECTRE_KIMI_ENABLED': '1'},
                              run=self.fake_run, scan=lambda: self.processes)
        self.assertEqual(result['owner'], 'kimi')
        self.assertEqual(seat.load(seat.seat_path(self.root))['terminal'], 'term_kimi')
        self.assertIsNone(read_json(self.loop)['workers']['minecraft']['handoff'])
        self.assertIn('Continue Nether work.', seat.brief_path(self.root).read_text())

    def test_failed_probe_creates_no_terminal_or_seat(self):
        result = kimi.launch(self.workers, self.provider, self.loop, self.root,
                             self.config, self.client, now=fixtures.NOW,
                             env={'SPECTRE_KIMI_ENABLED': '1'}, run=self.fake_run,
                             scan=lambda: self.processes,
                             probe_fn=lambda *_: {'ok': False, 'error': 'kimi_probe_http',
                                                   'status': 401})
        self.assertEqual(result['status'], 401)
        self.assertFalse(any(command[2] == 'create' for command in self.calls))
        self.assertFalse(seat.seat_path(self.root).exists())

    def test_rate_limited_probe_invalidates_candidate_and_records_quota(self):
        from unittest.mock import patch
        with patch('control_plane.kimi.cline_free.record') as record:
            result = kimi.launch(self.workers, self.provider, self.loop, self.root,
                                 self.config, self.client, now=fixtures.NOW,
                                 env={'SPECTRE_KIMI_ENABLED': '1'}, run=self.fake_run,
                                 scan=lambda: self.processes,
                                 probe_fn=lambda *_: {'ok': False, 'error': 'kimi_probe_http',
                                                       'status': 429})
        self.assertEqual(result['status'], 429)
        record.assert_called_once_with(None, fixtures.NOW, 429)
        self.assertFalse(read_json(self.provider)['ok'])
        self.assertFalse(any(command[2] == 'create' for command in self.calls))

    def test_stale_handoff_and_public_config_fail_closed(self):
        self.client.value = {**self.client.value, 'goal': {
            **self.client.value['goal'], 'attempt_id': 'attempt-8'}}
        with self.assertRaisesRegex(ValueError, 'authoritative Kimi handoff'):
            self.launch()
        self.client.value = {**self.client.value, 'goal': {
            'state': 'COMPLETED', 'goal_id': 'goal-7'}}
        self.config.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private'):
            self.launch()
        self.assertFalse(any(command[2] == 'create' for command in self.calls))

    def test_no_prompt_to_unconfirmed_process(self):
        self.launch()
        result = kimi.send_prompt(self.workers, self.provider, self.loop, self.root,
                                  self.client, 'term_kimi', now=fixtures.NOW + 1,
                                  env={'SPECTRE_KIMI_ENABLED': '1'}, run=self.fake_run,
                                  scan=lambda: self.processes)
        self.assertEqual(result['error'], 'kimi_terminal_unconfirmed')
        self.assertFalse(any(command[2] == 'send' for command in self.calls))

    def test_duplicate_launch_does_not_create_another_terminal(self):
        self.launch()
        result = self.launch()
        self.assertEqual(result['error'], 'kimi_terminal_already_created')
        self.assertEqual(len([command for command in self.calls if command[2] == 'create']), 1)

    def test_dry_run_does_not_probe_or_create(self):
        result = kimi.launch(self.workers, self.provider, self.loop, self.root,
                             self.config, self.client, now=fixtures.NOW,
                             env={'SPECTRE_KIMI_ENABLED': '1'}, run=self.fake_run,
                             scan=lambda: self.processes, dry=True,
                             probe_fn=lambda *_: self.fail('dry run must not probe'))
        self.assertTrue(result['dry_run'])
        self.assertFalse(any(command[2] == 'create' for command in self.calls))

    def test_running_astra_or_missing_efficient_blocks_launch(self):
        self.processes = [*self.processes, {'model': 'astra', 'cwd': str(self.cwd),
                                           'handle': 'term_astra'}]
        self.assertEqual(self.launch()['error'], 'top_level_agent_already_running')
        self.processes = []
        self.assertEqual(self.launch()['error'],
                         'efficient_pin_required_before_second_terminal')

    def test_create_timeout_requires_inspection(self):
        def timeout_create(command, **kwargs):
            if command[2] == 'create':
                return {'ok': False, 'uncertain': True}
            return self.fake_run(command, **kwargs)

        result = kimi.launch(self.workers, self.provider, self.loop, self.root,
                             self.config, self.client, now=fixtures.NOW,
                             env={'SPECTRE_KIMI_ENABLED': '1'}, run=timeout_create,
                             scan=lambda: self.processes,
                             probe_fn=lambda *_: {'ok': True})
        self.assertIn('inspect terminal list', result['error'])
        self.assertNotIn('terminal', read_json(self.loop)['workers']['minecraft']['handoff'])

    def test_prompt_timeout_is_not_retried(self):
        self.launch()
        self.processes = [*self.processes, {'model': 'kimi', 'cwd': str(self.cwd),
                                           'handle': 'term_kimi'}]
        sends = []

        def timeout_send(command, **kwargs):
            if command[2] == 'send':
                sends.append(command)
                return {'ok': False, 'uncertain': True}
            return self.fake_run(command, **kwargs)

        args = (self.workers, self.provider, self.loop, self.root,
                self.client, 'term_kimi')
        first = kimi.send_prompt(*args, now=fixtures.NOW + 1,
                                 env={'SPECTRE_KIMI_ENABLED': '1'},
                                 run=timeout_send, scan=lambda: self.processes)
        second = kimi.send_prompt(*args, now=fixtures.NOW + 2,
                                  env={'SPECTRE_KIMI_ENABLED': '1'},
                                  run=timeout_send, scan=lambda: self.processes)
        self.assertEqual(first['error'], 'kimi_prompt_send_unconfirmed')
        self.assertIn('inspect terminal', second['error'])
        self.assertEqual(len(sends), 1)

    def test_corrupt_configuration_and_disabled_gate_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'SPECTRE_KIMI_ENABLED'):
            kimi.launch(self.workers, self.provider, self.loop, self.root,
                        self.config, self.client, now=fixtures.NOW,
                        env={}, run=self.fake_run, scan=lambda: self.processes)
        self.config.write_text('[providers]\ncline = "wrong"\n[models]\n')
        with self.assertRaisesRegex(ValueError, 'provider or model entry'):
            self.launch()

    def test_endpoint_and_config_permissions_are_checked(self):
        with self.assertRaisesRegex(ValueError, 'loopback'):
            kimi.config_endpoint(self.config, 'http://example.com:8790/v1')
        self.config.write_text(self.config.read_text().replace(
            'http://127.0.0.1:8790/v1', 'http://127.0.0.1:8787/v1'))
        with self.assertRaisesRegex(ValueError, 'does not target'):
            kimi.config_endpoint(self.config, 'http://127.0.0.1:8790/v1')
        link = self.root / 'config-link.toml'
        link.symlink_to(self.config)
        with self.assertRaisesRegex(ValueError, 'private'):
            kimi.config_endpoint(link, 'http://127.0.0.1:8790/v1')

    def test_confirm_refuses_changed_seat(self):
        self.prompted()
        with patch('control_plane.kimi.seat.commit_if_current',
                   return_value={'ok': False, 'error': 'seat_changed'}):
            result = kimi.confirm(self.workers, self.provider, self.loop,
                                  self.root, self.client, 'term_kimi',
                                  observed_ready=True, now=fixtures.NOW + 2,
                                  env={'SPECTRE_KIMI_ENABLED': '1'},
                                  run=self.fake_run, scan=lambda: self.processes)
        self.assertEqual(result['error'], 'seat_changed')
        self.assertEqual(seat.load(seat.seat_path(self.root))['owner'], 'codex')

    def test_release_requires_stopped_kimi_and_fresh_astra_provider(self):
        self.prompted()
        confirmed = kimi.confirm(self.workers, self.provider, self.loop, self.root,
                                 self.client, 'term_kimi', observed_ready=True,
                                 now=fixtures.NOW + 2, env={'SPECTRE_KIMI_ENABLED': '1'},
                                 run=self.fake_run, scan=lambda: self.processes)
        self.assertTrue(confirmed['ok'])
        write_json(self.provider, {'ok': True, 'id': 'anyrouter',
                                   'checked_at': fixtures.NOW + 3})
        with self.assertRaisesRegex(ValueError, 'observed-stopped'):
            kimi.release(self.workers, self.provider, self.loop, self.root)
        still_running = kimi.release(self.workers, self.provider, self.loop,
                                     self.root, observed_stopped=True,
                                     now=fixtures.NOW + 3,
                                     scan=lambda: self.processes)
        self.assertEqual(still_running['error'], 'top_level_agent_still_running')
        self.processes = [row for row in self.processes if row['model'] != 'kimi']
        released = kimi.release(self.workers, self.provider, self.loop,
                                self.root, observed_stopped=True,
                                now=fixtures.NOW + 3,
                                scan=lambda: self.processes)
        self.assertEqual(released['owner'], 'codex')
        self.assertEqual(seat.load(seat.seat_path(self.root))['handoff_count'], 2)
        self.assertIsNone(seat.load(seat.seat_path(self.root))['terminal'])

    def test_confirm_and_release_move_the_planner_pin_with_the_seat(self):
        self.prompted()
        confirmed = kimi.confirm(self.workers, self.provider, self.loop, self.root,
                                 self.client, 'term_kimi', observed_ready=True,
                                 now=fixtures.NOW + 2, env={'SPECTRE_KIMI_ENABLED': '1'},
                                 run=self.fake_run, scan=lambda: self.processes)
        self.assertTrue(confirmed['ok'])
        entry = read_json(self.workers)['workers']['minecraft']
        self.assertEqual(entry['planner'], {'terminal': 'term_kimi', 'harness': 'kimi',
                                            'model': 'cline-free/kimi-k3',
                                            'provider': 'kimi_free'})
        self.assertEqual((entry['cwd'], entry['class']), (str(self.cwd), 'toplevel'))
        write_json(self.provider, {'ok': True, 'id': 'anyrouter',
                                   'checked_at': fixtures.NOW + 3})
        self.processes = [row for row in self.processes if row['model'] != 'kimi']
        released = kimi.release(self.workers, self.provider, self.loop, self.root,
                                observed_stopped=True, now=fixtures.NOW + 3,
                                scan=lambda: self.processes)
        self.assertEqual(released['next'], 'launch_astra')
        self.assertEqual(read_json(self.workers)['workers']['minecraft']['planner'],
                         {'terminal': None, 'harness': 'codex', 'model': 'gpt-6-astra'})

    def test_registry_failure_after_seat_commit_is_reported(self):
        self.prompted()
        with patch('control_plane.kimi.set_planner', side_effect=OSError):
            result = kimi.confirm(self.workers, self.provider, self.loop, self.root,
                                  self.client, 'term_kimi', observed_ready=True,
                                  now=fixtures.NOW + 2, env={'SPECTRE_KIMI_ENABLED': '1'},
                                  run=self.fake_run, scan=lambda: self.processes)
        self.assertEqual(result, {'ok': False, 'error': 'registry_update_failed',
                                  'seat_committed': True, 'terminal': 'term_kimi'})
        self.assertEqual(seat.load(seat.seat_path(self.root))['owner'], 'kimi')
        self.assertEqual(read_json(self.workers)['workers']['minecraft']['planner'],
                         {'terminal': 'term_astra'})
        write_json(self.provider, {'ok': True, 'id': 'anyrouter',
                                   'checked_at': fixtures.NOW + 3})
        with patch('control_plane.kimi.set_planner', side_effect=OSError):
            released = kimi.release(self.workers, self.provider, self.loop, self.root,
                                    observed_stopped=True, now=fixtures.NOW + 3,
                                    scan=lambda: [])
        self.assertEqual(released, {'ok': False, 'error': 'registry_update_failed',
                                    'seat_committed': True})
        self.assertEqual(seat.load(seat.seat_path(self.root))['owner'], 'codex')

    def test_running_claude_planner_blocks_kimi_launch_and_release(self):
        claude = {'model': 'claude', 'cwd': str(self.cwd), 'handle': 'term_claude'}
        self.processes = [*self.processes, claude]
        self.assertEqual(self.launch()['error'], 'top_level_agent_already_running')
        self.assertFalse(any(command[2] == 'create' for command in self.calls))
        write_json(seat.seat_path(self.root), {**seat.default_state(), 'owner': 'kimi'})
        write_json(self.provider, {'ok': True, 'id': 'anyrouter', 'checked_at': fixtures.NOW})
        result = kimi.release(self.workers, self.provider, self.loop, self.root,
                              observed_stopped=True, now=fixtures.NOW,
                              scan=lambda: [claude])
        self.assertEqual(result['error'], 'top_level_agent_still_running')
        self.assertEqual(seat.load(seat.seat_path(self.root))['owner'], 'kimi')

    def test_release_refuses_stale_provider_and_wrong_owner(self):
        with self.assertRaisesRegex(ValueError, 'fresh Astra provider'):
            kimi.release(self.workers, self.provider, self.loop, self.root,
                         observed_stopped=True, now=fixtures.NOW,
                         scan=lambda: [])
        write_json(self.provider, {'ok': True, 'id': 'anyrouter',
                                   'checked_at': fixtures.NOW})
        with self.assertRaisesRegex(ValueError, 'seat_owned_by_codex'):
            kimi.release(self.workers, self.provider, self.loop, self.root,
                         observed_stopped=True, now=fixtures.NOW,
                         scan=lambda: [])


class KimiProbeTest(unittest.TestCase):
    def test_probe_requires_cline_provider_and_preserves_vendor_model(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append({'model': body['model'], 'auth': self.headers.get('Authorization')})
                payload = b'{"choices":[{"message":{"content":"OK"}}]}'
                self.send_response(200)
                self.send_header('X-Omni-Provider', 'cline-free')
                self.send_header('X-Omni-Attempts', '1')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = kimi.probe(f'http://127.0.0.1:{server.server_port}/v1', 'dummy')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertTrue(result['ok'])
        self.assertEqual(calls, [{'model': 'cline-free/kimi-k3',
                                  'auth': 'Bearer dummy'}])

    def test_probe_does_not_follow_redirect_or_accept_wrong_provider(self):
        class Handler(BaseHTTPRequestHandler):
            mode = 'redirect'

            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', 'http://example.invalid/collect')
                    self.end_headers()
                    return
                payload = b'{"choices":[{}]}'
                self.send_response(200)
                self.send_header('X-Omni-Provider', 'paid')
                self.send_header('X-Omni-Attempts', '1')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f'http://127.0.0.1:{server.server_port}/v1'
            self.assertEqual(kimi.probe(endpoint, 'dummy')['status'], 302)
            Handler.mode = 'wrong_provider'
            self.assertEqual(kimi.probe(endpoint, 'dummy')['error'],
                             'kimi_probe_invalid_response')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
