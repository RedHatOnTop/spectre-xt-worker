"""The Claude seat is operator-driven, visible, and committed only after observation."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests import test_control_plane as fixtures
from control_plane import claude, runtime, seat
from control_plane.io import read_json, write_json


class Client:
    def __init__(self, state='COMPLETED'):
        self.value = {'snapshot_version': 7, 'goal': {'state': state, 'goal_id': 'goal-7'},
                      'policy': {}}

    def snapshot(self, worker):
        return self.value


class ClaudeHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cwd = self.root / 'minecraft'
        self.cwd.mkdir()
        self.client = Client()
        self.workers = self.root / 'workers.json'
        self.provider = self.root / 'provider.json'
        self.loop = self.root / 'loop.json'
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        self.binary = bin_dir / 'claude'
        self.binary.write_text('#!/bin/sh\n')
        self.binary.chmod(0o755)
        self.env = {'SPECTRE_CLAUDE_ENABLED': '1', 'PATH': str(bin_dir)}
        write_json(self.workers, {'workers': {'minecraft': {
            'cwd': str(self.cwd), 'class': 'toplevel', 'planner': {'terminal': 'term_astra'}}}})
        write_json(self.provider, {'ok': True, 'id': 'anyrouter', 'checked_at': fixtures.NOW})
        self.calls = []
        self.processes = [{'model': 'efficient', 'cwd': str(self.cwd), 'pid': 11,
                           'handle': 'term_efficient'}]

    def tearDown(self):
        self.temp.cleanup()

    def fake_run(self, command, **kwargs):
        self.calls.append((command, kwargs.get('cwd')))
        action = command[2]
        if action == 'ps':
            return {'ok': True, 'parsed': {'result': {'worktrees': [
                {'worktreeId': f'wt::{self.cwd}', 'path': str(self.cwd)}]}}}
        if action == 'list':
            terminals = [{'handle': handle, 'worktreePath': str(self.cwd),
                          'connected': True, 'writable': True}
                         for handle in ('term_efficient', 'term_claude')]
            return {'ok': True, 'parsed': {'result': {'terminals': terminals}}}
        if action == 'create':
            return {'ok': True, 'parsed': {'result': {'terminal': {
                'handle': 'term_claude', 'worktreeId': f'wt::{self.cwd}'}}}}
        if action == 'send':
            return {'ok': True, 'parsed': {'ok': True}}
        raise AssertionError(command)

    def step(self, name, *args, now=fixtures.NOW, **kwargs):
        return getattr(claude, name)(self.workers, self.loop, self.root, self.client, *args,
                                     now=now, env=self.env, run=self.fake_run,
                                     scan=lambda: self.processes, **kwargs)

    def running(self):
        self.processes = [*self.processes, {'model': 'claude', 'cwd': str(self.cwd), 'pid': 12,
                                            'handle': 'term_claude'}]

    def pending(self):
        return read_json(self.loop)['workers']['minecraft']['claude_handoff']

    def test_launch_prompt_confirm_moves_seat_and_pin_only_after_observation(self):
        launched = self.step('launch')
        self.assertEqual((launched['terminal'], launched['next']),
                         ('term_claude', 'send_prompt_after_observing_input'))
        create, cwd = next(call for call in self.calls if call[0][2] == 'create')
        self.assertEqual(create[3:7], ['--worktree', 'active', '--title', 'claude-planner'])
        self.assertEqual(create[8], f'exec {self.binary} --model claude-opus-5-5 '
                                    '--dangerously-skip-permissions')
        self.assertEqual(cwd, str(self.cwd))
        self.assertEqual(self.pending()['terminal'], 'term_claude')
        self.assertIn('Goal ID: goal-7', seat.brief_path(self.root).read_text())
        self.assertEqual(seat.load(seat.seat_path(self.root))['owner'], 'codex')

        with self.assertRaisesRegex(ValueError, 'observed-input'):
            self.step('send_prompt', 'term_claude')
        self.assertEqual(self.step('send_prompt', 'term_claude', observed_input=True)['error'],
                         'claude_terminal_unconfirmed')
        self.running()
        sent = self.step('send_prompt', 'term_claude', observed_input=True, now=fixtures.NOW + 1)
        self.assertTrue(sent['ok'])
        typed = self.calls[-1][0]
        self.assertEqual(typed[:5], ['orca-ide', 'terminal', 'send', '--terminal', 'term_claude'])
        self.assertIn(str(seat.brief_path(self.root)), typed[6])
        self.assertEqual(self.step('send_prompt', 'term_claude', observed_input=True)['error'],
                         'claude_prompt_already_sent')

        with self.assertRaisesRegex(ValueError, 'observed-ready'):
            self.step('confirm', 'term_claude')
        confirmed = self.step('confirm', 'term_claude', observed_ready=True, now=fixtures.NOW + 2)
        self.assertEqual(confirmed, {'ok': True, 'owner': 'claude', 'terminal': 'term_claude'})
        current = seat.load(seat.seat_path(self.root))
        self.assertEqual((current['owner'], current['terminal'], current['provider_chain']),
                         ('claude', 'term_claude', ['claude-max']))
        self.assertEqual(read_json(self.workers)['workers']['minecraft']['planner'],
                         {'terminal': 'term_claude', 'harness': 'claude',
                          'model': 'claude-opus-5-5', 'provider': 'claude-max'})
        self.assertIsNone(self.pending())

    def test_dry_run_creates_nothing(self):
        result = self.step('launch', dry=True)
        self.assertEqual(result['command'], f'exec {self.binary} --model claude-opus-5-5 '
                                            '--dangerously-skip-permissions')
        self.assertFalse(any(call[0][2] == 'create' for call in self.calls))
        self.assertFalse(self.loop.exists())
        self.assertFalse(seat.brief_path(self.root).exists())

    def test_launch_refusals_create_no_terminal(self):
        def planner_running():
            self.processes = [*self.processes, {'model': 'kimi', 'cwd': str(self.cwd),
                                                'pid': 13, 'handle': 'term_k'}]

        cases = (
            ('planner in the worktree', planner_running, 'top_level_agent_already_running'),
            ('no efficient pin', lambda: setattr(self, 'processes', []),
             'efficient_pin_required_before_second_terminal'),
        )
        for name, arrange, error in cases:
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                arrange()
                self.assertEqual(self.step('launch')['error'], error)
                self.assertFalse(any(call[0][2] == 'create' for call in self.calls))

    def test_launch_preconditions_fail_closed(self):
        cases = (
            ('gate off', lambda: self.env.pop('SPECTRE_CLAUDE_ENABLED'), 'SPECTRE_CLAUDE_ENABLED is off'),
            ('no binary', lambda: self.binary.unlink(), 'Claude Code executable is not on PATH'),
            ('plan in flight', lambda: write_json(self.loop, {'workers': {'minecraft': {
                'planning': {'request_id': 'r1'}}}}), 'planner_request_in_flight'),
            ('assigning', lambda: setattr(self, 'client', Client('ASSIGNING')), 'planner_request_in_flight'),
            ('already claude', lambda: write_json(seat.seat_path(self.root), {
                **seat.default_state(), 'owner': 'claude'}), 'seat_owned_by_claude'),
            ('day cap', lambda: write_json(seat.seat_path(self.root), {
                **seat.default_state(), 'handoff_day': seat._day(fixtures.NOW),
                'handoff_count': seat.MAX_HANDOFFS_PER_DAY}), 'handoff_day_cap'),
        )
        for name, arrange, error in cases:
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                arrange()
                with self.assertRaisesRegex(ValueError, error):
                    self.step('launch')
                self.assertFalse(any(call[0][2] == 'create' for call in self.calls))

    def test_duplicate_launch_does_not_create_another_terminal(self):
        self.assertTrue(self.step('launch')['ok'])
        self.assertEqual(self.step('launch')['error'], 'claude_terminal_already_created')
        self.assertEqual(sum(call[0][2] == 'create' for call in self.calls), 1)

    def test_loop_does_not_plan_while_the_handoff_is_pending(self):
        write_json(self.loop, {'workers': {'minecraft': {'claude_handoff': {
            'terminal': 'term_claude', 'launched_at': fixtures.NOW - 60}}}})
        state = read_json(self.loop)
        snapshot = {'goal': {'state': 'COMPLETED'}, 'policy': {'grokbot_may_advance': True}}
        entry = read_json(self.workers)['workers']['minecraft']
        _, action = runtime.start_plan('minecraft', entry, snapshot, state, self.loop,
                                       self.client, {}, fixtures.NOW, self.fake_run)
        self.assertEqual(action, {'worker': 'minecraft', 'action': 'skip',
                                  'reason': 'claude_handoff_pending'})
        dry = runtime.dry_action('minecraft', entry, snapshot, {'ASTRA_ENABLED': '1'},
                                 self.loop, fixtures.NOW)
        self.assertEqual(dry['reason'], 'claude_handoff_pending')
        self.assertEqual(self.calls, [])

    def test_an_abandoned_handoff_escalates_once(self):
        write_json(self.loop, {'workers': {'minecraft': {'claude_handoff': {
            'terminal': 'term_claude',
            'launched_at': fixtures.NOW - runtime.CLAUDE_HANDOFF_STALE - 1}}}})
        snapshot = {'goal': {'state': 'COMPLETED'}, 'policy': {'grokbot_may_advance': True}}
        entry = read_json(self.workers)['workers']['minecraft']
        sent = []

        def notify(argv, **_kwargs):
            sent.append(argv)
            return {'ok': True}

        state, first = runtime.start_plan('minecraft', entry, snapshot, read_json(self.loop),
                                          self.loop, self.client, {}, fixtures.NOW, notify)
        _, second = runtime.start_plan('minecraft', entry, snapshot, state, self.loop,
                                       self.client, {}, fixtures.NOW + 30, notify)
        dry = runtime.dry_action('minecraft', entry, snapshot, {'ASTRA_ENABLED': '1'},
                                 self.loop, fixtures.NOW)
        self.assertEqual((first['action'], first['reason']), ('escalate', 'claude_handoff_stale'))
        self.assertEqual((second['action'], second['reason']), ('sit', 'claude_handoff_stale'))
        self.assertEqual((dry['action'], dry['reason']), ('escalate', 'claude_handoff_stale'))
        self.assertEqual(len(sent), 1)
        self.assertFalse(any('--dispatch' in argv for argv in sent))

    def test_cancel_drops_an_abandoned_handoff_once_claude_is_gone(self):
        with self.assertRaisesRegex(ValueError, 'observed-closed'):
            claude.cancel(self.loop)
        self.assertEqual(claude.cancel(self.loop, observed_closed=True, scan=lambda: [])['error'],
                         'no_pending_claude_handoff')
        self.step('launch')
        self.running()
        still = claude.cancel(self.loop, observed_closed=True, scan=lambda: self.processes)
        self.assertEqual((still['error'], still['pids']), ('claude_terminal_still_running', [12]))
        self.assertEqual(self.pending()['terminal'], 'term_claude')
        self.processes = self.processes[:1]
        self.assertEqual(claude.cancel(self.loop, observed_closed=True, scan=lambda: self.processes),
                         {'ok': True, 'cancelled': 'term_claude'})
        self.assertIsNone(self.pending())
        self.assertEqual(seat.load(seat.seat_path(self.root))['owner'], 'codex')
        self.assertEqual(read_json(self.workers)['workers']['minecraft']['planner'],
                         {'terminal': 'term_astra'})

    def test_release_returns_a_stopped_claude_seat_to_codex(self):
        self.step('launch')
        self.running()
        self.step('send_prompt', 'term_claude', observed_input=True, now=fixtures.NOW + 1)
        self.step('confirm', 'term_claude', observed_ready=True, now=fixtures.NOW + 2)
        release = dict(observed_stopped=True, now=fixtures.NOW + 3, scan=lambda: self.processes)
        self.assertEqual(claude.release(self.workers, self.provider, self.loop, self.root,
                                        **release)['error'], 'top_level_agent_still_running')
        self.processes = self.processes[:1]
        result = claude.release(self.workers, self.provider, self.loop, self.root, **release)
        self.assertEqual(result, {'ok': True, 'owner': 'codex', 'next': 'launch_astra'})
        self.assertEqual(read_json(self.workers)['workers']['minecraft']['planner'],
                         {'terminal': None, 'harness': 'codex', 'model': 'gpt-6-astra'})
        with self.assertRaisesRegex(ValueError, 'seat_owned_by_codex'):
            claude.release(self.workers, self.provider, self.loop, self.root, **release)


if __name__ == '__main__':
    unittest.main()
