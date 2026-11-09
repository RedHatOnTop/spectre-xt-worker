"""A native validation refusal recovers only through its original durable request."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import assignment, execution, worker_launch, worker_pool, worker_recovery


class WorkerRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = execution.private_directory(Path(self.temp.name) / 'launch')
        self.request = {'launch_id': 'launch_1', 'run_id': 'run_1', 'task_id': 'task_1',
                        'agent': 'dsh-worker-go', 'worktree_id': 'repo::/worker'}
        self.owner = {'handle': 'term_coordinator', 'pane': 'pane', 'incarnationId': 'process_1'}
        self.command = ['/exact/orca', 'orchestration', 'worker-start', '--task', 'task_1']
        self.intent = {'request': self.request, 'owner': self.owner, 'command': self.command,
                       'native_agent': 'freebuff', 'observer': {}}
        self.response = {'error': {'code': 'task_not_startable', 'data': {
            'orchestrationRequestId': 'request_1', 'originalCommand': self.command}}}
        execution.persist(self.folder, 'intent.json', self.intent)
        self.report = {'state': 'outcome_unknown', 'native_response': self.response}
        execution.persist(self.folder, 'receipt.json', {
            'request_sha256': worker_launch.request_digest(self.request), 'report': self.report})

    def test_original_refusal_retains_bytes_and_replays_only_its_exact_native_id(self):
        original = (self.folder / 'receipt.json').read_bytes()
        ready = {'runId': 'run_1', 'taskId': 'task_1', 'dispatchId': 'ctx_1', 'state': 'ready',
                 'launch': {'effective': {'agent': 'freebuff'}}, 'mutation': {'requestId': 'request_1'},
                 'effects': [{'kind': 'terminal', 'role': 'agent', 'id': 'term_1'}]}
        with patch.object(worker_recovery.session_execution, 'absent'), \
                patch.object(worker_launch.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps({'ok': True, 'result': ready}), '')) as start:
            result = worker_recovery.replay(self.folder, self.intent, self.report, 'request_1')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(start.call_args.args[0], [*self.command, '--retry-request', 'request_1'])
        self.assertEqual((self.folder / 'receipt.json').read_bytes(), original)
        self.assertEqual(worker_launch.prior(self.folder, self.request)['state'], 'ready')

    def test_foreign_request_command_and_unknown_transport_cannot_replay(self):
        for changed in ({'orchestrationRequestId': 'foreign'}, {'originalCommand': ['/other/orca']}):
            report = {**self.report, 'native_response': {'error': {**self.response['error'],
                'data': {**self.response['error']['data'], **changed}}}}
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                worker_recovery.refusal(self.intent, report, 'request_1')
        with self.assertRaises(ValueError):
            worker_recovery.refusal(self.intent, {**self.report, 'native_response': None}, 'request_1')

    def test_a_live_original_observer_prevents_a_second_recovery_start(self):
        with patch.object(worker_recovery.session_execution, 'absent', side_effect=ValueError('observer still live')), \
                patch.object(worker_launch.subprocess, 'run') as start, self.assertRaises(ValueError):
            worker_recovery.replay(self.folder, self.intent, self.report, 'request_1')
        start.assert_not_called()

    def test_retained_recovery_never_mutates_while_native_request_is_pending(self):
        root, current = self.prepare_pool()
        with patch.object(worker_pool, 'owner', return_value=current), \
                patch.object(worker_pool.observe, 'query', return_value={'requestId': 'request_1', 'state': 'pending'}), \
                patch.object(worker_recovery, 'replay') as replay, self.assertRaisesRegex(ValueError, 'pending work stays owned'):
            worker_pool.recover(root, '/orca', self.policy(), 'run_1',
                                'launch_1', 'request_1', {}, '/coordinator')
        replay.assert_not_called()

    def policy(self):
        return {'schema_version': 1, 'max_workers': 2, 'agents': ['dsh-worker-go']}

    def prepare_pool(self):
        root = execution.private_directory(Path(self.temp.name) / 'pool')
        run = execution.private_directory(root / 'run_1')
        directory = execution.private_directory(run / 'launch_1')
        request = {**self.request, 'terminal': 'term_1', 'terminal_incarnation': 'process_1', 'reuse_of': 'ctx_old'}
        intent = {**self.intent, 'request': request}
        execution.persist(directory, 'intent.json', intent)
        execution.persist(directory, 'receipt.json', {
            'request_sha256': worker_launch.request_digest(request), 'report': self.report})
        return root, assignment.coordinator_identity(self.owner)

    def test_managed_recovery_rechecks_custody_and_routes_only_the_original_operation(self):
        root, current = self.prepare_pool()
        row = {'runId': 'run_1', 'dispatchId': 'ctx_old', 'agentTerminalHandle': 'term_1',
               'resource': {'terminalHandle': 'term_1', 'endpointId': 'endpoint_1', 'worktreeId': 'repo::/worker'}}
        proof = {'endpointId': 'endpoint_1', 'handle': 'term_1'}
        with patch.object(worker_pool, 'owner', return_value=current), \
                patch.object(worker_pool.observe, 'query', return_value={'requestId': 'request_1', 'state': 'absent'}), \
                patch.object(worker_pool.observe, 'collect_workers', return_value=[row]), \
                patch.object(worker_pool.worker_reuse, 'proof', return_value=proof), \
                patch.object(worker_pool, 'ready_task') as ready, \
                patch.object(worker_recovery, 'replay', return_value={'state': 'ready'}) as replay:
            result = worker_pool.recover(root, '/orca', self.policy(), 'run_1', 'launch_1',
                                         'request_1', {}, '/coordinator')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(ready.call_args.args[1]['reuse_of'], 'ctx_old')
        self.assertEqual(replay.call_args.args[-1], 'request_1')

    def test_timeout_and_changed_native_request_are_retained_without_reconciliation(self):
        with patch.object(worker_recovery.session_execution, 'absent'), \
                patch.object(worker_launch.subprocess, 'run', side_effect=subprocess.TimeoutExpired('/exact/orca', 90)):
            result = worker_recovery.replay(self.folder, self.intent, self.report, 'request_1')
        self.assertEqual(result['state'], 'outcome_unknown')
        self.assertFalse((self.folder / 'reconciliation.json').exists())
        self.assertTrue((self.folder / 'request_1.recovery-receipt.json').exists())
