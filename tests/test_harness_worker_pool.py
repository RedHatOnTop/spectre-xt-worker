"""Native worker policy bounds implementations without copying Orca occupancy."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import worker_launch, worker_pool

POLICY = {'schema_version': 1, 'max_workers': 2, 'agents': ['dsh-worker', 'dsh-worker-go']}
CALLER = {'ORCA_TERMINAL_HANDLE': 'coordinator_1', 'ORCA_PANE_KEY': 'pane_1',
          'ORCA_WORKTREE_ID': 'repo::/coordinator'}
TARGET = {'handle': 'coordinator_1', 'worktreeId': 'repo::/coordinator', 'worktreePath': '/coordinator',
          'incarnationId': 'process_1', 'connected': True, 'writable': True, 'paneKey': 'pane_1'}


def worker(index, state='active', status='dispatched'):
    return {'runId': 'run_1', 'dispatchId': f'dispatch_{index}', 'taskId': f'task_{index}',
            'agentTerminalHandle': f'worker_{index}', 'terminalState': state, 'dispatchStatus': status,
            'resource': {'id': f'resource_{index}', 'terminalHandle': f'worker_{index}',
                         'worktreeId': f'repo::/worker_{index}', 'ownershipState': 'owned',
                         'releaseState': 'not_requested'}}


class WorkerPoolTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'private'
        self.request = {'launch_id': 'launch_1', 'run_id': 'run_1', 'task_id': 'task_new',
                        'agent': 'dsh-worker', 'worktree_id': 'repo::/worker_new'}
        self.workers = []
        self.ready = {'runId': 'run_1', 'taskId': 'task_new', 'dispatchId': 'dispatch_new', 'state': 'ready',
                      'launch': {'effective': {'agent': 'dsh-worker'}},
                      'mutation': {'requestId': 'native_launch_1'}, 'effects': [
                          {'kind': 'terminal', 'role': 'agent', 'id': 'worker_new', 'action': 'created'}]}

    def query(self, cli, args):
        if args[:2] == ['orchestration', 'run-show']:
            if '--full' not in args:
                return {'runId': 'run_1', 'objective': 'concise'}
            return {'run': {'id': 'run_1', 'coordinator_handle': 'coordinator_1', 'legacy': 0}}
        if args[:2] == ['orchestration', 'task-list']:
            if '--full' not in args:
                return {'runId': 'run_1', 'tasks': [{'id': 'task_new', 'status': 'ready', 'title': 'concise'}]}
            return {'runId': 'run_1', 'tasks': [{'id': 'task_new', 'run_id': 'run_1', 'status': 'ready'}]}
        if args[:2] == ['worktree', 'ps']:
            return {'worktrees': [{'worktreeId': 'repo::/coordinator', 'path': '/coordinator'},
                                 {'worktreeId': 'repo::/worker_new', 'path': '/worker_new'}]}
        if args[:2] == ['terminal', 'show']:
            return {'terminal': TARGET}
        raise AssertionError(args)

    def launch(self, request=None, output=None):
        response = output or subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'result': self.ready}), '')
        with patch.object(worker_pool.observe, 'query', side_effect=self.query), \
                patch.object(worker_pool.observe, 'collect_workers', side_effect=lambda *a, **k: self.workers), \
                patch.object(worker_launch.subprocess, 'run', return_value=response) as execute:
            report = worker_pool.start(self.root, '/exact/orca', POLICY, request or self.request, CALLER, '/coordinator')
        return report, execute.call_args_list

    def test_exact_native_start_uses_only_approved_harness_and_run(self):
        report, calls = self.launch()
        self.assertEqual(report['state'], 'ready')
        self.assertEqual(report['dispatch_id'], 'dispatch_new')
        self.assertEqual(len(calls), 1)
        args = calls[0].args[0]
        self.assertEqual(args[args.index('--agent') + 1], 'dsh-worker')
        self.assertEqual(args[args.index('--run') + 1], 'run_1')
        self.assertEqual(args[args.index('--worktree') + 1], 'id:repo::/worker_new')
        self.assertNotIn('--model', args)

    def test_two_existing_workers_block_a_third_without_terminal_mutation(self):
        self.workers = [worker(1), worker(2)]
        report, calls = self.launch()
        self.assertEqual(report['state'], 'capacity_wait')
        self.assertEqual(report['occupied'], 2)
        self.assertEqual(calls, [])
        self.assertFalse(self.root.joinpath('run_1/launch_1/intent.json').exists())

    def test_unknown_active_and_settled_unreleased_terminals_all_consume_slots(self):
        for row in (worker(1), worker(1, 'retained', 'completed'),
                    {'runId': 'run_1', 'dispatchId': 'unknown', 'dispatchStatus': 'dispatched'}):
            self.workers = [row, worker(2)]
            with self.subTest(row=row):
                report, calls = self.launch()
                self.assertEqual(report['state'], 'capacity_wait')
                self.assertEqual(calls, [])

    def test_released_history_and_same_terminal_retry_history_do_not_duplicate_occupancy(self):
        old = {**worker(1), 'dispatchId': 'old_dispatch', 'dispatchStatus': 'failed'}
        released = {**worker(2, 'released', 'completed'), 'resource': {
            **worker(2)['resource'], 'ownershipState': 'released', 'releaseState': 'released'}}
        self.workers = [worker(1), old, released]
        report, calls = self.launch()
        self.assertEqual(report['state'], 'ready')
        self.assertEqual(report['occupied_before'], 1)
        self.assertEqual(len(calls), 1)

    def test_occupied_worktree_refuses_another_implementation_writer(self):
        self.workers = [{**worker(1), 'resource': {**worker(1)['resource'], 'worktreeId': 'repo::/worker_new'}}]
        report, calls = self.launch()
        self.assertEqual(report['state'], 'workspace_wait')
        self.assertEqual(calls, [])

    def test_unknown_workspace_resource_blocks_new_writer_until_native_topology_is_known(self):
        self.workers = [{'runId': 'run_1', 'dispatchId': 'unknown_dispatch', 'dispatchStatus': 'dispatched'}]
        report, calls = self.launch()
        self.assertEqual(report['state'], 'workspace_wait')
        self.assertEqual(calls, [])

    def test_completed_response_replay_is_read_only_across_reopen(self):
        first, calls = self.launch()
        second, replay_calls = self.launch()
        self.assertEqual(first['dispatch_id'], second['dispatch_id'])
        self.assertTrue(second['replayed'])
        self.assertEqual(replay_calls, [])
        self.assertEqual(len(calls), 1)

    def test_lost_response_preserves_intent_and_blocks_duplicate_and_other_launches(self):
        failed = subprocess.CompletedProcess([], 1, '', 'native worker start transport lost verbatim')
        report, calls = self.launch(output=failed)
        self.assertEqual(report['state'], 'outcome_unknown')
        self.assertIn('native worker start transport lost verbatim', report['stderr'])
        again, repeated = self.launch()
        other, other_calls = self.launch(request={**self.request, 'launch_id': 'launch_2'})
        self.assertEqual(again['state'], 'outcome_unknown')
        self.assertEqual(other['state'], 'reconciliation_required')
        self.assertEqual(repeated, [])
        self.assertEqual(other_calls, [])
        self.assertEqual(len(calls), 1)

    def test_invalid_policy_or_route_is_rejected_before_native_queries(self):
        for value in ({**POLICY, 'max_workers': 0}, {**POLICY, 'max_workers': True},
                      {**POLICY, 'agents': ['codex']}, {**POLICY, 'extra': True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worker_pool.validate_policy(value)
        with self.assertRaises(ValueError):
            self.launch(request={**self.request, 'agent': 'codex'})

    def test_folder_terminal_confirms_the_registered_workspace_when_worktree_path_is_empty(self):
        caller = {**CALLER, 'ORCA_PANE_KEY': 'tab_1:leaf_1'}
        folder = {**TARGET, 'worktreePath': '', 'paneKey': None, 'tabId': 'tab_1', 'leafId': 'leaf_1'}

        def query(cli, args):
            if args[:2] == ['terminal', 'show']:
                return {'terminal': folder}
            return self.query(cli, args)

        with patch.object(worker_pool.observe, 'query', side_effect=query), \
                patch.object(worker_pool.observe, 'collect_workers', return_value=[]), \
                patch.object(worker_launch.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps({'ok': True, 'result': self.ready}), '')) as execute:
            report = worker_pool.start(self.root, '/exact/orca', POLICY, self.request, caller, '/coordinator')
        self.assertEqual(report['state'], 'ready')
        self.assertEqual(len(execute.call_args_list), 1)

    def test_concise_ready_task_row_is_not_the_scoped_task(self):
        def query(cli, args):
            if args[:2] == ['orchestration', 'task-list']:
                return {'runId': 'run_1', 'tasks': [{'id': 'task_new', 'status': 'ready', 'title': 'concise'}]}
            return self.query(cli, args)

        with patch.object(worker_pool.observe, 'query', side_effect=query), \
                patch.object(worker_pool.observe, 'collect_workers', return_value=[]), \
                self.assertRaisesRegex(ValueError, 'exact ready native Task'):
            worker_pool.start(self.root, '/orca', POLICY, self.request, CALLER, '/coordinator')

    def test_concise_run_view_is_not_coordinator_ownership(self):
        def query(cli, args):
            if args[:2] == ['orchestration', 'run-show']:
                return {'runId': 'run_1', 'objective': 'concise'}
            return self.query(cli, args)

        with patch.object(worker_pool.observe, 'query', side_effect=query), \
                self.assertRaisesRegex(ValueError, 'scope'):
            worker_pool.start(self.root, '/orca', POLICY, self.request, CALLER, '/coordinator')

    def test_foreign_coordinator_or_unregistered_workspace_cannot_launch(self):
        with patch.object(worker_pool.observe, 'query', return_value={
                'run': {'id': 'run_1', 'coordinator_handle': 'other', 'legacy': 0}}), \
                self.assertRaisesRegex(ValueError, 'coordinator'):
            worker_pool.start(self.root, '/orca', POLICY, self.request, CALLER, '/coordinator')
        def query(cli, args):
            return {'worktrees': [{'worktreeId': 'repo::/coordinator', 'path': '/coordinator'}]} if args[:2] == ['worktree', 'ps'] else self.query(cli, args)
        with patch.object(worker_pool.observe, 'query', side_effect=query), \
                patch.object(worker_pool.observe, 'collect_workers', return_value=[]), \
                self.assertRaisesRegex(ValueError, 'registered'):
            worker_pool.start(self.root, '/orca', POLICY, self.request, CALLER, '/coordinator')

    def test_changed_same_launch_request_cannot_reuse_original_receipt(self):
        self.launch()
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.launch(request={**self.request, 'agent': 'dsh-worker-go'})

    def test_nonvisible_accepted_launch_blocks_a_new_launch_until_native_readback(self):
        self.launch()
        report, calls = self.launch(request={**self.request, 'launch_id': 'launch_2'})
        self.assertEqual(report['state'], 'reconciliation_required')
        self.assertEqual(calls, [])

    def test_observed_first_worker_allows_second_but_observed_two_hold_third(self):
        self.launch()
        self.workers = [{**worker(1), 'dispatchId': 'dispatch_new', 'taskId': 'task_new'}]
        self.ready = {**self.ready, 'dispatchId': 'dispatch_second'}
        second, calls = self.launch(request={**self.request, 'launch_id': 'launch_2'})
        self.assertEqual(second['state'], 'ready')
        self.assertEqual(len(calls), 1)
        self.workers = [*self.workers, {**worker(2), 'dispatchId': 'dispatch_second', 'taskId': 'task_new'}]
        third, calls = self.launch(request={**self.request, 'launch_id': 'launch_3'})
        self.assertEqual(third['state'], 'capacity_wait')
        self.assertEqual(calls, [])

    def test_terminal_handle_across_different_hosts_does_not_collapse_capacity(self):
        first = worker(1)
        second = {**worker(1), 'dispatchId': 'other_dispatch', 'resource': {**worker(1)['resource'], 'endpointId': 'other_host'}}
        self.workers = [first, second]
        report, calls = self.launch()
        self.assertEqual(report['state'], 'capacity_wait')
        self.assertEqual(calls, [])

    def test_crash_before_response_receipt_blocks_later_managed_start(self):
        from superagent_queue import execution
        parent = execution.private_directory(self.root / 'run_1')
        root = execution.private_directory(parent / 'launch_previous')
        execution.persist(root, 'intent.json', {'request': {**self.request, 'launch_id': 'launch_previous'}})
        report, calls = self.launch()
        self.assertEqual(report['state'], 'reconciliation_required')
        self.assertEqual(calls, [])

    def test_native_failure_receipt_keeps_original_recovery_response_verbatim(self):
        value = {'ok': True, 'result': {**self.ready, 'state': 'outcome_unknown',
                                      'recovery': {'argv': ['worker-show', '--dispatch', 'dispatch_new']}}}
        result = subprocess.CompletedProcess([], 1, json.dumps(value), 'native timeout retained resources')
        report, calls = self.launch(output=result)
        self.assertEqual(report['state'], 'outcome_unknown')
        self.assertEqual(report['native_response'], value)
        self.assertEqual(report['stderr'], 'native timeout retained resources')
        self.assertEqual(self.launch()[1], [])

    def test_transport_timeout_is_retained_and_not_retried(self):
        error = subprocess.TimeoutExpired(['/orca'], 90, output=b'partial response', stderr=b'partial error')
        with patch.object(worker_pool.observe, 'query', side_effect=self.query), \
                patch.object(worker_pool.observe, 'collect_workers', return_value=[]), \
                patch.object(worker_launch.subprocess, 'run', side_effect=error):
            report = worker_pool.start(self.root, '/orca', POLICY, self.request, CALLER, '/coordinator')
        self.assertEqual(report['state'], 'outcome_unknown')
        self.assertIn('partial response', report['stdout'])
        self.assertEqual(self.launch()[1], [])

    def test_invalid_policy_schema_and_response_shape_fail_closed(self):
        for value in ({**POLICY, 'schema_version': True}, {**POLICY, 'max_workers': 3},
                      {**POLICY, 'agents': ['dsh-worker', 'dsh-worker']}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worker_pool.validate_policy(value)
        report, calls = self.launch(output=subprocess.CompletedProcess([], 0, 'not json', 'native malformed response'))
        self.assertEqual(report['state'], 'outcome_unknown')
        self.assertEqual(len(calls), 1)

    def test_malformed_native_fields_are_unknown_not_a_crashing_ready_response(self):
        for change in ({'launch': None}, {'mutation': None}, {'dispatchId': ''}, {'effects': []}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                response = {'ok': True, 'result': {**self.ready, **change}}
                report = worker_launch.result(self.request, response, 0, json.dumps(response), '')
                self.assertEqual(report['state'], 'outcome_unknown')

    def test_exact_native_request_reconciliation_appends_proof_without_another_start(self):
        failed = subprocess.CompletedProcess([], 1, '', 'native launch response lost')
        self.launch(output=failed)
        directory = self.root / 'run_1/launch_1'
        original = (directory / 'receipt.json').read_bytes()
        record = {'requestId': 'native_launch_1', 'state': 'completed',
                  'method': 'orchestration.workerStart', 'receipt': self.ready}
        with patch.object(worker_pool.observe, 'query', side_effect=lambda cli, args:
                          record if args[:2] == ['orchestration', 'request-show'] else self.query(cli, args)), \
                patch.object(worker_launch.subprocess, 'run') as start:
            report = worker_pool.reconcile(self.root, '/orca', POLICY, 'run_1', 'launch_1',
                                            'native_launch_1', CALLER, '/coordinator')
        start.assert_not_called()
        self.assertEqual(report['state'], 'ready')
        self.assertEqual((directory / 'receipt.json').read_bytes(), original)
        replay, calls = self.launch()
        self.assertEqual(replay['state'], 'ready')
        self.assertTrue(replay['replayed'])
        self.assertEqual(calls, [])

    def test_foreign_reconciliation_receipt_does_not_clear_native_uncertainty(self):
        self.launch(output=subprocess.CompletedProcess([], 1, '', 'native response lost'))
        record = {'requestId': 'native_launch_1', 'state': 'completed',
                  'method': 'orchestration.workerStart', 'receipt': {**self.ready, 'taskId': 'other_task'}}
        with patch.object(worker_pool.observe, 'query', side_effect=lambda cli, args:
                          record if args[:2] == ['orchestration', 'request-show'] else self.query(cli, args)), \
                self.assertRaisesRegex(ValueError, 'original'):
            worker_pool.reconcile(self.root, '/orca', POLICY, 'run_1', 'launch_1',
                                  'native_launch_1', CALLER, '/coordinator')
        self.assertEqual(self.launch()[0]['state'], 'outcome_unknown')

    def test_native_receipt_recovers_observer_crash_without_wrapper_response_file(self):
        self.launch()
        directory = self.root / 'run_1/launch_1'
        (directory / 'receipt.json').unlink()
        record = {'requestId': 'native_launch_1', 'state': 'completed',
                  'method': 'orchestration.workerStart', 'receipt': self.ready}
        with patch.object(worker_pool.observe, 'query', side_effect=lambda cli, args:
                          record if args[:2] == ['orchestration', 'request-show'] else self.query(cli, args)):
            worker_pool.reconcile(self.root, '/orca', POLICY, 'run_1', 'launch_1',
                                  'native_launch_1', CALLER, '/coordinator')
        report, calls = self.launch()
        self.assertEqual(report['state'], 'ready')
        self.assertEqual(calls, [])
        self.assertFalse((directory / 'receipt.json').exists())

    def test_prior_result_task_is_reviewed_not_reexecuted(self):
        def query(cli, args):
            if args[:2] == ['orchestration', 'task-list']:
                return {'runId': 'run_1', 'tasks': [{'id': 'task_new', 'run_id': 'run_1',
                        'status': 'ready', 'result': '{"outcome":"already_verified"}'}]}
            return self.query(cli, args)
        with patch.object(worker_pool.observe, 'query', side_effect=query), \
                patch.object(worker_pool.observe, 'collect_workers', return_value=[]), \
                self.assertRaisesRegex(ValueError, 'prior result evidence'):
            worker_pool.start(self.root, '/orca', POLICY, self.request, CALLER, '/coordinator')

    def test_concurrent_start_holds_native_mutation_lock(self):
        from superagent_queue import execution, settings
        path = execution.private_directory(self.root / 'run_1')
        with settings.lock(path / 'start.lock'):
            report, calls = self.launch()
        self.assertEqual(report['state'], 'launch_busy')
        self.assertEqual(calls, [])

    def test_policy_and_request_control_data_are_rejected(self):
        for request in ({**self.request, 'worktree_id': 'repo::/work\x00'},
                        {**self.request, 'agent': []}, {**self.request, 'launch_id': '../other'}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.launch(request=request)

    def test_wrong_native_response_identity_stays_uncertain(self):
        self.ready = {**self.ready, 'runId': 'other_run'}
        report, calls = self.launch()
        self.assertEqual(report['state'], 'outcome_unknown')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.launch()[1], [])


if __name__ == '__main__':
    unittest.main()
