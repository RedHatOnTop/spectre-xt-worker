"""The service tick reads the ledger and launches at most one native worker."""
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
SETTINGS = {'schema_version': 1, 'coordinators': [TARGET]}
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
    body = {'route_id': 'zen_1', 'harness': 'opencode-zen', 'adapter': 'registered', 'family': 'zen',
            'model': 'zen/flash', 'role': 'worker', 'price': dict(ZERO), 'expires_at': None,
            'usefulness': 0.9, 'remaining': 8, 'approved_paid': False, 'malformed': False, 'exhausted': False}
    return {**body, **overrides}


def work(**overrides):
    body = {'work_id': 'work_1', 'run_id': 'run_1', 'authorized': True, 'suitable_for_free': True,
            'consume': 'implementation', 'task_id': 'task_1', 'worktree_id': 'repo::/worker'}
    return {**body, **overrides}


def proxy(agent=20, anyrouter=10, dsh=20, agent_exhausted=False, anyrouter_exhausted=False, dsh_exhausted=False):
    return {'proxy': {'providers': {
                'agentrouter': {'remaining': agent, 'exhausted': agent_exhausted},
                'anyrouter': {'remaining': anyrouter, 'exhausted': anyrouter_exhausted}}},
            'dsh': {'remaining': dsh, 'exhausted': dsh_exhausted, 'limit': 100}}


class TickSelectionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'private' / 'journal.sqlite3'
        self.queue = Journal(self.path)
        self.addCleanup(self.queue.close)
        self.state_path = self.root / 'native-state.json'
        self.fixture = str(ROOT / 'tests/fixtures/superagent-native.py')

    def write_ledger(self, **overrides):
        body = {'usefulness_bar': 0.5, 'mandates': [{'work_id': 'work_1', 'run_id': 'run_1', 'authorized': True}],
                'work': work(), 'routes': [], 'transcripts': {}, **proxy()}
        body.update(overrides)
        path = self.path.parent / 'free-ledger.json'
        path.write_text(json.dumps(body))
        path.chmod(0o600)
        return body

    def native(self, **overrides):
        state = {'run': {'id': 'run_1', 'coordinator_handle': 'term_1', 'legacy': 0},
                 'agent_state': 'working', 'since': 1000, 'prompt': 'previous', 'response': 'previous',
                 'incarnation': 'process_1', 'workspace_path': '/work', 'workers': [], 'messages': [],
                 'delivery': None, 'dispatches': [], 'gates': [], 'calls': [], 'submissions': 0,
                 'requests': {}, 'mutations': {}, 'faults': {}, 'tasks': [
                     {'id': 'task_1', 'run_id': 'run_1', 'status': 'ready'}],
                 'workspace_pages': [{'worktrees': [
                     {'worktreeId': 'repo::/work', 'path': '/work', 'agents': [
                         {'paneKey': 'pane_1', 'state': 'working', 'stateStartedAt': 1000,
                          'prompt': 'previous', 'lastAssistantMessage': 'previous', 'interrupted': False}]},
                     {'worktreeId': 'repo::/worker', 'path': '/worker'}]}]}
        state.update(overrides)
        if 'agent_state' in overrides:
            state['workspace_pages'][0]['worktrees'][0]['agents'][0]['state'] = overrides['agent_state']
        self.state_path.write_text(json.dumps(state))
        return state

    def tick(self, now=1_000_000, disposition=None):
        previous = os.environ.get('NATIVE_FIXTURE_STATE')
        os.environ['NATIVE_FIXTURE_STATE'] = str(self.state_path)
        try:
            return driver.tick(self.queue, SETTINGS, disposition or {'run_1': {'state': 'working'}},
                               self.fixture, now=now)
        finally:
            if previous is None:
                os.environ.pop('NATIVE_FIXTURE_STATE', None)
            else:
                os.environ['NATIVE_FIXTURE_STATE'] = previous

    def starts(self):
        state = json.loads(self.state_path.read_text())
        return [call for call in state['calls'] if call[:2] == ['orchestration', 'worker-start']]

    def test_unblocked_tick_stores_one_select_and_launches_once(self):
        self.write_ledger(routes=[route()])
        self.native()
        first = self.tick()
        self.assertEqual(first['selection']['action'], 'select')
        self.assertEqual(first['selection']['route_id'], 'zen_1')
        self.assertEqual(first['selection']['orchestrator']['model'], 'gpt-6.1-sol')
        self.assertEqual(first['selection']['orchestrator']['count'], 1)
        self.assertEqual(first['worker_launch']['state'], 'ready')
        self.assertFalse(first['worker_launch']['replayed'])
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.starts()[0][self.starts()[0].index('--agent') + 1], 'dsh-worker')
        second = self.tick(now=1_000_100)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.queue.connection.execute('SELECT COUNT(*) FROM selection_assignment').fetchone()[0], 1)
        self.assertEqual(second['selection']['launch'], False)
        self.assertNotIn('worker_launch', second)
        self.assertFalse(second['automatic_worker_creation'])

    def test_live_unverifiable_and_open_claim_do_not_launch(self):
        self.write_ledger(routes=[route()])
        live = {'runId': 'run_1', 'dispatchId': 'dispatch_1', 'dispatchStatus': 'dispatched',
                'terminalState': 'active', 'projection': {'outcome': 'in_progress'}}
        self.native(workers=[live])
        report = self.tick()
        self.assertEqual(report['selection']['action'], 'retain')
        self.assertEqual(report['selection']['owner'], 'dispatch_1')
        self.assertFalse(report['selection']['launch'])
        self.assertNotIn('worker_launch', report)
        self.assertEqual(self.starts(), [])
        again = self.tick(now=1_000_050)
        self.assertEqual(again['selection']['owner'], 'dispatch_1')
        self.assertEqual(self.starts(), [])
        unverifiable = {**live, 'dispatchId': 'dispatch_9', 'projection': {'liveness': {'verdict': 'unverifiable'}}}
        self.native(workers=[unverifiable])
        held = self.tick(now=1_000_080)
        self.assertEqual(held['selection']['owner'], 'dispatch_1')
        self.assertEqual(self.starts(), [])

    def test_finished_disposition_keeps_one_live_worker(self):
        live = {'runId': 'run_1', 'dispatchId': 'dispatch_1', 'dispatchStatus': 'dispatched',
                'terminalState': 'active', 'projection': {'outcome': 'in_progress'}}
        self.native(workers=[live])
        self.assertFalse((self.path.parent / 'free-ledger.json').exists())
        self.assertFalse(selection.table_exists(self.queue, 'selection_mandate'))
        self.assertFalse(selection.table_exists(self.queue, 'selection_assignment'))
        report = self.tick(disposition={'run_1': {'state': 'finished'}})
        self.assertEqual(report['selection']['action'], 'retain')
        self.assertEqual(report['selection']['owner'], 'dispatch_1')
        self.assertFalse(report['selection']['launch'])
        self.assertNotIn('worker_launch', report)
        self.assertEqual(self.starts(), [])

    def test_open_claim_after_kill_keeps_the_owner_and_does_not_launch(self):
        self.write_ledger(routes=[route()])
        self.native()
        self.queue.ingest('operator', 'event_1', {'note': 'authorized'}, 'wake_1', 'wake', 'run_1', TARGET, now=100)
        gate = self.root / 'claimed'
        process = subprocess.Popen([sys.executable, '-c', CLAIM_CHILD, str(ROOT / 'scripts'), str(self.path), str(gate)])
        deadline = time.monotonic() + 10
        while not gate.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(gate.exists())
        process.kill()
        self.assertEqual(process.wait(timeout=10), -9)
        report = self.tick()
        self.assertEqual(report['selection']['action'], 'retain')
        self.assertFalse(report['selection']['launch'])
        self.assertEqual(self.starts(), [])
        self.assertEqual(json.loads(self.state_path.read_text())['submissions'], 0)
        self.assertEqual(self.queue.connection.execute('SELECT COUNT(*) FROM selection_assignment').fetchone()[0], 1)

    def test_tick_prefers_glm_and_stealth_and_skips_bad_or_exhausted_routes(self):
        paid = [route(route_id='clinepass_1', harness='dsh', family='clinepass',
                      model='cline-pass/deepseek-v4.1-flash-1017', price={'prompt': 1, 'completion': 1, 'request': 0},
                      approved_paid=True, usefulness=0.99),
                route(route_id='go_1', harness='dsh', family='opencode-go', model='opencode-go/glm-5.3-flash',
                      price={'prompt': 1, 'completion': 1, 'request': 0}, approved_paid=True, usefulness=0.99)]
        bad = [route(route_id='low_1', family='qoder', harness='qoder', model='qoder/qwen', usefulness=0.2),
               route(route_id='priced_stealth', family='stealth', harness='openrouter', model='stealth/priced',
                     price={'prompt': 1, 'completion': 0, 'request': 0}),
               route(route_id='expired_stealth', family='stealth', harness='openrouter', model='stealth/expired',
                     expires_at=10),
               route(route_id='bad_stealth', family='stealth', harness='openrouter', model='stealth/bad', malformed=True),
               route(route_id='ordinary_free', family='stealth', harness='openrouter', model='openrouter/free'),
               route(route_id='unknown_1', model='zen/unknown', price=None),
               route(route_id='unapproved_1', harness='dsh', family='clinepass', model='cline-pass/unapproved',
                     price={'prompt': 1, 'completion': 1, 'request': 0}, approved_paid=False)]
        self.write_ledger(routes=[*paid, *bad, route(route_id='glm_1', family='zcode-glm', harness='zcode',
                                                     model='zcode/glm', usefulness=0.95)])
        self.native()
        glm = self.tick()
        self.assertEqual(glm['selection']['route_id'], 'glm_1')
        self.assertEqual(glm['selection']['family'], 'zcode-glm')
        self.assertNotIn(glm['selection']['route_id'], {'clinepass_1', 'go_1', 'low_1', 'priced_stealth',
                                                        'expired_stealth', 'bad_stealth', 'ordinary_free', 'unknown_1',
                                                        'unapproved_1'})
        self.queue = Journal(self.root / 'private' / 'stealth.sqlite3')
        self.addCleanup(self.queue.close)
        self.path = self.queue.path
        assistant = 'evidence: kept the original token boundary\nI guessed a stronger plan'
        self.write_ledger(routes=[*paid, *bad, route(
            route_id='stealth_1', family='stealth', harness='openrouter', model='stealth/fixture', usefulness=0.97,
            expires_at=2_000_000)], transcripts={'stealth/fixture': [
                {'role': 'user', 'text': 'evidence: user invented a strength'},
                {'role': 'assistant', 'text': assistant}]})
        self.native()
        chosen = self.tick(now=1_000_200)
        self.assertEqual(chosen['selection']['model'], 'stealth/fixture')
        profile = chosen['selection']['profile_text']
        self.assertTrue(profile.startswith('model: stealth/fixture\n'))
        self.assertNotIn('user invented', profile)
        self.assertNotIn('guessed', profile)
        for line in profile.splitlines():
            if line.startswith('evidence: '):
                self.assertIn(line[len('evidence: '):], assistant)
        self.assertEqual(selection.load(self.queue)['profiles']['stealth/fixture'], profile)

    def test_exhausted_family_receives_no_new_work(self):
        paid = route(route_id='clinepass_1', harness='dsh', family='clinepass',
                     model='cline-pass/deepseek-v4.1-flash-1017', price={'prompt': 1, 'completion': 1, 'request': 0},
                     approved_paid=True)
        self.write_ledger(work=work(suitable_for_free=False, consume='balanced'), routes=[paid],
                          **proxy(agent=0, anyrouter=0, agent_exhausted=True, anyrouter_exhausted=True, dsh=30))
        self.native()
        dsh = self.tick()
        self.assertEqual(dsh['selection']['family'], 'dsh')
        self.assertEqual(dsh['selection']['route_id'], 'clinepass_1')
        self.assertNotEqual(dsh['selection']['family'], 'agentrouter')
        self.queue = Journal(self.root / 'private' / 'sol.sqlite3')
        self.addCleanup(self.queue.close)
        self.path = self.queue.path
        self.write_ledger(work=work(suitable_for_free=False, consume='balanced'), routes=[paid, route(price=None,
                          route_id='unknown_1', model='zen/unknown')],
                          **proxy(dsh=0, dsh_exhausted=True, agent=40))
        self.native()
        sol = self.tick(now=1_000_300)
        self.assertEqual(sol['selection']['family'], 'agentrouter')
        self.assertEqual(sol['selection']['model'], 'gpt-6.1-sol')
        self.assertIsNone(sol['selection']['route_id'] if sol['selection']['route_id'] == 'unknown_1' else None)
        self.assertNotEqual(sol['selection']['route_id'], 'unknown_1')
        self.assertNotEqual(sol['selection']['family'], 'dsh')
        self.assertNotIn('worker_launch', sol)

    def test_sos_limit_is_enforced_by_the_tick(self):
        transcript = [{'role': 'assistant', 'text': 'evidence: named the missing price field'}]
        sos = {'episode_id': 'episode_1', 'stuck': True, 'recorded': True, 'owner': 'dispatch_1',
               'model': 'cline-pass/deepseek-v4.1-flash-1017'}
        self.write_ledger(transcripts={'stealth/insight': transcript,
                                       'cline-pass/deepseek-v4.1-flash-1017': [
                                           {'role': 'assistant', 'text': 'evidence: stayed on the paid route'}]},
                          sos=sos, routes=[route()])
        self.native()
        first = self.tick(now=1_000)
        self.assertTrue(first['selection']['dispatch'])
        self.assertEqual(first['selection']['owner'], 'dispatch_1')
        self.assertFalse(first['selection']['ownership_transferred'])
        self.assertFalse(first['selection']['paid_route_opened'])
        self.assertEqual(first['selection']['insight_model'], 'stealth/insight')
        self.assertIn('named the missing price field', first['selection']['insight'])
        self.assertEqual(self.starts(), [])
        second = self.tick(now=1_010)
        self.assertFalse(second['selection']['dispatch'])
        self.assertEqual(second['selection']['reason'], 'sos_limit')
        self.assertEqual(second['selection']['owner'], 'dispatch_1')
        self.assertEqual(self.queue.connection.execute('SELECT COUNT(*) FROM selection_sos').fetchone()[0], 1)
        self.write_ledger(sos={**sos, 'stuck': False, 'recorded': False}, routes=[route()])
        idle = self.tick(now=1_020)
        self.assertFalse(idle['selection']['dispatch'])
        self.assertEqual(idle['selection']['reason'], 'no_stuck_episode')
        self.assertEqual(self.queue.connection.execute('SELECT COUNT(*) FROM selection_sos').fetchone()[0], 1)

    def test_idle_quota_without_authorized_work_does_not_launch(self):
        self.write_ledger(work=None, mandates=[], routes=[route()])
        self.native()
        report = self.tick()
        self.assertNotIn('selection', report)
        self.assertNotIn('worker_launch', report)
        self.assertEqual(self.starts(), [])
        self.assertFalse(selection.table_exists(self.queue, 'selection_assignment'))


if __name__ == '__main__':
    unittest.main()
