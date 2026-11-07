"""Native bootstrap preserves authority, real process state, and failed receipts."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import execution, worker_launch, worker_pool
from superagent_queue import worker_receipt, worker_status, go_native

REQUEST = {'launch_id': 'launch_1', 'run_id': 'run_1', 'task_id': 'task_1',
           'agent': 'dsh-worker-go', 'worktree_id': 'repo::/work'}
CALLER = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'tab_1:leaf_1',
          'ORCA_WORKTREE_ID': 'repo::/work'}
BINDING = {'run_id': 'run_1', 'task_id': 'task_1', 'dispatch_id': 'ctx_1',
           'terminal_handle': 'term_1', 'pane_key': 'tab_1:leaf_1', 'worktree_id': 'repo::/work',
           'workspace_path': '/work', 'incarnation_id': 'process_1', 'route': 'opencode-go',
           'packet_sha256': 'a' * 64}


def receipt(state='ready'):
    return {'runId': 'run_1', 'taskId': 'task_1', 'dispatchId': 'ctx_1', 'state': state,
            'launch': {'requested': {'agent': 'freebuff'}, 'effective': {'agent': 'freebuff'}},
            'mutation': {'requestId': 'request_1'},
            'effects': [{'kind': 'terminal', 'role': 'agent', 'id': 'term_1', 'action': 'created'},
                        {'kind': 'worktree', 'id': 'repo::/work', 'action': 'reused'}]}


class BootstrapReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_unique_exact_spare_override_is_an_alias_not_another_agent(self):
        overrides = {'freebuff': '/home/user/bin/dsh-worker-go', 'codex': 'codex'}
        before = dict(overrides)
        self.assertEqual(worker_receipt.alias_agent('dsh-worker-go', overrides, Path('/home/user/bin')), 'freebuff')
        self.assertEqual(overrides, before)
        for values in ({'codex': '/home/user/bin/dsh-worker-go'},
                       {'freebuff': '/other/dsh-worker-go'},
                       {'freebuff': '/home/user/bin/dsh-worker-go --unsafe'},
                       {**overrides, 'jcode': '/home/user/bin/dsh-worker-go'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                worker_receipt.alias_agent('dsh-worker-go', values, Path('/home/user/bin'))

    def test_clinepass_alias_does_not_confuse_the_go_alias_with_extra_arguments(self):
        values = {'jcode': '/home/user/bin/dsh-worker', 'freebuff': '/home/user/bin/dsh-worker-go'}
        self.assertEqual(worker_receipt.alias_agent('dsh-worker', values, Path('/home/user/bin')), 'jcode')
        self.assertEqual(worker_receipt.alias_agent('dsh-worker-go', values, Path('/home/user/bin')), 'freebuff')

    def test_receipt_checks_exact_native_agent_scope_effects_and_request(self):
        self.assertTrue(worker_receipt.matches(REQUEST, receipt(), 'freebuff'))
        for changed in ({'taskId': 'foreign'}, {'runId': 'foreign'}, {'state': 'starting'},
                        {'mutation': {}}, {'effects': []},
                        {'launch': {'effective': {'agent': 'codex'}}}):
            with self.subTest(changed=changed):
                self.assertFalse(worker_receipt.matches(REQUEST, {**receipt(), **changed}, 'freebuff'))
        self.assertFalse(worker_receipt.matches(REQUEST, receipt(), 'jcode'))

    def test_full_native_start_records_alias_proof_and_exact_retry_link(self):
        request = {**REQUEST, 'retry_of': 'ctx_old'}
        output = subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'result': receipt()}), '')
        with patch.object(worker_receipt, 'resolve_native', return_value='freebuff'), \
                patch.object(worker_launch.subprocess, 'run', return_value=output) as start:
            report = worker_launch.submit(self.root, '/exact/orca', request, {'handle': 'coordinator_1'}, 0)
        self.assertEqual(report['state'], 'ready')
        command = start.call_args.args[0]
        self.assertIn('--full', command)
        self.assertEqual(command[command.index('--retry-of') + 1], 'ctx_old')
        intent = json.loads((self.root / 'launch_1/intent.json').read_text())
        self.assertEqual(intent['native_agent'], 'freebuff')

    def test_completed_failure_reconciles_without_start_or_rewriting_old_response(self):
        folder = execution.private_directory(self.root / 'launch_1')
        owner = {'handle': 'coordinator_1'}
        execution.persist(folder, 'intent.json', {'request': REQUEST, 'owner': owner})
        old = {'request_sha256': worker_launch.request_digest(REQUEST),
               'report': {'state': 'outcome_unknown', 'dispatch_id': None}}
        execution.persist(folder, 'receipt.json', old)
        before = (folder / 'receipt.json').read_bytes()
        record = {'requestId': 'request_1', 'method': 'orchestration.workerStart',
                  'state': 'completed', 'receipt': receipt('failed')}
        with patch('orca_runtime.observe.query', return_value=record), \
                patch.object(worker_receipt, 'resolve_native', return_value='freebuff'), \
                patch.object(worker_launch.subprocess, 'run') as start:
            result = worker_launch.reconcile(folder, '/exact/orca', REQUEST, 'request_1', owner)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['dispatch_id'], 'ctx_1')
        self.assertFalse(result['native_start'])
        self.assertEqual((folder / 'receipt.json').read_bytes(), before)
        start.assert_not_called()
        self.assertEqual(worker_launch.prior(folder, REQUEST)['state'], 'failed')

    def test_failed_native_receipt_does_not_release_an_unverified_terminal(self):
        value = worker_pool.occupancy([{'runId': 'run_1', 'dispatchId': 'ctx_1',
            'dispatchStatus': 'failed', 'terminalState': 'reclaimable',
            'resource': {'terminalHandle': 'term_1', 'worktreeId': 'repo::/work',
                         'releaseState': 'not_requested'}}], 'run_1')
        self.assertEqual(len(value), 1)

    def test_optional_retry_identity_is_validated_without_relaxing_policy(self):
        policy = {'schema_version': 1, 'max_workers': 2, 'agents': ['dsh-worker-go']}
        worker_pool.validate_request({**REQUEST, 'retry_of': 'ctx_old'}, policy)
        for request in ({**REQUEST, 'retry_of': ''}, {**REQUEST, 'unknown': True}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                worker_pool.validate_request(request, policy)


class BootstrapStatusTest(unittest.TestCase):
    def test_idle_reader_emits_actual_native_state_without_task_completion_claims(self):
        data = worker_status.frame('freebuff', {'state': 'ready'}, False)
        payload = json.loads(data.removeprefix('\x1b]9999;').removesuffix('\x07'))
        self.assertEqual(payload['state'], 'done')
        self.assertEqual(payload['agentType'], 'freebuff')
        self.assertNotIn('outcome', payload)
        self.assertNotIn('turnCompletedAt', payload)
        self.assertNotIn('model', payload)

    def test_live_child_cannot_be_reported_idle_even_on_ready_transition(self):
        data = worker_status.frame('freebuff', {'state': 'ready'}, True)
        self.assertIn('"state": "working"', data)
        self.assertEqual(worker_status.frame(None, {'state': 'ready'}, False), '')
        self.assertEqual(worker_status.frame('freebuff', {'state': 'input_recorded'}, False), '')
        with self.assertRaises(ValueError):
            worker_status.frame('codex', {'state': 'ready'}, False)

    def test_native_identity_is_bound_to_current_validated_pane(self):
        with patch.object(worker_status.assignment, 'current_terminal',
                          return_value={'agentIdentity': 'freebuff'}) as read:
            self.assertEqual(worker_status.bind('/exact/orca', CALLER), 'freebuff')
        read.assert_called_once()
        with patch.object(worker_status.assignment, 'current_terminal',
                          return_value={'agentIdentity': 'codex'}), self.assertRaises(ValueError):
            worker_status.bind('/exact/orca', CALLER)


class BootstrapGoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binding = {**BINDING, 'workspace_path': str(self.root),
                        'worktree_id': 'repo::' + str(self.root)}

    def test_existing_go_cli_is_pinned_without_changing_auth_or_default_profile(self):
        command = go_native.command(Path('/trusted/opencode'), Path('/private/packet'), None)
        self.assertTrue(command[2].startswith('Execute the attached'))
        self.assertEqual(command[-1], '/private/packet')
        self.assertIn('opencode-go/glm-5.3-flash', command)
        self.assertIn('--pure', command)
        self.assertNotIn('--auto', command)
        self.assertNotIn('--continue', command)
        resumed = go_native.command(Path('/trusted/opencode'), Path('/private/packet'), 'ses_123')
        self.assertEqual(resumed[resumed.index('--session') + 1], 'ses_123')

    def test_actual_json_session_and_exit_are_private_and_not_task_success(self):
        binary = self.root / 'opencode'
        binary.write_text('#!' + sys.executable + '\nimport json\n'
            'print(json.dumps({"type":"step_start","sessionID":"ses_123"}))\n'
            'print(json.dumps({"type":"text","sessionID":"ses_123","part":{"text":"ok"}}))\n'
            'raise SystemExit(7)\n')
        binary.chmod(0o755)
        packet = self.root / 'packet.txt'
        packet.write_text('Exact native fixture packet.\n')
        packet.chmod(0o600)
        root = self.root / 'executions'
        with patch.object(go_native.assignment, 'confirm'), contextlib.redirect_stdout(io.StringIO()):
            result = go_native.execute(root, self.binding, {}, packet, '/exact/orca', binary)
        self.assertEqual(result['returncode'], 7)
        self.assertEqual(result['session_id'], 'ses_123')
        stored = execution.inspect(root, 'ctx_1')
        self.assertEqual(stored['observation'], 'exited')
        self.assertEqual((root / 'ctx_1/session.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            go_native.execute(root, self.binding, {}, packet, '/exact/orca', binary)

    def test_session_mismatch_and_missing_retry_history_fail_closed(self):
        with self.assertRaises(ValueError):
            go_native.session_id({'sessionID': 'unsafe/session'})
        with self.assertRaises((ValueError, FileNotFoundError)):
            go_native.prepare(self.root / 'executions', self.binding,
                              {'retry_of_dispatch_id': 'ctx_missing'}, '/exact/orca')


if __name__ == '__main__':
    unittest.main()
