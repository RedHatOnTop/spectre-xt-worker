"""Execution receipts preserve native binding and exact OS attempt evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import execution

BINDING = {'run_id': 'run_1', 'task_id': 'task_1', 'dispatch_id': 'dispatch_1',
           'terminal_handle': 'term_1', 'pane_key': 'tab_1:leaf_1', 'worktree_id': 'repo::/work',
           'workspace_path': '/work', 'incarnation_id': 'pane_process_1',
           'route': 'clinepass', 'packet_sha256': 'a' * 64}


class ExecutionReceiptTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'private'

    def test_actual_child_exit_is_recorded_without_task_success(self):
        result = execution.run(self.root, BINDING, [sys.executable, '-c', 'raise SystemExit(7)'])
        self.assertEqual(result['returncode'], 7)
        self.assertEqual(execution.inspect(self.root, BINDING['dispatch_id'])['observation'], 'exited')
        self.assertNotIn('outcome', result)
        self.assertEqual(result['binding'], BINDING)

    def test_same_dispatch_cannot_execute_twice_after_exit(self):
        execution.run(self.root, BINDING, ['/bin/true'])
        with patch('superagent_queue.execution.subprocess.Popen') as spawn, \
                self.assertRaisesRegex(ValueError, 'already has an execution intent'):
            execution.run(self.root, BINDING, ['/bin/true'])
        spawn.assert_not_called()

    def test_launch_failure_retains_original_intent_and_exact_error(self):
        result = execution.run(self.root, BINDING, ['/missing-dsh-executable'])
        self.assertEqual(result['returncode'], 127)
        self.assertIn('/missing-dsh-executable', result['error'])
        self.assertEqual(execution.inspect(self.root, 'dispatch_1')['observation'], 'exited')

    def test_child_exit_before_identity_read_still_records_actual_wait_status(self):
        original = execution.process

        def identity(pid):
            if pid != os.getpid():
                raise FileNotFoundError('child exited before process inspection')
            return original(pid)

        with patch('superagent_queue.execution.process', side_effect=identity):
            result = execution.run(self.root, BINDING, ['/bin/true'])
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(execution.inspect(self.root, 'dispatch_1')['observation'], 'exited')

    def test_fork_failure_after_committed_intent_never_allows_blind_retry(self):
        with patch('superagent_queue.execution.subprocess.Popen', side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            execution.run(self.root, BINDING, ['/bin/true'])
        record = execution.inspect(self.root, 'dispatch_1')
        self.assertEqual(record['observation'], 'unverifiable')
        self.assertIn('intent', record)
        with self.assertRaisesRegex(ValueError, 'already has an execution intent'):
            execution.run(self.root, BINDING, ['/bin/true'])

    def test_child_outlives_observer_crash_and_stays_proven_live(self):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        with patch('superagent_queue.execution.subprocess.Popen', return_value=proc), \
                patch.object(proc, 'wait', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            execution.run(self.root, BINDING, ['fixture-worker'])
        record = execution.inspect(self.root, 'dispatch_1')
        self.assertEqual(record['observation'], 'live')
        self.assertEqual(record['child']['pid'], proc.pid)
        self.assertTrue(record['execution_only'])

    def test_private_receipts_and_binding_digests_are_preserved(self):
        before = json.dumps(BINDING, sort_keys=True)
        execution.run(self.root, BINDING, ['/bin/true'])
        self.assertEqual(json.dumps(BINDING, sort_keys=True), before)
        directory = self.root / 'dispatch_1'
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for path in directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_native_binding_never_spawns(self):
        for change in ({'dispatch_id': '../escape'}, {'route': 'unknown-paid-route'},
                       {'packet_sha256': 'bad'}, {'workspace_path': 'relative'},
                       {'api_key': 'not-allowed'}):
            with self.subTest(change=change), patch('superagent_queue.execution.subprocess.Popen') as spawn, \
                    self.assertRaises(ValueError):
                execution.run(self.root, {**BINDING, **change}, ['/bin/true'])
            spawn.assert_not_called()

    def test_process_reuse_and_boot_change_are_not_live(self):
        proc = execution.process(os.getpid())
        self.assertEqual(execution.observe_process(proc), 'live')
        self.assertEqual(execution.observe_process({**proc, 'start_ticks': proc['start_ticks'] + 1}), 'unverifiable')
        self.assertEqual(execution.observe_process({**proc, 'boot_id': 'different-boot'}), 'unverifiable')
        self.assertEqual(execution.observe_process({**proc, 'pid': 2147483647}), 'unverifiable')

    def test_unreadable_process_does_not_become_exit_evidence(self):
        identity = execution.process(os.getpid())
        with patch('superagent_queue.execution.process', side_effect=PermissionError('process unreadable')):
            self.assertEqual(execution.observe_process(identity), 'unverifiable')

    def test_incomplete_child_identity_never_becomes_live_or_raises(self):
        self.assertEqual(execution.observe_process({'pid': os.getpid()}), 'unverifiable')

    def test_corrupted_intent_digest_is_rejected_before_observation(self):
        execution.run(self.root, BINDING, ['/bin/true'])
        path = self.root / 'dispatch_1/intent.json'
        value = json.loads(path.read_text())
        path.write_text(json.dumps({**value, 'intent_id': 'wrong-intent'}))
        with self.assertRaisesRegex(ValueError, 'intent identity'):
            execution.inspect(self.root, 'dispatch_1')

    def test_receipt_directory_symlink_is_refused_before_spawn(self):
        actual = Path(self.directory.name) / 'actual'
        actual.mkdir()
        self.root.symlink_to(actual, target_is_directory=True)
        with patch('superagent_queue.execution.subprocess.Popen') as spawn, self.assertRaises(ValueError):
            execution.run(self.root, BINDING, ['/bin/true'])
        spawn.assert_not_called()

    def test_concurrent_claim_refuses_spawn_before_original_intent_is_written(self):
        self.root.mkdir(mode=0o700)
        (self.root / 'dispatch_1').mkdir(mode=0o700)
        with patch('superagent_queue.execution.subprocess.Popen') as spawn, self.assertRaises(ValueError):
            execution.run(self.root, BINDING, ['/bin/true'])
        spawn.assert_not_called()

    def test_untrusted_exit_cannot_claim_another_binding_or_invent_an_exit_code(self):
        execution.run(self.root, BINDING, ['/bin/true'])
        path = self.root / 'dispatch_1/exit.json'
        original = json.loads(path.read_text())
        for change in ({'intent_id': 'other'}, {'binding': {**BINDING, 'task_id': 'other_task'}},
                       {'returncode': None}, {'returncode': True}, {'finished_at': -1}, {'finished_at': None}):
            path.write_text(json.dumps({**original, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                execution.inspect(self.root, 'dispatch_1')

    def test_partial_persistence_never_exposes_final_receipt(self):
        self.root.mkdir(mode=0o700)
        with patch('superagent_queue.execution.os.fsync', side_effect=OSError('disk unavailable')), \
                self.assertRaisesRegex(OSError, 'disk unavailable'):
            execution.persist(self.root, 'exit.json', {'returncode': 0})
        self.assertFalse((self.root / 'exit.json').exists())

    def test_malformed_binding_values_raise_validation_error(self):
        for change in ({'route': []}, {'packet_sha256': None}, {'workspace_path': None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                execution.validate({**BINDING, **change})

    def test_exit_without_original_intent_is_not_task_evidence(self):
        with self.assertRaises(FileNotFoundError):
            execution.inspect(self.root, 'dispatch_1')


if __name__ == '__main__':
    unittest.main()
