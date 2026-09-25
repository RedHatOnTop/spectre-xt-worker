"""Runtime handoff requests must not masquerade as a running Kimi seat."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import test_control_plane as fixtures
from control_plane import astra, cli, providers, seat
from control_plane.io import read_json, write_json


class HandoffRuntimeTest(unittest.TestCase):
    setUp = fixtures.LoopRuntimeTest.setUp
    tearDown = fixtures.LoopRuntimeTest.tearDown
    run_command = fixtures.LoopRuntimeTest.run_command
    tick = fixtures.LoopRuntimeTest.tick

    def test_kimi_selection_requests_handoff_without_claim_or_plan(self):
        fixtures.completed(self.store)
        write_json(self.root / 'provider.json', {
            'ok': True, 'id': 'kimi_free', 'checked_at': fixtures.NOW})

        result = self.tick()

        self.assertEqual(result['actions'][0]['reason'], 'kimi_handoff_required')
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'COMPLETED')
        self.assertFalse(any('--dispatch' in command for command in self.sent))
        self.assertFalse(seat.seat_path(self.root).exists())
        self.assertTrue(seat.brief_path(self.root).is_file())
        pending = read_json(self.path)['workers']['minecraft']['handoff']
        self.assertEqual(pending['to'], 'kimi')

        self.tick(now=fixtures.NOW + 30)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(any('--dispatch' in command for command in self.sent))

    def test_recovered_relay_clears_uncommitted_request(self):
        fixtures.completed(self.store)
        write_json(self.root / 'provider.json', {
            'ok': True, 'id': 'kimi_free', 'checked_at': fixtures.NOW})
        self.tick()
        write_json(self.root / 'provider.json', {
            'ok': True, 'id': 'anyrouter', 'checked_at': fixtures.NOW + 30})

        result = self.tick(now=fixtures.NOW + 30)

        self.assertEqual(result['actions'][0]['action'], 'plan')
        self.assertIsNone(read_json(self.path)['workers']['minecraft'].get('handoff'))
        self.assertTrue(any('plan' in command for command in self.sent))

    def test_committed_kimi_seat_blocks_astra_dispatch(self):
        fixtures.completed(self.store)
        write_json(seat.seat_path(self.root), {
            **seat.default_state(), 'owner': 'kimi'})

        result = self.tick()

        self.assertEqual(result['actions'][0]['reason'], 'seat_owned_by_kimi')
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'COMPLETED')
        self.assertFalse(any('--dispatch' in command for command in self.sent))

    def test_handoff_cap_blocks_request_without_brief(self):
        fixtures.completed(self.store)
        from control_plane.seat import _day
        write_json(seat.seat_path(self.root), {
            **seat.default_state(), 'handoff_day': _day(fixtures.NOW), 'handoff_count': 2})
        write_json(self.root / 'provider.json', {
            'ok': True, 'id': 'kimi_free', 'checked_at': fixtures.NOW})

        result = self.tick()

        self.assertEqual(result['actions'][0]['reason'], 'handoff_day_cap')
        self.assertFalse(seat.brief_path(self.root).exists())
        self.assertFalse(any('--dispatch' in command for command in self.sent))

    def test_corrupt_seat_blocks_planning_without_typing(self):
        fixtures.completed(self.store)
        path = seat.seat_path(self.root)
        path.parent.mkdir(parents=True)
        path.write_text('{')

        with self.assertRaises(ValueError):
            self.tick()

        self.assertFalse(any('--dispatch' in command for command in self.sent))
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'COMPLETED')

    def test_kimi_seat_plans_through_its_own_pin_without_codex_gates(self):
        fixtures.completed(self.store)
        write_json(seat.seat_path(self.root), {**seat.default_state(), 'owner': 'kimi'})
        write_json(self.root / 'provider.json', {'ok': False, 'id': 'openai', 'checked_at': 0})
        write_json(self.path, {'plans': [
            {'at': fixtures.NOW - 60 * n, 'provider': 'openai', 'critical': False}
            for n in (1, 2, 3)]})
        self.entry = {**self.entry, 'planner': {'terminal': 'term_kimi', 'harness': 'kimi'}}

        result = self.tick()

        self.assertEqual(result['actions'][0]['action'], 'plan')
        state = read_json(self.path)
        self.assertEqual(state['plans'][-1]['provider'], 'cline-free')
        self.assertEqual(state['workers']['minecraft']['planning']['timeout'], 300)
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'ASSIGNING')
        self.assertEqual(len([c for c in self.sent if '--dispatch' in c]), 1)

    def test_claude_seat_outlasts_one_anyrouter_edge_window(self):
        fixtures.completed(self.store)
        write_json(seat.seat_path(self.root), {**seat.default_state(), 'owner': 'claude'})
        self.entry = {**self.entry, 'planner': {'terminal': 'term_claude', 'harness': 'claude'}}

        self.assertEqual(self.tick()['actions'][0]['action'], 'plan')
        state = read_json(self.path)
        self.assertEqual(state['plans'][-1]['provider'], 'anyrouter')
        self.assertEqual(state['workers']['minecraft']['planning']['timeout'], 900)
        self.assertEqual(self.tick(now=fixtures.NOW + 301)['actions'][0]['action'], 'waiting')
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'ASSIGNING')
        timed_out = self.tick(now=fixtures.NOW + 901)['actions'][0]
        self.assertTrue(timed_out['reason'].startswith('assignment_timeout:'))
        self.assertEqual(len([c for c in self.sent if '--dispatch' in c]), 1)

    def test_planner_pin_must_match_the_seat_owner(self):
        fixtures.completed(self.store)
        cases = (
            ('claude', {'terminal': 'term_kimi', 'harness': 'kimi'}, 'seat_owned_by_claude'),
            ('codex', {'terminal': 'term_kimi', 'harness': 'kimi'}, 'planner_harness_mismatch'),
            ('codex', {'terminal': 'term_x', 'harness': 'gemini'}, 'planner_harness_mismatch'),
            ('kimi', {'terminal': 'term_astra'}, 'seat_owned_by_kimi'),
            ('codex', {'terminal': None, 'harness': 'codex'}, 'planner_terminal_missing'),
            ('kimi', {'terminal': None, 'harness': 'kimi'}, 'planner_terminal_missing'),
        )
        for owner, planner, reason in cases:
            with self.subTest(owner=owner, planner=planner):
                write_json(seat.seat_path(self.root), {**seat.default_state(), 'owner': owner})
                self.entry = {**self.entry, 'planner': planner}

                result = self.tick()

                self.assertEqual(result['actions'][0]['reason'], reason)
                self.assertFalse(any('--dispatch' in command for command in self.sent))
                self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'COMPLETED')


class AstraKimiGuardTest(unittest.TestCase):
    def test_committed_kimi_seat_blocks_astra_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / 'workers.json', {'workers': {
                'minecraft': {'cwd': '/work/minecraft', 'class': 'toplevel'}}})
            write_json(root / 'provider.json', {
                'ok': True, 'id': 'anyrouter', 'checked_at': fixtures.NOW})
            write_json(seat.seat_path(root), {
                **seat.default_state(), 'owner': 'kimi'})
            run = Mock(return_value={'ok': False})
            with patch('control_plane.astra.inventory.scan', return_value=[]):
                result = astra.launch(root / 'workers.json', root / 'modes',
                                      root / 'provider.json', now=fixtures.NOW,
                                      run=run)

            self.assertEqual(result['error'], 'seat_owned_by_kimi')
            run.assert_not_called()

    def test_kimi_health_cannot_launch_codex_as_astra(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / 'workers.json', {'workers': {
                'minecraft': {'cwd': '/work/minecraft', 'class': 'toplevel'}}})
            write_json(root / 'provider.json', {
                'ok': True, 'id': 'kimi_free', 'checked_at': fixtures.NOW})
            run = Mock(return_value={'ok': False})
            with patch('control_plane.astra.inventory.scan', return_value=[]):
                result = astra.launch(root / 'workers.json', root / 'modes',
                                      root / 'provider.json', now=fixtures.NOW,
                                      run=run)

            self.assertEqual(result['error'], 'kimi_handoff_required')
            run.assert_not_called()
            self.assertFalse((root / 'modes' / 'active-provider').exists())


class KimiProviderGateTest(unittest.TestCase):
    def test_uninstalled_kimi_cannot_preempt_plus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modes = root / 'modes'
            write_json(modes / 'providers.json', {'providers': [
                {'id': 'agentrouter'}, {'id': 'anyrouter'}]})
            dead = {'ok': False, 'configured': True, 'status': 503}
            with (patch.dict(os.environ, {}, clear=True),
                  patch.object(providers, 'probe', return_value=dead),
                  patch('control_plane.cli.cline_free.load') as usage,
                  patch('control_plane.cli.runtime.notify', return_value={'ok': True}) as notify,
                  redirect_stdout(io.StringIO()) as output):
                code = cli.provider_main(['--modes', str(modes), '--state', str(root / 'provider.json')])

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['id'], 'openai')
            usage.assert_not_called()
            notify.assert_called_once()
            self.assertIn('plus_fallback', notify.call_args.args[3])

    def test_explicit_kimi_enablement_allows_free_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modes = root / 'modes'
            write_json(modes / 'providers.json', {'providers': [
                {'id': 'agentrouter'}, {'id': 'anyrouter'}]})
            dead = {'ok': False, 'configured': True, 'status': 503}
            live_free = {'ok': True, 'configured': True, 'exhausted': False}
            with (patch.dict(os.environ, {'SPECTRE_KIMI_ENABLED': '1'}, clear=True),
                  patch.object(providers, 'probe', return_value=dead),
                  patch.object(providers, 'probe_kimi', return_value=live_free),
                  patch('control_plane.cli.cline_free.status', return_value=live_free),
                  patch('control_plane.cli.runtime.notify', return_value={'ok': True}) as notify,
                  redirect_stdout(io.StringIO()) as output):
                code = cli.provider_main(['--modes', str(modes), '--state', str(root / 'provider.json')])

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['id'], 'kimi_free')
            notify.assert_not_called()

    def test_enabled_kimi_with_failed_real_probe_falls_to_plus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modes = root / 'modes'
            write_json(modes / 'providers.json', {'providers': [
                {'id': 'agentrouter'}, {'id': 'anyrouter'}]})
            dead = {'ok': False, 'configured': True, 'status': 503}
            with (patch.dict(os.environ, {'SPECTRE_KIMI_ENABLED': '1'}, clear=True),
                  patch.object(providers, 'probe', return_value=dead),
                  patch.object(providers, 'probe_kimi', return_value={
                      'ok': False, 'configured': True, 'exhausted': True, 'status': 429}),
                  patch('control_plane.cli.cline_free.status', return_value={
                      'ok': True, 'configured': True, 'exhausted': False}),
                  patch('control_plane.cli.runtime.notify', return_value={'ok': True}) as notify,
                  redirect_stdout(io.StringIO()) as output):
                code = cli.provider_main(['--modes', str(modes), '--state', str(root / 'provider.json')])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['id'], 'openai')
            self.assertIn('plus_fallback', notify.call_args.args[3])


if __name__ == '__main__':
    unittest.main()
