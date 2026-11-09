"""Route admission, resume, SOS, and transcript profiles on the continuity journal."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from superagent_queue import driver, selection
from superagent_queue.store import Journal

TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': 'Resume only existing authorized work.'}
ZERO = {'prompt': 0, 'completion': 0, 'request': 0}
MANDATE = {'work_id': 'work_1', 'run_id': 'run_1', 'authorized': True}
CLAIM_CHILD = '''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from superagent_queue.store import Journal
journal = Journal(sys.argv[2])
claim = journal.claim('continuity-driver', now=100)
if claim is None:
    raise SystemExit(2)
Path(sys.argv[3]).write_text(claim.token)
time.sleep(30)
'''


def route(**overrides):
    body = {'route_id': 'zen_1', 'harness': 'fixture-harness', 'adapter': 'registered', 'family': 'zen',
            'model': 'zen/flash', 'role': 'worker', 'price': dict(ZERO), 'expires_at': None,
            'usefulness': 0.9, 'remaining': 10, 'approved_paid': False, 'malformed': False, 'exhausted': False}
    return {**body, **overrides}


def budgets(agent=10, dsh=10, agent_exhausted=False, dsh_exhausted=False):
    return {'agentrouter': {'remaining': agent, 'limit': 100, 'exhausted': agent_exhausted},
            'dsh': {'remaining': dsh, 'limit': 100, 'exhausted': dsh_exhausted}}


def work(**overrides):
    body = {'work_id': 'work_1', 'run_id': 'run_1', 'authorized': True, 'suitable_for_free': True,
            'consume': 'implementation'}
    return {**body, **overrides}


def snapshot(**overrides):
    body = {'now': 1_000_000, 'usefulness_bar': 0.5, 'work': work(), 'workers': [], 'budgets': budgets()}
    return {**body, **overrides}


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'private' / 'journal.sqlite3'
        self.queue = Journal(self.path)
        self.addCleanup(self.queue.close)
        selection.bind_mandate(self.queue, MANDATE, now=100)

    def admit(self, **overrides):
        current = work()
        for key in ('suitable_for_free', 'consume', 'authorized'):
            if key in overrides:
                current = {**current, key: overrides.pop(key)}
        if 'work' in overrides:
            current = overrides.pop('work')
        stamp = overrides.get('now', 1_000_000)
        return driver.admit(self.queue, snapshot(work=current, **overrides), now=stamp)

    def assignment_count(self):
        return self.queue.connection.execute('SELECT COUNT(*) FROM selection_assignment').fetchone()[0]

    def test_reopen_after_kill_retains_mandate_and_refuses_second_owner(self):
        added = self.queue.ingest('operator', 'event_1', {'note': 'authorized mandate'}, 'wake_1', 'wake',
                                 'run_1', TARGET, now=100)
        self.assertTrue(added)
        gate = self.root / 'claimed'
        process = subprocess.Popen([sys.executable, '-c', CLAIM_CHILD, str(ROOT / 'scripts'), str(self.path), str(gate)])
        deadline = time.monotonic() + 10
        while not gate.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(gate.exists(), 'claim did not commit before the kill point')
        with Journal(self.path) as checker:
            self.assertEqual(checker.connection.execute('SELECT status FROM effect').fetchone()['status'], 'running')
        process.kill()
        self.assertEqual(process.wait(timeout=10), -9)
        worker = {'work_id': 'work_1', 'run_id': 'run_1', 'state': 'unverifiable', 'owner': 'dispatch_1'}
        first = self.admit(workers=[worker])
        second = self.admit(workers=[{**worker, 'owner': 'dispatch_2'}])
        self.assertEqual(first['action'], 'retain')
        self.assertFalse(first['launch'])
        self.assertFalse(first['native_start'])
        self.assertFalse(second['second_owner'])
        self.assertEqual(second['owner'], 'dispatch_1')
        self.assertEqual(self.assignment_count(), 1)
        self.assertEqual(selection.load(self.queue)['mandates']['work_1']['run_id'], 'run_1')
        state_path = self.root / 'native-state.json'
        state_path.write_text(json.dumps({
            'run': {'id': 'run_1', 'coordinator_handle': 'term_1', 'legacy': 0},
            'agent_state': 'done', 'since': 1000, 'prompt': 'previous', 'response': 'previous',
            'incarnation': 'process_1', 'workers': [{
                'runId': 'run_1', 'dispatchId': 'dispatch_1', 'dispatchStatus': 'dispatched',
                'terminalState': 'active', 'projection': {'outcome': 'in_progress',
                    'liveness': {'verdict': 'unverifiable'}}}],
            'messages': [], 'delivery': None, 'dispatches': [],
            'gates': [{'id': 'gate_1', 'status': 'pending'}], 'calls': [], 'submissions': 0,
            'requests': {}, 'mutations': {}, 'faults': {}}))
        previous = os.environ.get('NATIVE_FIXTURE_STATE')
        os.environ['NATIVE_FIXTURE_STATE'] = str(state_path)
        try:
            report = driver.tick(self.queue, {'schema_version': 1, 'coordinators': [TARGET]},
                                {'run_1': {'state': 'working'}},
                                str(ROOT / 'tests/fixtures/superagent-native.py'), now=1_000_000)
        finally:
            if previous is None:
                os.environ.pop('NATIVE_FIXTURE_STATE', None)
            else:
                os.environ['NATIVE_FIXTURE_STATE'] = previous
        native = json.loads(state_path.read_text())
        self.assertEqual(native['submissions'], 0)
        self.assertFalse(any(call[:2] == ['orchestration', 'worker-start'] for call in native['calls']))
        self.assertEqual(report['automatic_worker_creation'], False)
        self.assertEqual(report['selection']['owner'], 'dispatch_1')
        self.assertFalse(report['selection']['launch'])
        self.assertEqual(self.queue.summary()['effects']['reconciling'], 1)
        self.assertEqual(self.assignment_count(), 1)

    def test_live_worker_is_retained_when_a_second_owner_is_proposed(self):
        worker = {'work_id': 'work_1', 'run_id': 'run_1', 'state': 'live', 'owner': 'dispatch_1'}
        first = self.admit(workers=[worker], suitable_for_free=False)
        second = self.admit(workers=[{**worker, 'owner': 'dispatch_2'}])
        self.assertEqual(first['reason'], 'live_or_unverifiable')
        self.assertEqual(second['owner'], 'dispatch_1')
        self.assertFalse(second['launch'])
        self.assertFalse(second['native_start'])
        self.assertEqual(self.assignment_count(), 1)
        self.assertEqual(second['orchestrator']['model'], 'gpt-6.1-sol')
        self.assertEqual(second['orchestrator']['count'], 1)
        selection.register_route(self.queue, route(), now=100)
        resumed = self.admit(workers=[], suitable_for_free=True)
        self.assertEqual(resumed['action'], 'select')
        self.assertEqual(resumed['route_id'], 'zen_1')
        self.assertEqual(resumed['model'], 'zen/flash')
        self.assertTrue(resumed['launch'])
        self.assertFalse(resumed['native_start'])
        self.assertEqual(resumed['owner'], 'worker:zen_1')
        self.assertNotEqual(resumed['owner'], 'dispatch_1')
        self.assertEqual(self.assignment_count(), 1)
        stored = selection.load(self.queue)['assignments']['work_1']
        self.assertEqual(stored['state'], 'select')
        self.assertEqual(stored['route_id'], 'zen_1')

    def test_exhausted_route_does_not_select_unknown_or_unapproved_paid_fallback(self):
        selection.register_route(self.queue, route(
            route_id='clinepass_1', harness='dsh', family='clinepass',
            model='cline-pass/deepseek-v4.1-flash-1017', price={'prompt': 1, 'completion': 1, 'request': 0},
            approved_paid=True, exhausted=True), now=100)
        selection.register_route(self.queue, route(route_id='unknown_1', model='zen/unknown', price=None), now=100)
        with self.assertRaises(ValueError):
            selection.register_route(self.queue, route(
                route_id='unapproved_1', harness='dsh', family='clinepass',
                model='cline-pass/unapproved', price={'prompt': 1, 'completion': 1, 'request': 0},
                approved_paid=False), now=100)
        decision = self.admit(suitable_for_free=False, budgets=budgets(dsh_exhausted=True))
        self.assertEqual(decision['action'], 'refused')
        self.assertIsNone(decision['route_id'])
        self.assertFalse(decision['paid_route_opened'])
        self.assertNotEqual(decision['model'], 'zen/unknown')
        self.assertEqual(self.assignment_count(), 0)
        self.assertFalse(selection.eligible_paid(
            route(family='clinepass', model='cline-pass/unapproved', approved_paid=False,
                  price={'prompt': 1, 'completion': 1, 'request': 0}), 'clinepass'))

    def test_registration_makes_one_harness_or_temporary_endpoint_selectable(self):
        other = selection.register_route(self.queue, route(route_id='qoder_1', harness='qoder', family='qoder',
                                                           model='qoder/deepseek-flash', usefulness=0.2), now=100)
        harness = selection.register_route(self.queue, route(route_id='harness_1', harness='fixture-harness',
                                                             model='fixture/harness-model'), now=100)
        temporary = selection.register_route(self.queue, route(
            route_id='temp_1', harness='operator-temp', family='temporary', model='temp/endpoint',
            expires_at=2_000_000, usefulness=0.95), now=100)
        with self.assertRaises(ValueError):
            selection.register_route(self.queue, route(role='orchestrator', model='gpt-6.1-sol'), now=100)
        with self.assertRaises(ValueError):
            selection.register_route(self.queue, route(route_id='harness_1', model='fixture/changed'), now=100)
        reloaded = selection.load(self.queue)['routes']
        self.assertEqual(reloaded['qoder_1'], other)
        self.assertEqual(reloaded['harness_1']['model'], harness['model'])
        decision = self.admit()
        self.assertEqual(decision['route_id'], 'temp_1')
        self.assertEqual(decision['model'], temporary['model'])
        self.assertEqual(decision['orchestrator'], selection.orchestrator())
        self.assertEqual(decision['orchestrator']['model'], 'gpt-6.1-sol')
        self.assertEqual(decision['orchestrator']['endpoint'], 'agentrouter/anyrouter')
        self.assertNotIn('qoder_1', (decision['route_id'],))
        expired = self.admit(now=3_000_000)
        self.assertEqual(expired['route_id'], 'harness_1')
        self.assertNotEqual(expired['model'], 'gpt-6.1-sol')
        self.assertEqual(selection.load(self.queue)['routes']['qoder_1']['harness'], 'qoder')

    def test_eligible_free_quota_beats_paid_routes_and_skips_bad_models(self):
        selection.register_route(self.queue, route(), now=100)
        selection.register_route(self.queue, route(
            route_id='clinepass_1', harness='dsh', family='clinepass',
            model='cline-pass/deepseek-v4.1-flash-1017', price={'prompt': 1, 'completion': 1, 'request': 0},
            approved_paid=True), now=100)
        selection.register_route(self.queue, route(
            route_id='go_1', harness='dsh', family='opencode-go', model='opencode-go/glm-5.3-flash',
            price=dict(ZERO), approved_paid=True), now=100)
        selection.register_route(self.queue, route(route_id='low_1', family='qoder', harness='qoder',
                                                   model='qoder/qwen', usefulness=0.2), now=100)
        selection.register_route(self.queue, route(
            route_id='priced_stealth', family='stealth', harness='openrouter', model='stealth/priced',
            price={'prompt': 1, 'completion': 0, 'request': 0}), now=100)
        selection.register_route(self.queue, route(
            route_id='expired_stealth', family='stealth', harness='openrouter', model='stealth/expired',
            expires_at=10), now=100)
        selection.register_route(self.queue, route(
            route_id='bad_stealth', family='stealth', harness='openrouter', model='stealth/bad',
            malformed=True), now=100)
        selection.register_route(self.queue, route(
            route_id='ordinary_free', family='stealth', harness='openrouter', model='openrouter/free'), now=100)
        decision = self.admit()
        self.assertEqual(decision['route_id'], 'zen_1')
        self.assertEqual(decision['family'], 'zen')
        for rejected in ('clinepass_1', 'go_1', 'low_1', 'priced_stealth', 'expired_stealth', 'bad_stealth',
                         'ordinary_free'):
            self.assertNotEqual(decision['route_id'], rejected)
        self.assertNotEqual(decision['model'], 'opencode-go/glm-5.3-flash')

    def test_subscription_families_are_not_assigned_when_exhausted(self):
        selection.register_route(self.queue, route(
            route_id='clinepass_1', harness='dsh', family='clinepass',
            model='cline-pass/deepseek-v4.1-flash-1017', price={'prompt': 1, 'completion': 1, 'request': 0},
            approved_paid=True), now=100)
        selection.register_route(self.queue, route(
            route_id='go_1', harness='dsh', family='opencode-go', model='opencode-go/glm-5.3-flash',
            price={'prompt': 2, 'completion': 2, 'request': 0}, approved_paid=True), now=100)
        dsh_only = self.admit(suitable_for_free=False, consume='balanced',
                              budgets=budgets(agent=0, agent_exhausted=True, dsh=25))
        self.assertEqual(dsh_only['family'], 'dsh')
        self.assertEqual(dsh_only['route_id'], 'clinepass_1')
        self.assertNotEqual(dsh_only['family'], 'agentrouter')
        sol = self.admit(suitable_for_free=False, consume='balanced',
                         budgets=budgets(agent=40, dsh=0, dsh_exhausted=True))
        self.assertEqual(sol['family'], 'agentrouter')
        self.assertEqual(sol['model'], 'gpt-6.1-sol')
        self.assertEqual(sol['route_id'], 'orca_agentrouter')
        self.assertNotEqual(sol['family'], 'dsh')
        fresh = Journal(self.root / 'private' / 'other.sqlite3')
        self.addCleanup(fresh.close)
        selection.bind_mandate(fresh, MANDATE, now=100)
        selection.register_route(fresh, route(route_id='unknown_1', price=None), now=100)
        explicit = driver.admit(fresh, snapshot(
            work=work(suitable_for_free=False, consume='dsh'), budgets=budgets(dsh_exhausted=True)), now=1_000_000)
        self.assertEqual(explicit['action'], 'refused')
        self.assertEqual(explicit['reason'], 'dsh_exhausted')
        self.assertIsNone(explicit['route_id'])
        go_only = Journal(self.root / 'private' / 'go.sqlite3')
        self.addCleanup(go_only.close)
        selection.bind_mandate(go_only, MANDATE, now=100)
        selection.register_route(go_only, route(
            route_id='go_1', harness='dsh', family='opencode-go', model='opencode-go/glm-5.3-flash',
            price=dict(ZERO), approved_paid=True), now=100)
        paid = driver.admit(go_only, snapshot(work=work(suitable_for_free=True), budgets=budgets()), now=1_000_000)
        self.assertEqual(paid['route_id'], 'go_1')
        self.assertEqual(paid['model'], 'opencode-go/glm-5.3-flash')
        self.assertEqual(paid['family'], 'dsh')
        self.assertFalse(paid['native_start'])

    def test_first_sos_keeps_owner_and_later_or_unstuck_sos_does_not_dispatch(self):
        transcript = [{'role': 'assistant', 'text': 'evidence: named the missing price field'}]
        selection.update_profile(self.queue, 'stealth/fixture', transcript, now=100)
        selection.update_profile(self.queue, 'cline-pass/deepseek-v4.1-flash-1017',
                                [{'role': 'assistant', 'text': 'evidence: stayed on the paid route'}], now=100)
        sos = {'episode_id': 'episode_1', 'stuck': True, 'recorded': True, 'owner': 'dispatch_1',
               'model': 'cline-pass/deepseek-v4.1-flash-1017'}
        first = self.admit(sos=sos, now=1_000)
        self.assertTrue(first['dispatch'])
        self.assertEqual(first['owner'], 'dispatch_1')
        self.assertFalse(first['ownership_transferred'])
        self.assertFalse(first['paid_route_opened'])
        self.assertEqual(first['insight_model'], 'stealth/fixture')
        self.assertIn('named the missing price field', first['insight'])
        second = self.admit(sos=sos, now=1_000 + 10)
        self.assertFalse(second['dispatch'])
        self.assertEqual(second['reason'], 'sos_limit')
        self.assertEqual(second['owner'], 'dispatch_1')
        idle = self.admit(sos={**sos, 'stuck': False, 'recorded': False}, now=1_200)
        self.assertFalse(idle['dispatch'])
        self.assertEqual(idle['reason'], 'no_stuck_episode')
        self.assertEqual(self.queue.connection.execute('SELECT COUNT(*) FROM selection_sos').fetchone()[0], 1)
        later = self.admit(sos=sos, now=1_000 + selection.SOS_WINDOW_SECONDS)
        self.assertTrue(later['dispatch'])
        self.assertEqual(later['owner'], 'dispatch_1')
        self.assertFalse(later['paid_route_opened'])

    def test_profile_update_keeps_only_transcript_evidence_and_selection_reads_it(self):
        assistant = 'evidence: kept the original token boundary\nI guessed a stronger plan'
        transcript = [{'role': 'user', 'text': 'evidence: user invented a strength\nstrong at planning'},
                      {'role': 'assistant', 'text': assistant},
                      {'role': 'assistant', 'text': 'evidence: api_key sk-secret-value'}]
        body = selection.update_profile(self.queue, 'stealth/fixture', transcript, now=100)
        self.assertTrue(body.startswith('model: stealth/fixture\n'))
        self.assertIn('evidence: kept the original token boundary\n', body)
        self.assertNotIn('user invented', body)
        self.assertNotIn('strong at planning', body)
        self.assertNotIn('guessed', body)
        self.assertNotIn('sk-', body)
        for line in body.splitlines():
            if line.startswith('evidence: '):
                self.assertIn(line[len('evidence: '):], assistant)
        selection.register_route(self.queue, route(
            route_id='stealth_1', family='stealth', harness='openrouter', model='stealth/fixture'), now=100)
        decision = self.admit()
        self.assertEqual(decision['model'], 'stealth/fixture')
        self.assertEqual(decision['profile_text'], body)
        self.assertEqual(selection.load(self.queue)['profiles']['stealth/fixture'], decision['profile_text'])

    def test_status_command_is_stable_and_names_orchestrator_routes_and_faults(self):
        selection.register_route(self.queue, route(route_id='glm_1', family='zcode-glm', harness='zcode',
                                                   model='zcode/glm'), now=100)
        config = self.root / 'settings.json'
        config.write_text(json.dumps({'schema_version': 1, 'coordinators': [TARGET]}))
        config.chmod(0o600)
        dispositions = self.root / 'dispositions.json'
        dispositions.write_text('{}')
        dispositions.chmod(0o600)
        command = [sys.executable, str(ROOT / 'scripts/spectre-superagent.py'), '--config', str(config),
                   '--dispositions', str(dispositions), '--journal', str(self.path), '--orca', 'unused-orca',
                   'status']
        first = subprocess.run(command, text=True, capture_output=True, timeout=10, check=False)
        second = subprocess.run(command, text=True, capture_output=True, timeout=10, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)['selection']
        self.assertEqual(report['work_id'], 'work_1')
        self.assertEqual(report['orchestrator']['model'], 'gpt-6.1-sol')
        self.assertEqual(report['orchestrator']['endpoint'], 'agentrouter/anyrouter')
        self.assertEqual(report['orchestrator']['count'], 1)
        self.assertEqual(report['admitted_routes'][0]['route_id'], 'glm_1')
        self.assertTrue(report['fault_record'].endswith('#selection_fault'))
        self.assertNotIn('sk-', first.stdout)

    def test_instructions_name_native_authority_duplicate_rule_sos_limit_and_faults(self):
        text = selection.instructions()
        self.assertIn('Orca Run, Task, and Dispatch', text)
        self.assertIn('second owner', text)
        self.assertIn('five hours', text)
        self.assertIn('selection_fault', text)
        self.assertIn('orca-agentrouter-b', text)

    def test_proxy_snapshot_balances_the_existing_agentrouter_family(self):
        mapped = selection.budgets_from_proxy(
            {'providers': {'agentrouter': {'remaining': 0, 'exhausted': True},
                           'anyrouter': {'remaining': 4, 'exhausted': False}}},
            {'remaining': 0, 'exhausted': True, 'limit': 100})
        self.assertFalse(mapped['agentrouter']['exhausted'])
        self.assertTrue(mapped['dsh']['exhausted'])
        both = selection.budgets_from_proxy(
            {'providers': {'agentrouter': {'remaining': 0, 'exhausted': True},
                           'anyrouter': {'remaining': 0, 'exhausted': True}}},
            {'remaining': 9, 'exhausted': False, 'limit': 100})
        self.assertTrue(both['agentrouter']['exhausted'])
        self.assertFalse(both['dsh']['exhausted'])
        with self.assertRaises(ValueError):
            selection.budgets_from_proxy({'providers': []}, {})

    def test_malformed_snapshot_appends_a_fault_and_keeps_the_mandate(self):
        decision = driver.admit(self.queue, ['not', 'a', 'snapshot'], now=100)
        self.assertEqual(decision['action'], 'refused')
        self.assertFalse(decision['launch'])
        self.assertEqual(selection.load(self.queue)['mandates']['work_1']['work_id'], 'work_1')
        self.assertEqual(selection.status_view(self.queue)['faults'], 1)
        self.assertTrue(selection.status_view(self.queue)['fault_record'].endswith('#selection_fault'))


if __name__ == '__main__':
    unittest.main()
