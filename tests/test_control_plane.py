"""Runtime contracts for the previously unwired control-plane paths."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_plane import packets, runtime, providers, inventory
from control_plane.io import read_json, write_json
from worker_state.store import Store
from worker_state.types import iso_from

NOW = 1_800_000_000.0


class StoreClient:
    def __init__(self, store, now=NOW):
        self.store, self.now = store, now

    def snapshot(self, worker):
        return self.store.snapshot(worker, self.now, rebuild=False)

    def claim(self, body):
        return 200, self.store.claim(body['worker'], body['action'],
            body['expected_snapshot_version'], body['idempotency_key'], self.now,
            {key: value for key, value in body.items() if key in {'target', 'terminal'}})

    def result(self, ident, ok, error=None):
        return 200, self.store.action_result(ident, ok, self.now, error)

    def evidence(self, body):
        return 200, self.store.ingest(body, self.now)


def completed(store, worker='minecraft', goal='g1', now=NOW):
    return store.ingest({'event_id': f'done-{goal}', 'worker': worker,
        'kind': 'goal.completed', 'source': 'qoder_jsonl', 'goal_id': goal,
        'attempt_id': 1, 'source_timestamp': iso_from(now), 'payload': {}}, now)


def packet(ident='p1', **over):
    return {'id': ident, 'assignee': 'flash', 'kind': 'implement',
        'goal': 'Fix the parser', 'acceptance': ['Parser tests pass'],
        'requires_astra_review': False, **over}


class InputTest(unittest.TestCase):
    def test_next_goal_requires_valid_json_and_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'next_goal.json'
            for value in ('not JSON', '{"goal":42}', '{"goal":"x","blocked":true}',
                          json.dumps({'goal': 'x' * 601})):
                path.write_text(value)
                self.assertIsNone(packets.next_goal(tmp))
            path.write_text('{"goal":"Fix parser"}')
            self.assertEqual(packets.next_goal(tmp), 'Fix parser')

    def test_packet_schema_and_request_binding(self):
        value = {'request_id': 'r1', 'worker': 'minecraft',
                 'wake_reason': 'completed', 'packets': [packet()]}
        self.assertEqual(packets.validate(value, 'r1'), [packet()])
        for changed in ({'request_id': '../r1'}, {'worker': 'qoder'},
                        {'packets': [packet(assignee='efficient')]},
                        {'packets': [packet(assignee='mimo', kind='mechanical')]},
                        {'packets': [packet(), packet()]}, {'packets': []}):
            with self.assertRaises(ValueError):
                packets.validate({**value, **changed}, 'r1')

    def test_state_private_and_corruption_is_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            write_json(path, {'x': 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text('{')
            with self.assertRaises(ValueError):
                read_json(path)


class LoopRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state.sqlite')
        self.client = StoreClient(self.store)
        self.path = self.root / 'loop.json'
        self.entry = {'cwd': str(self.root), 'planner': {'terminal': 'term_astra'}}
        self.env = {'SPECTRE_LOOP': '1', 'ASTRA_ENABLED': '1',
                    'SPECTRE_PACKET_DIR': str(self.root / 'packets'),
                    'SPECTRE_PROVIDER_STATE': str(self.root / 'provider.json')}
        (self.root / 'packets').mkdir()
        write_json(self.root / 'provider.json', {'ok': True, 'id': 'anyrouter', 'checked_at': NOW})
        self.sent = []

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_command(self, argv, **kwargs):
        self.sent.append(argv)
        return {'ok': True, 'parsed': {'ok': True, 'evt': 'dispatch_sent'}}

    def tick(self, workers=None, now=NOW, dry=False):
        self.client.now = now
        return runtime.tick(workers or {'minecraft': self.entry}, self.client,
            self.path, self.env, now=now, dry=dry, run=self.run_command)

    def test_plan_once_then_stable_packets_then_single_dispatch(self):
        completed(self.store)
        self.tick()
        state = read_json(self.path)
        pending = state['workers']['minecraft']['planning']
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'ASSIGNING')
        self.assertEqual(len(self.sent), 1)
        self.assertIn('--request-id', self.sent[0])
        result = Path(self.env['SPECTRE_PACKET_DIR']) / f"{pending['request_id']}.json"
        result.write_text(json.dumps({'request_id': pending['request_id'],
            'worker': 'minecraft', 'wake_reason': 'completed',
            'packets': [packet('p1', assignee='efficient', kind='mechanical'),
                        packet('p2', goal='Fix another parser')]}))
        self.tick(now=NOW + 30)
        self.assertEqual(len(self.sent), 1)
        self.tick(now=NOW + 60)
        self.assertEqual(len(self.sent), 2)
        self.assertIn('goal', self.sent[-1])
        self.tick(now=NOW + 90)
        self.assertEqual(len(self.sent), 2, 'stale snapshot must not replay a sent packet')

    def test_dry_run_does_not_claim_write_or_send(self):
        completed(self.store)
        version = self.client.snapshot('minecraft')['snapshot_version']
        self.tick(dry=True)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.client.snapshot('minecraft')['snapshot_version'], version)

    def test_efficient_close_advance_repeat_guard(self):
        completed(self.store, 'qoder')
        (self.root / 'next_goal.json').write_text('{"goal":"Fix parser"}')
        workers = {'qoder': {'cwd': str(self.root)}}
        self.tick(workers)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.client.snapshot('qoder')['policy']['grokbot_may_advance'])
        completed(self.store, 'qoder', 'g2', NOW + 120)
        self.tick(workers, NOW + 120)
        self.assertEqual(len([c for c in self.sent if '--dispatch' in c]), 1)

    def test_failed_notification_is_retried(self):
        completed(self.store, 'qoder')
        workers = {'qoder': {'cwd': str(self.root)}}
        self.run_command = lambda *args, **kw: {'ok': False, 'error': 'offline'}
        self.tick(workers)
        state = read_json(self.path)
        self.assertFalse(state['workers']['qoder'].get('notified'))
        self.run_command = lambda argv, **kw: self.sent.append(argv) or {'ok': True}
        self.tick(workers, NOW + 60)
        self.assertEqual(len(self.sent), 1)
        self.tick(workers, NOW + 120)
        self.assertEqual(len(self.sent), 1)

    def test_assignment_timeout_reopens_completion_and_escalates(self):
        completed(self.store)
        self.tick()
        self.tick(now=NOW + 301)
        snap = self.client.snapshot('minecraft')
        self.assertEqual(snap['goal']['state'], 'COMPLETED')
        self.assertTrue(snap['policy']['grokbot_may_advance'])
        self.assertEqual(len([c for c in self.sent if 'plan' in c]), 1)
        self.assertTrue(any('--channel' in c for c in self.sent))

    def test_planner_for_other_worker_is_rejected(self):
        with self.assertRaises(ValueError):
            self.tick({'qoder': self.entry})


class ProviderTest(unittest.TestCase):
    def test_agentrouter_preferred_and_probed_first(self):
        calls = []
        registry = {'providers': [{'id': name} for name in providers.RELAYS]}
        def probe(row):
            calls.append(row['id'])
            return {'ok': row['id'] == 'agentrouter', 'status': 200}
        result = providers.choose(registry, {}, NOW, probe)
        self.assertEqual(result['id'], 'agentrouter')
        # Both relays are probed for health; rank prefers agentrouter when alive.
        self.assertEqual(calls, ['agentrouter', 'anyrouter'])
        self.assertEqual(calls[0], 'agentrouter')

    def test_anyrouter_is_fallback_when_agentrouter_down(self):
        calls = []
        registry = {'providers': [{'id': name} for name in providers.RELAYS]}
        def probe(row):
            calls.append(row['id'])
            return {'ok': row['id'] == 'anyrouter', 'status': 200}
        result = providers.choose(registry, {}, NOW, probe)
        self.assertEqual(result['id'], 'anyrouter')
        self.assertEqual(calls, ['agentrouter', 'anyrouter'])

    def test_kimi_free_outranks_plus_and_plus_needs_kimi_exhausted(self):
        registry = {'providers': [{'id': name} for name in providers.RELAYS]}
        dead = lambda row: {'ok': False, 'configured': True, 'status': None,
                            'upstream_quota': False}
        kimi_live = lambda: {'ok': True, 'configured': True, 'exhausted': False}
        kimi_out = lambda: {'ok': False, 'configured': True, 'exhausted': True}
        picked = providers.choose(registry, {}, NOW, dead, kimi_fn=kimi_live)
        self.assertEqual(picked['id'], providers.KIMI_FREE)
        plus = providers.choose(registry, {}, NOW, dead, kimi_fn=kimi_out)
        self.assertEqual(plus['id'], providers.LAST_RESORT)

    def test_plus_cooldown_is_retained(self):
        result = providers.choose({'providers': []}, {'cooldown_until': NOW + 60}, NOW,
                                  lambda row: {'ok': False})
        self.assertFalse(result['ok'])
        self.assertEqual(result['cooldown_until'], NOW + 60)

    def test_402_is_upstream_quota_not_dead_account(self):
        hit = {'ok': False, 'configured': True, 'status': 402, 'upstream_quota': True}
        self.assertEqual(providers.upstream_retry_at(hit, NOW), NOW + providers.UPSTREAM_QUOTA_BACKOFF)
        self.assertEqual(providers.upstream_retry_at({'status': 500}, NOW), NOW)

    def test_no_heavy_model_probe_and_one_token_body(self):
        with self.assertRaises(ValueError):
            providers.probe_request({'base_url': 'https://relay.example/v1',
                                     'wire_api': 'responses', 'probe_model': 'gpt-6-astra'})
        url, body = providers.probe_request({'base_url': 'https://relay.example/v1',
                                            'wire_api': 'responses', 'probe_model': 'cheap'})
        self.assertEqual(url, 'https://relay.example/v1/responses')
        self.assertEqual(body['max_output_tokens'], 1)
        self.assertFalse(body['store'])


class InventoryTest(unittest.TestCase):
    def test_model_uses_argv_not_title_or_prompt(self):
        self.assertEqual(inventory.model(['qodercli', '-m', 'Efficient']), 'efficient')
        self.assertEqual(inventory.model(['codex', '--model=gpt-6-astra']), 'astra')
        self.assertIsNone(inventory.model(['bash', '-c', 'codex -m gpt-6-astra']))
        self.assertIsNone(inventory.model(['qodercli', '-m', 'Auto', 'Efficient']))
        self.assertEqual(inventory.model(['dsh', '--profile', 'headless']), 'flash')
        self.assertEqual(inventory.model(['dsh-clinepass', '--file', 'x.txt']), 'flash')
        self.assertEqual(inventory.model(['mimo', 'run', 'task']), 'mimo')
        self.assertEqual(inventory.model(['mimo-clinepass', '--file', 'x.txt']), 'mimo')


if __name__ == '__main__':
    unittest.main()

class AdditionalLoopTest(unittest.TestCase):
    setUp = LoopRuntimeTest.setUp
    tearDown = LoopRuntimeTest.tearDown
    run_command = LoopRuntimeTest.run_command
    tick = LoopRuntimeTest.tick

    def test_failed_dispatch_return_is_not_success(self):
        completed(self.store, 'qoder')
        (self.root / 'next_goal.json').write_text('{"goal":"Fix parser"}')
        self.run_command = lambda *args, **kwargs: {'ok': False, 'uncertain': True, 'error': 'timeout'}
        result = self.tick({'qoder': {'cwd': str(self.root)}})
        self.assertFalse(result['ok'])
        self.assertEqual(read_json(self.path)['workers']['qoder']['active']['status'], 'uncertain')

    def test_planner_off_never_reads_next_goal(self):
        completed(self.store)
        (self.root / 'next_goal.json').write_text('{"goal":"Wrong target"}')
        self.env = {**self.env, 'ASTRA_ENABLED': '0'}
        out = self.tick()
        self.assertEqual(out['actions'][0]['reason'], 'planner_pin')
        self.assertEqual(self.sent, [])

    def test_caps_and_fingerprint_are_immutable(self):
        from control_plane import budget
        initial = {'workers': {}}
        first = budget.reserve(initial, 'qoder', 'goal', NOW)
        self.assertEqual(initial, {'workers': {}})
        self.assertEqual(budget.refusal(first, 'qoder', 'goal', NOW + 120), 'repeated_goal')
        self.assertEqual(budget.refusal(first, 'qoder', 'other', NOW + 59), 'dispatch_spacing')
        full = {**first, 'dispatches': [{'worker': 'qoder', 'at': NOW - 100}] * 96}
        self.assertEqual(budget.refusal(full, 'qoder', 'other', NOW), 'worker_daily_cap')
        global_full = {**first, 'dispatches': [{'worker': 'other', 'at': NOW - 100}] * 256}
        self.assertEqual(budget.refusal(global_full, 'qoder', 'other', NOW), 'global_daily_cap')

    def test_plus_budget_and_stale_health(self):
        from control_plane import budget
        provider = {'ok': True, 'id': 'openai', 'checked_at': NOW}
        state = {'plans': [{'at': NOW, 'provider': 'openai', 'critical': False}] * 3}
        self.assertEqual(budget.planner_refusal(state, provider, NOW), 'plus_normal_cap')
        self.assertIsNone(budget.planner_refusal(state, provider, NOW, critical=True))
        self.assertEqual(budget.planner_refusal(state, provider, NOW + 601), 'provider_health_stale')

class RecoveryTest(unittest.TestCase):
    setUp = LoopRuntimeTest.setUp
    tearDown = LoopRuntimeTest.tearDown
    run_command = LoopRuntimeTest.run_command
    tick = LoopRuntimeTest.tick

    def test_parked_goal_escalates_without_claiming_or_typing(self):
        for reason in ('plan_gate', 'goal_budget'):
            self.store.ingest({'event_id': reason, 'worker': 'minecraft',
                'kind': 'goal.parked', 'source': 'qoder_jsonl', 'goal_id': reason,
                'attempt_id': 1, 'payload': {'park_reason': reason}}, NOW)
            self.tick()
            self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'PARKED')
        self.assertEqual(len(self.sent), 2)
        self.assertFalse(any('--dispatch' in command for command in self.sent))

    def test_crash_between_advance_and_assignment_start_recovers(self):
        from control_plane import planning
        completed(self.store)
        snapshot = self.client.snapshot('minecraft')
        pending = planning.request(snapshot, NOW)
        claim = planning.advance(self.client, 'minecraft', snapshot, pending['request_id'])
        write_json(self.path, {'workers': {'minecraft': {'planning': {
            **pending, 'action_id': claim['action_id'], 'status': 'sending'}}}})
        self.tick(now=NOW + 30)
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'ASSIGNING')
        self.tick(now=NOW + 301)
        self.assertTrue(self.client.snapshot('minecraft')['policy']['grokbot_may_advance'])
        self.assertFalse(any('plan' in command for command in self.sent))

    def test_crash_after_packet_advance_recovers_prepared_intent(self):
        from control_plane import planning, budget
        completed(self.store, 'qoder')
        snapshot = self.client.snapshot('qoder')
        item = packet(assignee='efficient', kind='mechanical')
        active = {'origin': planning.identity(snapshot), 'packet': item,
                  'status': 'prepared', 'request_id': 'prepared-goal'}
        state = budget.reserve({}, 'qoder', item['goal'], NOW)
        state = runtime.worker_state(state, 'qoder', {**state['workers']['qoder'], 'active': active})
        write_json(self.path, state)
        planning.advance(self.client, 'qoder', snapshot, active['request_id'])
        self.tick({'qoder': {'cwd': str(self.root)}}, NOW + 30)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(read_json(self.path)['workers']['qoder']['active']['status'], 'sent')

    def test_missing_goal_intent_survives_advance_failure(self):
        completed(self.store, 'qoder')
        workers = {'qoder': {'cwd': str(self.root)}}
        with patch.object(self.client, 'result', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.tick(workers)
        self.assertTrue(read_json(self.path)['workers']['qoder']['awaiting_next'])
        self.tick(workers, NOW + 30)
        self.assertEqual(len(self.sent), 1)

    def test_unavailable_flash_notice_is_retried_without_next_goal(self):
        completed(self.store)
        write_json(self.path, {'workers': {'minecraft': {'queue': [packet()]}}})
        self.run_command = lambda argv, **kwargs: {'ok': False, 'parsed': {
            'evt': 'dispatch_flash_unavailable' if '--dispatch' in argv else 'notify_failed'}}
        self.tick()
        self.run_command = LoopRuntimeTest.run_command.__get__(self)
        self.tick(now=NOW + 30)
        self.assertTrue(any('flash_unavailable:p1' in ' '.join(command) for command in self.sent))

    def test_non_mechanical_flash_unavailable_keeps_packet_and_runs_rest(self):
        completed(self.store)
        head = packet('p1')
        tail = packet('p2', assignee='efficient', kind='mechanical', goal='Rename a symbol')
        write_json(self.path, {'workers': {'minecraft': {'queue': [head, tail]}}})
        flash_targets = []
        def flash_down(argv, **kwargs):
            self.sent.append(argv)
            if argv[:2] == ['spectre-slack-bridge', '--dispatch'] and 'goal' in argv:
                target = argv[argv.index('--target') + 1]
                flash_targets.append(target)
                if target == 'flash':
                    return {'ok': False, 'parsed': {'evt': 'dispatch_flash_unavailable'}}
            return {'ok': True, 'parsed': {'ok': True, 'evt': 'dispatch_sent'}}
        self.run_command = flash_down
        self.tick()
        state = read_json(self.path)['workers']['minecraft']
        self.assertIsNone(state.get('last_fingerprint'))
        self.assertEqual([row['id'] for row in state['queue']], ['p2', 'p1'])
        self.assertEqual(state['queue'][1]['assignee'], 'flash')
        self.tick(now=NOW + 120)
        state = read_json(self.path)['workers']['minecraft']
        self.assertEqual([row['id'] for row in state['queue']], ['p1'])
        self.assertEqual(flash_targets, ['flash', 'efficient'])
        self.assertIn('Rename a symbol', self.sent[-1][4])
        self.assertNotIn('Fix the parser', self.sent[-1][4])

    def test_mimo_unavailable_keeps_packet_and_escalates(self):
        completed(self.store)
        head = packet('p1', assignee='mimo', kind='review')
        tail = packet('p2', assignee='efficient', kind='mechanical', goal='Rename a symbol')
        write_json(self.path, {'workers': {'minecraft': {'queue': [head, tail]}}})
        targets = []
        def mimo_down(argv, **kwargs):
            self.sent.append(argv)
            if argv[:2] == ['spectre-slack-bridge', '--dispatch'] and 'goal' in argv:
                target = argv[argv.index('--target') + 1]
                targets.append(target)
                if target == 'mimo':
                    return {'ok': False, 'parsed': {'evt': 'dispatch_mimo_unavailable'}}
            return {'ok': True, 'parsed': {'ok': True, 'evt': 'dispatch_sent'}}
        self.run_command = mimo_down
        self.tick()
        state = read_json(self.path)['workers']['minecraft']
        self.assertEqual([row['id'] for row in state['queue']], ['p2', 'p1'])
        self.assertEqual(state['queue'][1]['assignee'], 'mimo')
        self.assertFalse(any('efficient' == t and 'Fix the parser' in
                             (self.sent[i][4] if len(self.sent[i]) > 4 else '')
                             for i, t in enumerate(targets)))
        self.tick(now=NOW + 120)
        self.assertEqual(targets, ['mimo', 'efficient'])

    def test_uncertain_send_error_is_never_blindly_retried(self):
        completed(self.store, 'qoder')
        (self.root / 'next_goal.json').write_text('{"goal":"Fix parser"}')
        calls = []
        def failed(argv, **kwargs):
            calls.append(argv)
            return {'ok': False, 'parsed': {'evt': 'dispatch_send_failed'}}
        self.run_command = failed
        self.tick({'qoder': {'cwd': str(self.root)}})
        self.tick({'qoder': {'cwd': str(self.root)}}, NOW + 120)
        self.assertEqual(len(calls), 1)

    def test_crash_after_send_does_not_replay_queue_head(self):
        completed(self.store)
        head = packet('p1')
        tail = packet('p2', goal='Fix another parser')
        write_json(self.path, {'workers': {'minecraft': {'queue': [head, tail],
            'active': {'origin': 'old:1', 'packet': head, 'status': 'sending'}}}})
        self.tick(now=NOW + 120)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('Fix another parser', self.sent[0][4])

    def test_failed_worker_opens_one_planner_advance(self):
        self.store.ingest({'event_id': 'failed', 'worker': 'minecraft', 'kind': 'goal.failed',
            'source': 'dsh_exit', 'goal_id': 'g1', 'attempt_id': 1, 'payload': {}}, NOW)
        self.assertTrue(self.client.snapshot('minecraft')['policy']['grokbot_may_advance'])
        self.tick()
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'ASSIGNING')
        self.assertEqual(len(self.sent), 1)

    def test_planning_left_from_old_epoch_cannot_finish_over_new_work(self):
        completed(self.store)
        self.tick()
        self.store.ingest({'event_id': 'operator', 'worker': 'minecraft', 'kind': 'input.goal',
            'source': 'api', 'goal_id': 'g2', 'attempt_id': 1, 'payload': {}}, NOW + 1)
        self.tick(now=NOW + 30)
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'INJECTED')
        self.assertIsNone(read_json(self.path)['workers']['minecraft']['planning'])
