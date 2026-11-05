"""Native runtime observation must never authorize terminal mutations."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from orca_runtime import observe


def worker(verdict='live', outcome='in_progress', terminal='active', attention=False):
    return {
        'dispatchId': 'dispatch_test', 'runId': 'run_test', 'taskId': 'task_test',
        'worktreeId': 'repo::/project', 'terminalState': terminal,
        'agentTerminalHandle': 'term_test',
        'projection': {
            'outcome': outcome, 'liveness': {'verdict': verdict},
            'evidence': {'liveStatus': 'fresh'},
            'attention': {'requiresAction': attention, 'categories': []},
            'nextAction': {'kind': 'inspect', 'argv': ['orca', 'orchestration', 'worker-show']},
        },
    }


def inventory(workers=None):
    return {
        'runtime': {'runtime': {'reachable': True, 'state': 'ready'}},
        'worktrees': [{'path': '/project', 'worktreeId': 'repo::/project'}],
        'terminals': [{'handle': 'term_test', 'worktreeId': 'repo::/project', 'connected': True}],
        'workers': [worker()] if workers is None else workers,
    }


class ObservationTest(unittest.TestCase):
    def test_settled_success_is_not_a_liveness_incident(self):
        settled = worker('unverifiable', 'succeeded', 'retained', attention=True)
        settled = {**settled, 'workerState': 'succeeded', 'dispatchStatus': 'completed'}
        self.assertTrue(observe.assess(inventory([settled]), [])['ok'])

    def test_old_attempt_does_not_override_current_native_owner(self):
        old = worker('exited', 'failed', attention=True)
        old = {**old, 'dispatchId': 'old'}
        self.assertTrue(observe.assess(inventory([worker(), old]), ['/project'])['ok'])

    def test_fresh_native_run_coordinator_counts_as_work(self):
        data = inventory([])
        data = {**data, 'runs': [{'id': 'run_test', 'legacy': 0, 'coordinator_handle': 'term_test'}],
                'terminals': [{**data['terminals'][0], 'paneKey': 'pane_test'}],
                'worktrees': [{**data['worktrees'][0], 'agents': [{'paneKey': 'pane_test', 'state': 'working', 'updatedAt': 100000}]}]}
        self.assertTrue(observe.assess(data, ['/project'], now=100)['ok'])

    def test_stale_coordinator_does_not_hide_missing_workers(self):
        data = inventory([])
        data = {**data, 'runs': [{'id': 'run_test', 'legacy': 0, 'coordinator_handle': 'term_test'}],
                'terminals': [{**data['terminals'][0], 'paneKey': 'pane_test'}],
                'worktrees': [{**data['worktrees'][0], 'agents': [{'paneKey': 'pane_test', 'state': 'working', 'updatedAt': 1000}]}]}
        self.assertFalse(observe.assess(data, ['/project'], now=1000)['ok'])

    def test_missing_worker_is_an_incident_not_completion(self):
        report = observe.assess(inventory([]), ['/project'])
        self.assertFalse(report['ok'])
        self.assertIn('required_workspace_unassigned', str(report['issues']))

    def test_live_shell_does_not_replace_supervised_worker(self):
        self.assertFalse(observe.assess(inventory([]), ['/project'])['ok'])

    def test_live_native_agent_is_positive_evidence(self):
        self.assertTrue(observe.assess(inventory(), ['/project'])['ok'])

    def test_unverifiable_never_becomes_exited(self):
        report = observe.assess(inventory([worker('unverifiable')]), ['/project'])
        self.assertFalse(report['ok'])
        self.assertEqual(report['workers'][0]['projection']['liveness']['verdict'], 'unverifiable')
        self.assertNotIn('retry', report)

    def test_exited_unsettled_agent_requires_attention(self):
        self.assertFalse(observe.assess(inventory([worker('exited')]), ['/project'])['ok'])

    def test_released_success_is_not_a_running_assignment(self):
        report = observe.assess(inventory([worker('exited', 'succeeded', 'released')]), ['/project'])
        self.assertFalse(report['ok'])

    def test_no_required_work_and_no_workers_is_explicit_idle(self):
        report = observe.assess(inventory([]), [])
        self.assertTrue(report['ok'])
        self.assertEqual(report['status'], 'idle')

    def test_attention_on_live_worker_is_not_healthy(self):
        self.assertFalse(observe.assess(inventory([worker(attention=True)]), ['/project'])['ok'])

    def test_stale_status_is_not_live_progress(self):
        w = worker()
        w = {**w, 'projection': {**w['projection'], 'evidence': {'liveStatus': 'stale'}}}
        self.assertFalse(observe.assess(inventory([w]), ['/project'])['ok'])

    def test_missing_terminal_does_not_trigger_relaunch(self):
        self.assertFalse(observe.assess({**inventory(), 'terminals': []}, ['/project'])['ok'])

    def test_unknown_projection_fails_closed(self):
        self.assertFalse(observe.assess(inventory([{'terminalState': 'active'}]), [])['ok'])

    def test_unreachable_runtime_is_not_idle(self):
        report = observe.assess({**inventory(), 'runtime': {}}, [])
        self.assertFalse(report['ok'])

    def test_unknown_required_workspace_is_reported(self):
        self.assertFalse(observe.assess(inventory(), ['/elsewhere'])['ok'])


class CollectionTest(unittest.TestCase):
    def test_scoped_run_reads_never_scan_unrelated_run_history(self):
        with patch.object(observe, 'query', side_effect=[{'run': {'id': 'run_one'}},
                {'run': {'id': 'run_two'}}]) as query:
            result = observe.collect_runs('/cli', run_ids=('run_one', 'run_two'))
        self.assertEqual([row['id'] for row in result], ['run_one', 'run_two'])
        self.assertEqual([call.args[1] for call in query.call_args_list], [
            ['orchestration', 'run-show', '--id', 'run_one', '--full'],
            ['orchestration', 'run-show', '--id', 'run_two', '--full']])

    def test_show_run_reads_the_full_record_and_rejects_the_concise_view(self):
        with patch.object(observe, 'query', return_value={'runId': 'run_one', 'objective': 'concise'}) as query, \
                self.assertRaisesRegex(ValueError, 'scope'):
            observe.show_run('/cli', 'run_one')
        self.assertEqual(query.call_args.args[1], ['orchestration', 'run-show', '--id', 'run_one', '--full'])
        with patch.object(observe, 'query', return_value={'run': {'id': 'run_one', 'coordinator_handle': 'term_1'}}):
            self.assertEqual(observe.show_run('/cli', 'run_one')['coordinator_handle'], 'term_1')

    def test_scoped_run_identity_mismatch_is_not_an_empty_inventory(self):
        with patch.object(observe, 'query', return_value={'run': {'id': 'other'}}), \
                self.assertRaisesRegex(ValueError, 'scope'):
            observe.collect_runs('/cli', run_ids=('run_one',))

    def test_scoped_worker_pages_keep_the_same_explicit_run(self):
        first = {'workers': [worker()], 'page': {'hasMore': True, 'nextCursor': 'next'},
                 'scope': {'run': 'run_test'}}
        last = {'workers': [], 'page': {'hasMore': False}, 'scope': {'run': 'run_test'}}
        with patch.object(observe, 'query', side_effect=[first, last]) as query:
            self.assertEqual(len(observe.collect_workers('/cli', run_id='run_test')), 1)
        self.assertTrue(all(call.args[1][call.args[1].index('--run') + 1] == 'run_test'
                            for call in query.call_args_list))

    def test_scoped_worker_response_cannot_hide_another_run_or_missing_scope(self):
        for scope in ({}, {'run': 'other'}):
            page = {'workers': [], 'page': {'hasMore': False}, 'scope': scope}
            with self.subTest(scope=scope), patch.object(observe, 'query', return_value=page), \
                    self.assertRaisesRegex(ValueError, 'scope'):
                observe.collect_workers('/cli', run_id='run_test')

    def test_partial_workspace_inventory_is_expanded_and_never_reported_complete(self):
        first = {'worktrees': [{'worktreeId': 'first'}], 'truncated': True, 'totalCount': 101}
        final = {'worktrees': [{'worktreeId': 'target'}], 'truncated': False, 'totalCount': 1}
        with patch.object(observe, 'query', side_effect=[first, final]) as query:
            rows = observe.collect_workspaces('/cli')
        self.assertEqual(rows, final['worktrees'])
        self.assertEqual(query.call_args.args[1], ['worktree', 'ps', '--limit', '101'])
        with patch.object(observe, 'query', return_value=first), \
                self.assertRaisesRegex(ValueError, 'incomplete'):
            observe.collect_workspaces('/cli')

    def test_control_snapshot_omits_worker_history_without_omitting_owner_state(self):
        with patch.object(observe, 'query', side_effect=[inventory()['runtime'], {'terminals': []}]), \
                patch.object(observe, 'collect_workspaces', return_value=[]) as workspaces, \
                patch.object(observe, 'collect_runs', return_value=[]) as runs, \
                patch.object(observe, 'collect_workers') as workers:
            result = observe.collect('/cli', run_ids=('run_test',), include_workers=False)
        workers.assert_not_called()
        runs.assert_called_once_with('/cli', run_ids=('run_test',))
        workspaces.assert_called_once_with('/cli')
        self.assertEqual(result['workers'], [])

    def test_run_inventory_pagination(self):
        pages = [{'runs': [{'id': 'r1'}], 'nextCursor': 1}, {'runs': [{'id': 'r2'}], 'nextCursor': None}]
        with patch('orca_runtime.observe.query', side_effect=pages):
            self.assertEqual(observe.collect_runs('/cli'), [{'id': 'r1'}, {'id': 'r2'}])

    def test_run_inventory_rejects_repeated_cursor(self):
        with patch('orca_runtime.observe.query', return_value={'runs': [], 'nextCursor': 1}):
            with self.assertRaisesRegex(ValueError, 'cursor'):
                observe.collect_runs('/cli')

    def test_run_inventory_requires_current_schema(self):
        with patch('orca_runtime.observe.query', return_value={}):
            with self.assertRaisesRegex(ValueError, 'run inventory'):
                observe.collect_runs('/cli')

    def test_cli_failures_are_preserved_verbatim(self):
        with patch('orca_runtime.observe.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'terminal_handle_stale')):
            with self.assertRaisesRegex(RuntimeError, 'terminal_handle_stale'):
                observe.query('/cli', ['status'])

    def test_invalid_json_is_an_error(self):
        with patch('orca_runtime.observe.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'not json', '')):
            with self.assertRaises(ValueError):
                observe.query('/cli', ['status'])

    def test_error_envelope_is_not_success(self):
        with patch('orca_runtime.observe.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '{"ok":false,"error":{"code":"offline"}}', '')):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                observe.query('/cli', ['status'])

    def test_pages_are_collected_without_mutating_orca(self):
        first = {'workers': [worker()], 'page': {'hasMore': True, 'nextCursor': 'next'}}
        last = {'workers': [], 'page': {'hasMore': False, 'nextCursor': None}}
        with patch('orca_runtime.observe.query', side_effect=[first, last]) as query:
            self.assertEqual(len(observe.collect_workers('/cli')), 1)
        self.assertIn('--cursor', query.call_args.args[1])
        self.assertTrue(all('worker-list' in call.args[1] for call in query.call_args_list))

    def test_missing_pagination_is_incompatible_not_empty(self):
        with patch('orca_runtime.observe.query', return_value={'workers': []}):
            with self.assertRaisesRegex(ValueError, 'pagination'):
                observe.collect_workers('/cli')

    def test_cursor_loop_is_rejected(self):
        page = {'workers': [], 'page': {'hasMore': True, 'nextCursor': 'same'}}
        with patch('orca_runtime.observe.query', return_value=page):
            with self.assertRaisesRegex(ValueError, 'cursor'):
                observe.collect_workers('/cli')

    def test_partial_host_failure_is_not_a_complete_inventory(self):
        page = {'workers': [], 'page': {'hasMore': False}, 'partialHostErrors': [{'code': 'host_unavailable'}]}
        with patch('orca_runtime.observe.query', return_value=page):
            with self.assertRaisesRegex(RuntimeError, 'host_unavailable'):
                observe.collect_workers('/cli')

    def test_collect_uses_only_current_read_commands(self):
        replies = [inventory()['runtime'], {'worktrees': []}, {'terminals': []}]
        with patch('orca_runtime.observe.query', side_effect=replies) as query, patch('orca_runtime.observe.collect_workers', return_value=[]), patch('orca_runtime.observe.collect_runs', return_value=[]):
            self.assertEqual(observe.collect('/cli')['workers'], [])
        self.assertEqual([c.args[1] for c in query.call_args_list], [['status'], ['worktree', 'ps'], ['terminal', 'list']])

    def test_query_requires_a_result_object(self):
        reply = subprocess.CompletedProcess([], 0, '{"ok":true,"result":[]}', '')
        with patch('orca_runtime.observe.subprocess.run', return_value=reply):
            with self.assertRaises(ValueError):
                observe.query('/cli', ['status'])

    def test_query_timeout_propagates(self):
        with patch('orca_runtime.observe.subprocess.run', side_effect=subprocess.TimeoutExpired('cli', 30)):
            with self.assertRaises(subprocess.TimeoutExpired):
                observe.query('/cli', ['status'])


if __name__ == '__main__':
    unittest.main()
