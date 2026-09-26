"""Runtime handoff requests must not masquerade as a running Kimi seat."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import test_control_plane as fixtures
from control_plane import astra, cli, cline_free, providers, seat
from control_plane.io import locked, read_json, write_json


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

    def test_spent_kimi_free_quota_escalates_once_without_typing(self):
        fixtures.completed(self.store)
        write_json(seat.seat_path(self.root), {**seat.default_state(), 'owner': 'kimi'})
        self.entry = {**self.entry, 'planner': {'terminal': 'term_kimi', 'harness': 'kimi'}}
        cline_free.record(None, fixtures.NOW - 3600, 200)
        cline_free.record(None, fixtures.NOW - 60, 429)

        dry = self.tick(dry=True)['actions'][0]
        first = self.tick()['actions'][0]
        second = self.tick(now=fixtures.NOW + 30)['actions'][0]

        self.assertEqual((dry['action'], dry.get('reason')), ('escalate', 'kimi_free_exhausted'))
        self.assertEqual((first['action'], first['reason']), ('escalate', 'kimi_free_exhausted'))
        self.assertEqual((second['action'], second['reason']), ('sit', 'kimi_free_exhausted'))
        self.assertFalse(any('--dispatch' in command for command in self.sent))
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'COMPLETED')

        write_json(cline_free.DEFAULT_PATH, {
            'window_start': fixtures.NOW - cline_free.WINDOW_SEC - 10,
            'first_429_at': fixtures.NOW - cline_free.WINDOW_SEC})
        self.assertEqual(self.tick(now=fixtures.NOW + 60)['actions'][0]['action'], 'plan')
        self.assertEqual(len([c for c in self.sent if '--dispatch' in c]), 1)

    def test_claude_seat_plans_on_max_with_a_long_assignment_timeout(self):
        fixtures.completed(self.store)
        write_json(seat.seat_path(self.root), {**seat.default_state(), 'owner': 'claude'})
        self.entry = {**self.entry, 'planner': {'terminal': 'term_claude', 'harness': 'claude'}}

        self.assertEqual(self.tick()['actions'][0]['action'], 'plan')
        state = read_json(self.path)
        self.assertEqual(state['plans'][-1]['provider'], 'claude-max')
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

    def test_dry_run_reports_the_seat_verdict(self):
        fixtures.completed(self.store)
        cases = (
            ('codex', {'terminal': 'term_astra'}, 'plan', None),
            ('kimi', {'terminal': 'term_kimi', 'harness': 'kimi'}, 'plan', None),
            ('claude', {'terminal': 'term_kimi', 'harness': 'kimi'}, 'escalate', 'seat_owned_by_claude'),
            ('codex', {'terminal': 'term_kimi', 'harness': 'kimi'}, 'escalate', 'planner_harness_mismatch'),
            ('kimi', {'terminal': 'term_astra'}, 'escalate', 'seat_owned_by_kimi'),
            ('kimi', {'terminal': None, 'harness': 'kimi'}, 'escalate', 'planner_terminal_missing'),
        )
        for owner, planner, action, reason in cases:
            with self.subTest(owner=owner, planner=planner):
                write_json(seat.seat_path(self.root), {**seat.default_state(), 'owner': owner})
                self.entry = {**self.entry, 'planner': planner}

                verdict = self.tick(dry=True)['actions'][0]

                self.assertEqual((verdict['action'], verdict.get('reason')), (action, reason))
                self.assertEqual(self.sent, [])
                self.assertFalse(self.path.exists())


    def test_dry_run_reports_the_codex_gate_the_live_tick_escalates(self):
        fixtures.completed(self.store)
        fresh = {'ok': True, 'id': 'anyrouter', 'checked_at': fixtures.NOW}
        plus_plans = {'plans': [{'at': fixtures.NOW - 60 * n, 'provider': 'openai', 'critical': False}
                                for n in (1, 2, 3)]}
        cases = (
            ({**fresh, 'ok': False}, {}, {}, 'provider_health_stale'),
            ({**fresh, 'cooldown_until': fixtures.NOW + 60}, {}, {}, 'provider_cooldown'),
            ({**fresh, 'id': 'openai'}, plus_plans, {}, 'plus_normal_cap'),
            (fresh, {}, {'provider': 'agentrouter'}, 'provider_restart_required'),
            ({**fresh, 'id': 'kimi_free'}, {}, {}, 'kimi_handoff_required'),
        )
        for provider, loop_state, pin, reason in cases:
            with self.subTest(reason=reason):
                write_json(self.root / 'provider.json', provider)
                write_json(self.path, loop_state)
                self.entry = {**self.entry, 'planner': {'terminal': 'term_astra', **pin}}

                dry = self.tick(dry=True)['actions'][0]
                live = self.tick()['actions'][0]

                self.assertEqual((dry['action'], dry.get('reason')), ('escalate', reason))
                self.assertEqual(live.get('reason'), reason)
                self.assertFalse(any('--dispatch' in command for command in self.sent))


class LoopRegistryTest(unittest.TestCase):
    setUp = fixtures.LoopRuntimeTest.setUp
    tearDown = fixtures.LoopRuntimeTest.tearDown
    run_command = fixtures.LoopRuntimeTest.run_command
    tick = fixtures.LoopRuntimeTest.tick

    def test_tick_plans_with_the_pin_read_under_the_loop_lock(self):
        fixtures.completed(self.store)
        write_json(seat.seat_path(self.root), {**seat.default_state(), 'owner': 'kimi'})
        write_json(self.root / 'provider.json', {'ok': False, 'id': 'openai', 'checked_at': 0})
        confirmed = {**self.entry, 'planner': {'terminal': 'term_kimi', 'harness': 'kimi'}}

        def load():
            with self.assertRaises(BlockingIOError), locked(self.path.with_suffix('.lock')):
                pass
            return {'minecraft': confirmed}

        result = self.tick(load=load)

        self.assertEqual(result['actions'][0]['action'], 'plan')
        self.assertEqual(len([c for c in self.sent if '--dispatch' in c]), 1)

    def test_registry_read_under_the_loop_lock_is_validated(self):
        fixtures.completed(self.store)
        cases = (({}, 'worker registry is empty or unreadable'),
                 ({'flash': self.entry}, 'planner is legal only on minecraft'))
        for loaded, message in cases:
            with self.subTest(loaded=loaded):
                with self.assertRaisesRegex(ValueError, message):
                    self.tick(load=lambda: loaded)
                self.assertEqual(self.sent, [])

    def test_loop_main_hands_the_tick_a_registry_reader(self):
        workers_file = self.root / 'workers.json'
        write_json(workers_file, {'workers': {'minecraft': self.entry}})
        confirmed = {**self.entry, 'planner': {'terminal': 'term_kimi', 'harness': 'kimi'}}
        seen = []

        def tick(workers, client, path, env, *, dry, load):
            write_json(workers_file, {'workers': {'minecraft': confirmed}})
            seen.append((workers, load()))
            return {'ok': True, 'dry_run': dry, 'actions': []}

        with (patch.dict(os.environ, {'SPECTRE_LOOP': '1', 'SPECTRE_LOOP_STATE': str(self.path)}),
              patch.object(cli.runtime, 'tick', side_effect=tick),
              redirect_stdout(io.StringIO())):
            self.assertEqual(cli.loop_main(['--workers-file', str(workers_file)]), 0)

        self.assertEqual(seen, [({'minecraft': self.entry}, {'minecraft': confirmed})])

    def test_loop_main_prints_a_verdict_when_the_tick_crashes(self):
        fixtures.completed(self.store)
        workers_file = self.root / 'workers.json'
        write_json(workers_file, {'workers': {'minecraft': self.entry}})
        write_json(self.path, {'workers': []})

        with (patch.dict(os.environ, {**self.env, 'SPECTRE_LOOP_STATE': str(self.path)}),
              patch.object(cli, 'StateClient', return_value=self.client),
              redirect_stdout(io.StringIO()) as stdout,
              redirect_stderr(io.StringIO()) as stderr):
            code = cli.loop_main(['--workers-file', str(workers_file)])

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue()),
                         {'ok': False, 'error': "AttributeError: 'list' object has no attribute 'get'"})
        self.assertIn('Traceback', stderr.getvalue())
        with locked(self.path.with_suffix('.lock')):
            pass


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

    def test_planner_in_the_worktree_blocks_astra_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / 'workers.json', {'workers': {
                'minecraft': {'cwd': '/work/minecraft', 'class': 'toplevel'}}})
            write_json(root / 'provider.json', {
                'ok': True, 'id': 'anyrouter', 'checked_at': fixtures.NOW})
            cases = (
                ({'pid': 7, 'model': 'kimi', 'cwd': '/work/minecraft'}, 'top_level_agent_already_running', [7]),
                ({'pid': 8, 'model': 'claude', 'cwd': '/work/minecraft'}, 'top_level_agent_already_running', [8]),
                ({'pid': 9, 'model': 'claude', 'cwd': '/work/operator'}, 'codex_mode_failed', None),
                ({'pid': 10, 'model': 'efficient', 'cwd': '/work/minecraft'}, 'codex_mode_failed', None),
            )
            for process, error, pids in cases:
                with self.subTest(process=process):
                    run = Mock(return_value={'ok': False})
                    with patch('control_plane.astra.inventory.scan', return_value=[process]):
                        result = astra.launch(root / 'workers.json', root / 'modes',
                                              root / 'provider.json', now=fixtures.NOW,
                                              run=run)

                    self.assertEqual((result['error'], result.get('pids')), (error, pids))
                    self.assertEqual(run.call_count, 0 if pids else 1)

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
