"""Only an exited native-linked attempt can resume the same durable session."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import execution, session_execution

BINDING = {'run_id': 'run_1', 'task_id': 'task_1', 'dispatch_id': 'dispatch_1',
           'terminal_handle': 'term_1', 'pane_key': 'tab_1:leaf_1', 'worktree_id': 'repo::/work',
           'workspace_path': '/work', 'incarnation_id': 'pane_process_1',
           'route': 'clinepass', 'packet_sha256': 'a' * 64}


class SessionExecutionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'executions'
        self.next = {**BINDING, 'dispatch_id': 'dispatch_2'}
        self.dispatch = {'retry_of_dispatch_id': 'dispatch_1'}

    def original(self):
        plan = session_execution.prepare(self.root, BINDING, {})
        result = execution.run(self.root, BINDING, ['/bin/sh', '-c', 'exit 7'], session=plan)
        execution.persist(self.root / 'dispatch_1', 'session.json.claim',
                          {'session_id': plan['session_id']})
        execution.persist(self.root / 'dispatch_1', 'session.json', {
            'session_id': plan['session_id'], 'resumed': False, 'provider': 'clinepass',
            'model': 'cline-pass/deepseek-v4.1-flash-1017', 'workspace_path': '/work',
            'first_seq': 0, 'pending_input_ids': [], 'runtime': result['child']})
        return result

    def test_first_attempt_has_stable_immutable_task_identity(self):
        before = deepcopy(BINDING)
        plan = session_execution.prepare(self.root, BINDING, {})
        self.assertEqual(plan['session_id'], session_execution.session_id(BINDING))
        self.assertFalse(plan['resume'])
        self.assertEqual(plan['root_dispatch_id'], 'dispatch_1')
        self.assertEqual(BINDING, before)
        self.assertNotEqual(plan['session_id'], session_execution.session_id({**BINDING, 'task_id': 'task_2'}))

    def test_actual_failure_and_native_retry_link_preserve_session(self):
        result = self.original()
        plan = session_execution.prepare(self.root, self.next, self.dispatch)
        self.assertTrue(plan['resume'])
        self.assertEqual(plan['session_id'], result['session']['session_id'])
        self.assertEqual(plan['retry_of_dispatch_id'], 'dispatch_1')
        self.assertEqual(plan['root_dispatch_id'], 'dispatch_1')

    def test_native_retry_can_create_only_when_original_never_claimed_a_session(self):
        plan = session_execution.prepare(self.root, BINDING, {})
        execution.run(self.root, BINDING, ['/bin/false'], session=plan)
        with patch.object(execution, 'process', side_effect=FileNotFoundError):
            retried = session_execution.prepare(self.root, self.next, self.dispatch)
        self.assertFalse(retried['resume'])
        self.assertEqual(retried['session_id'], plan['session_id'])
        self.assertEqual(retried['retry_of_dispatch_id'], 'dispatch_1')
        self.assertEqual(retried['root_dispatch_id'], 'dispatch_1')
        execution.persist(self.root / 'dispatch_1', 'session.json.claim',
                          {'session_id': plan['session_id']})
        with patch.object(execution, 'process', side_effect=FileNotFoundError), \
                self.assertRaisesRegex(ValueError, 'session claim'):
            session_execution.prepare(self.root, self.next, self.dispatch)
        record = execution.inspect(self.root, 'dispatch_1')
        record = {**record, 'session': {**record['session'], 'resume': True}}
        with patch.object(execution, 'inspect', return_value=record), \
                self.assertRaisesRegex(ValueError, 'resumed session'):
            session_execution.prepare(self.root, self.next, self.dispatch)

    def test_live_unknown_reused_pid_and_boot_change_never_authorize_resume(self):
        result = self.original()
        for change in ({'observation': 'live'}, {'observation': 'unverifiable'},
                       {'observer': {**result['observer'], 'boot_id': 'older-boot'}}):
            record = {**execution.inspect(self.root, 'dispatch_1'), **change}
            with self.subTest(change=change), patch.object(execution, 'inspect', return_value=record), \
                    self.assertRaises(ValueError):
                session_execution.prepare(self.root, self.next, self.dispatch)
        current = execution.process(os.getpid())
        with patch.object(execution, 'process', return_value=current), self.assertRaises(ValueError):
            session_execution.prepare(self.root, self.next, self.dispatch)

    def test_scope_route_packet_and_receipt_mismatch_are_refused(self):
        self.original()
        for change in ({'run_id': 'run_2'}, {'task_id': 'task_2'}, {'workspace_path': '/other'},
                       {'worktree_id': 'other::/work'}, {'route': 'opencode-go'}, {'packet_sha256': 'b' * 64}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                session_execution.prepare(self.root, {**self.next, **change}, self.dispatch)
        (self.root / 'dispatch_1/session.json').unlink()
        with self.assertRaises(ValueError):
            session_execution.prepare(self.root, self.next, self.dispatch)

    def test_old_non_session_attempt_and_self_retry_are_refused(self):
        execution.run(self.root, BINDING, ['/bin/false'])
        with self.assertRaises(ValueError):
            session_execution.prepare(self.root, self.next, self.dispatch)
        with self.assertRaises(ValueError):
            session_execution.prepare(self.root, BINDING, self.dispatch)

    def test_new_dispatch_without_native_retry_link_never_recreates_same_task_session(self):
        self.original()
        with self.assertRaisesRegex(ValueError, 'native retry link'):
            session_execution.prepare(self.root, self.next, {})


if __name__ == '__main__':
    unittest.main()
