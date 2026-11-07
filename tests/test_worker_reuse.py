"""Reuse requires exact native custody and completed OS execution evidence."""
from pathlib import Path
import hashlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import worker_pool, worker_receipt, worker_reuse

REQUEST = {'launch_id': 'launch_1', 'run_id': 'run_1', 'task_id': 'task_new',
           'agent': 'dsh-worker-go', 'worktree_id': 'repo::/worker',
           'terminal': 'term_1', 'reuse_of': 'ctx_old', 'terminal_incarnation': 'process_1'}
TERMINAL = {'handle': 'term_1', 'worktreeId': 'repo::/worker', 'worktreePath': '/worker',
            'ptyId': 'pty_1', 'incarnationId': 'process_1', 'tabId': 'tab_1', 'leafId': 'leaf_1',
            'agentIdentity': 'freebuff', 'connected': True, 'writable': True, 'executionHostId': 'local'}
ROW = {'runId': 'run_1', 'dispatchId': 'ctx_old', 'taskId': 'task_old',
       'dispatchStatus': 'completed', 'workerState': 'succeeded', 'terminalState': 'retained',
       'agentTerminalHandle': 'term_1', 'resource': {'terminalHandle': 'term_1',
       'worktreeId': 'repo::/worker', 'ownershipState': 'owned', 'releaseState': 'retained',
       'ownerDispatchId': 'ctx_old', 'endpointId': 'endpoint_1', 'endpointIncarnation': 'pty_1:process_1'}}
RECORD = {'observation': 'exited', 'returncode': 0, 'error': None, 'observer': {},
          'binding': {'run_id': 'run_1', 'task_id': 'task_old', 'dispatch_id': 'ctx_old',
          'terminal_handle': 'term_1', 'worktree_id': 'repo::/worker', 'incarnation_id': 'process_1',
          'pane_key': 'tab_1:leaf_1', 'route': 'opencode-go'}}


class WorkerReuseTest(unittest.TestCase):
    def proof(self, rows=None, terminal=None, record=None):
        with patch.object(worker_reuse.observe, 'query', return_value={'terminal': terminal or TERMINAL}), \
                patch.object(worker_reuse.worker_receipt, 'resolve_native', return_value='freebuff'), \
                patch.object(worker_reuse.execution, 'inspect', return_value=record or RECORD), \
                patch.object(worker_reuse.session_execution, 'exited') as exited, \
                patch.object(worker_reuse.session_execution, 'absent') as absent:
            proof = worker_reuse.proof('/exact/orca', REQUEST, [ROW] if rows is None else rows)
        return proof, exited, absent

    def test_settled_native_owner_and_actual_exit_preserve_the_existing_reader(self):
        proof, exited, absent = self.proof()
        self.assertEqual(proof['handle'], 'term_1')
        self.assertEqual(proof['incarnationId'], 'process_1')
        exited.assert_called_once_with(RECORD)
        absent.assert_called_once_with(RECORD['observer'])

    def test_live_foreign_or_unverified_ownership_cannot_be_reused(self):
        changes = ({'dispatchStatus': 'dispatched'}, {'workerState': 'failed'},
                   {'taskId': 'other'}, {'resource': {**ROW['resource'], 'ownerDispatchId': 'other'}},
                   {'resource': {**ROW['resource'], 'endpointIncarnation': 'pty_1:other'}},
                   {'resource': {**ROW['resource'], 'releaseState': 'release_unknown'}})
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.proof(rows=[{**ROW, **changed}])
        for changed in ({'incarnationId': 'other'}, {'agentIdentity': 'codex'}, {'connected': False}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.proof(terminal={**TERMINAL, **changed})
        with self.assertRaises(ValueError):
            self.proof(rows=[ROW, {**ROW, 'dispatchId': 'ctx_busy', 'dispatchStatus': 'dispatched'}])
        with self.assertRaises(ValueError):
            self.proof(record={**RECORD, 'error': 'retained error'})

    def test_actual_live_child_or_executor_blocks_reuse(self):
        with patch.object(worker_reuse.observe, 'query', return_value={'terminal': TERMINAL}), \
                patch.object(worker_reuse.worker_receipt, 'resolve_native', return_value='freebuff'), \
                patch.object(worker_reuse.execution, 'inspect', return_value=RECORD), \
                patch.object(worker_reuse.session_execution, 'exited', side_effect=ValueError('child still present')), \
                self.assertRaisesRegex(ValueError, 'child still present'):
            worker_reuse.proof('/exact/orca', REQUEST, [ROW])

    def test_ready_timestamp_requires_the_exact_released_failed_start(self):
        request = {**REQUEST, 'retry_of': 'ctx_failed'}
        task = {'completed_at': 'recorded-time', 'result': None}
        value = {'dispatch': {'id': 'ctx_failed', 'runId': 'run_1', 'taskId': 'task_new',
                             'status': 'failed', 'completedAt': 'recorded-time', 'failureCount': 1},
                 'worker': {'stage': 'agent_readiness', 'worktreeId': 'repo::/worker'},
                 'terminalResource': {'ownershipState': 'released', 'releaseState': 'released'}}
        with patch.object(worker_reuse.observe, 'query', return_value=value):
            self.assertTrue(worker_reuse.reviewed_retry('/orca', request, task))
            self.assertFalse(worker_reuse.reviewed_retry('/orca', REQUEST, task))
            self.assertFalse(worker_reuse.reviewed_retry('/orca', request, {**task, 'result': 'evidence'}))
        latest = {'dispatch': {'id': 'ctx_failed', 'status': 'failed', 'run_id': 'run_1', 'task_id': 'task_new'}}
        with patch.object(worker_reuse.observe, 'query', side_effect=[value, latest]):
            self.assertTrue(worker_reuse.reviewed_retry('/orca', request,
                {**task, 'status': 'failed', 'completed_at': 'operator-state-transition'}))
        changes = ({'taskId': 'foreign'}, {'completedAt': 'other'}, {'failureCount': 3}, {'failureCount': True})
        for changed in changes:
            with self.subTest(changed=changed), patch.object(worker_reuse.observe, 'query',
                    return_value={**value, 'dispatch': {**value['dispatch'], **changed}}):
                self.assertFalse(worker_reuse.reviewed_retry('/orca', request, task))

    def test_stopped_execution_retry_requires_original_spec_scope_and_go_session(self):
        request = {**REQUEST, 'retry_of': 'ctx_old'}
        task = {'status': 'blocked', 'spec': 'Exact original Task.\n', 'result': None}
        value = {'dispatch': {'id': 'ctx_old', 'runId': 'run_1', 'taskId': 'task_new',
                             'status': 'failed', 'failureCount': 1},
                 'worker': {'state': 'stopped', 'stage': 'process_stopped', 'worktreeId': 'repo::/worker'},
                 'terminalResource': {'releaseState': 'released', 'ownershipState': 'released'}}
        latest = {'dispatch': {'id': 'ctx_old', 'status': 'failed', 'run_id': 'run_1', 'task_id': 'task_new'}}
        record = {**RECORD, 'binding': {**RECORD['binding'], 'task_id': 'task_new',
                  'packet_sha256': hashlib.sha256(task['spec'].encode()).hexdigest()}}
        with patch.object(worker_reuse.observe, 'query', side_effect=[value, latest]), \
                patch.object(worker_reuse.execution, 'inspect', return_value=record), \
                patch.object(worker_reuse.session_execution, 'exited') as exited, \
                patch.object(worker_reuse.session_execution, 'absent') as absent, \
                patch.object(worker_reuse.go_native, 'prepare', return_value={'resume': True, 'session_id': 'ses_original'}) as session:
            self.assertTrue(worker_reuse.reviewed_retry('/orca', request, task))
        exited.assert_called_once_with(record)
        absent.assert_called_once_with(record['observer'])
        self.assertEqual(session.call_args.args[2]['retry_of_dispatch_id'], 'ctx_old')
        with patch.object(worker_reuse.observe, 'query', side_effect=[value, latest]), \
                patch.object(worker_reuse.execution, 'inspect', return_value=record), \
                patch.object(worker_reuse.go_native, 'prepare') as session:
            self.assertFalse(worker_reuse.reviewed_retry('/orca', request, {**task, 'spec': 'Changed Task.'}))
        session.assert_not_called()

    def test_reuse_fields_are_all_or_nothing_and_bound_to_identifiers(self):
        policy = {'schema_version': 1, 'max_workers': 2, 'agents': ['dsh-worker-go']}
        worker_pool.validate_request(REQUEST, policy)
        for key in ('terminal', 'reuse_of', 'terminal_incarnation'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker_pool.validate_request({name: value for name, value in REQUEST.items() if name != key}, policy)
        with self.assertRaises(ValueError):
            worker_pool.validate_request({**REQUEST, 'terminal_incarnation': ''}, policy)

    def test_reused_receipt_cannot_claim_a_new_or_replaced_terminal(self):
        value = {'runId': 'run_1', 'taskId': 'task_new', 'dispatchId': 'ctx_new', 'state': 'ready',
                 'mutation': {'requestId': 'request_1'}, 'launch': None,
                 'prompt': {'processIncarnation': 'process_1'}, 'effects': [
                     {'kind': 'worktree', 'id': 'repo::/worker'},
                     {'kind': 'terminal', 'role': 'agent', 'id': 'term_1', 'action': 'reused'}]}
        self.assertTrue(worker_receipt.matches(REQUEST, value, 'freebuff'))
        self.assertTrue(worker_receipt.matches(REQUEST, {**value, 'launch': {
            'effective': {'agent': None}, 'requested': {'agent': None}}}, 'freebuff'))
        self.assertFalse(worker_receipt.matches(REQUEST, {**value, 'launch': {
            'effective': {'agent': 'codex'}, 'requested': None}}, 'freebuff'))
        self.assertFalse(worker_receipt.matches(REQUEST, {**value, 'prompt': None}, 'freebuff'))
        self.assertFalse(worker_receipt.matches(REQUEST, {**value, 'effects': value['effects'][1:]}, 'freebuff'))
        self.assertFalse(worker_receipt.matches(REQUEST, {**value, 'prompt': {'processIncarnation': 'other'}}, 'freebuff'))
        self.assertFalse(worker_receipt.matches(REQUEST, {**value, 'effects': [
            *value['effects'][:-1], {**value['effects'][-1], 'action': 'created'}]}, 'freebuff'))

    def test_managed_reuse_keeps_capacity_and_never_starts_another_process(self):
        from tests.test_harness_worker_pool import WorkerPoolTest
        fixture = WorkerPoolTest()
        fixture.setUp()
        self.addCleanup(fixture.directory.cleanup)
        request = {**REQUEST, 'worktree_id': fixture.request['worktree_id'], 'agent': 'dsh-worker'}
        old = {**ROW, 'resource': {**ROW['resource'], 'worktreeId': request['worktree_id']}}
        fixture.workers = [old]
        fixture.ready = {**fixture.ready, 'launch': None, 'prompt': {'processIncarnation': 'process_1'},
                         'effects': [{'kind': 'worktree', 'id': request['worktree_id']},
                                     {'kind': 'terminal', 'role': 'agent', 'id': 'term_1', 'action': 'reused'}]}
        proof = {**TERMINAL, 'endpointId': 'endpoint_1'}
        with patch.object(worker_reuse, 'proof', return_value=proof):
            result, calls = fixture.launch(request=request)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['occupied_before'], 1)
        self.assertEqual(len(calls), 1)
        command = calls[0].args[0]
        self.assertIn('--terminal', command)
        self.assertNotIn('--agent', command)
        self.assertNotIn('--model', command)
        self.assertEqual(command[command.index('--terminal') + 1], 'term_1')
        fixture.request = {**request, 'launch_id': 'launch_2'}
        fixture.workers = [old, {**old, 'dispatchId': 'dispatch_new', 'taskId': 'task_new'},
            {**old, 'dispatchId': 'ctx_other', 'agentTerminalHandle': 'term_other',
            'resource': {**old['resource'], 'terminalHandle': 'term_other', 'ownerDispatchId': 'ctx_other'}}]
        with patch.object(worker_reuse, 'proof', return_value=proof):
            result, calls = fixture.launch()
        self.assertEqual(result['state'], 'workspace_wait')
        self.assertEqual(calls, [])

    def test_native_reuse_is_revalidated_before_input(self):
        from tests.test_harness_worker_pool import WorkerPoolTest
        fixture = WorkerPoolTest()
        fixture.setUp()
        self.addCleanup(fixture.directory.cleanup)
        request = {**REQUEST, 'worktree_id': fixture.request['worktree_id'], 'agent': 'dsh-worker'}
        fixture.workers = [{**ROW, 'resource': {**ROW['resource'], 'worktreeId': request['worktree_id']}}]
        with patch.object(worker_reuse, 'proof', side_effect=[{**TERMINAL, 'endpointId': 'endpoint_1'},
                          {**TERMINAL, 'endpointId': 'endpoint_1', 'incarnationId': 'replaced'}]), \
                self.assertRaisesRegex(ValueError, 'identity changed'):
            fixture.launch(request=request)

    def test_reuse_at_capacity_keeps_both_slots_and_excess_capacity_still_blocks(self):
        from tests.test_harness_worker_pool import WorkerPoolTest, worker
        fixture = WorkerPoolTest()
        fixture.setUp()
        self.addCleanup(fixture.directory.cleanup)
        request = {**REQUEST, 'worktree_id': fixture.request['worktree_id'], 'agent': 'dsh-worker'}
        old = {**ROW, 'resource': {**ROW['resource'], 'worktreeId': request['worktree_id']}}
        fixture.workers = [old, worker(2)]
        fixture.ready = {**fixture.ready, 'launch': None, 'prompt': {'processIncarnation': 'process_1'},
            'effects': [{'kind': 'worktree', 'id': request['worktree_id']},
                        {'kind': 'terminal', 'role': 'agent', 'id': 'term_1', 'action': 'reused'}]}
        with patch.object(worker_reuse, 'proof', return_value={**TERMINAL, 'endpointId': 'endpoint_1'}):
            result, calls = fixture.launch(request=request)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['occupied_before'], 2)
        self.assertEqual(len(calls), 1)
        fixture.workers = [*fixture.workers, {**old, 'dispatchId': 'dispatch_new', 'taskId': 'task_new'}, worker(3)]
        with patch.object(worker_reuse, 'proof', return_value={**TERMINAL, 'endpointId': 'endpoint_1'}):
            result, calls = fixture.launch(request={**request, 'launch_id': 'launch_2'})
        self.assertEqual(result['state'], 'capacity_wait')
        self.assertEqual(result['occupied'], 3)
        self.assertEqual(calls, [])
