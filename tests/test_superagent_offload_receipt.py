"""Offload wrapper exits and remote command outcomes are independent durable facts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import execution, offload_receipt

BINDING = {'run_id': 'run_1', 'task_id': 'task_1', 'dispatch_id': 'dispatch_1',
           'terminal_handle': 'term_1', 'pane_key': 'tab_1:leaf_1', 'worktree_id': 'repo::/work',
           'workspace_path': '/work', 'incarnation_id': 'pane_process_1',
           'route': 'lightning', 'packet_sha256': 'a' * 64}
COMMAND = ['sh', '-c', 'printf verified']


def remote_result(job='job_1', code=0):
    return {'schema_version': 1, 'job_id': job, 'command_sha256': offload_receipt.command_digest(COMMAND),
            'returncode': code, 'spawned': True, 'started_at': 100, 'finished_at': 101,
            'runtime': {'pid': 123, 'boot_id': 'remote-boot', 'start_ticks': 10}}


class OffloadReceiptTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'private/offloads'

    def execute(self, result=None, code=0, job='job_1'):
        script = ('import json,os,pathlib; '
                  f'payload={result!r}; '
                  'p=pathlib.Path(os.environ["SPECTRE_OFFLOAD_COMMAND_RECEIPT"]); '
                  'p.write_text(json.dumps(payload)) if payload is not None else None; '
                  'p.chmod(0o600) if payload is not None else None; '
                  f'print("wrapper output"); raise SystemExit({code})')
        return offload_receipt.run(self.root, job, BINDING, COMMAND, [sys.executable, '-c', script])

    def test_wrapper_success_without_remote_receipt_is_not_remote_success(self):
        result = self.execute()
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['remote_observation'], 'unverifiable')
        self.assertIsNone(result['remote_result'])
        self.assertNotIn('outcome', result)
        self.assertEqual(offload_receipt.inspect(self.root, 'job_1')['observation'], 'exited')

    def test_actual_wrapper_exit_and_exact_remote_result_are_retained_separately(self):
        result = self.execute(remote_result(code=7), code=19)
        self.assertEqual(result['returncode'], 19)
        self.assertEqual(result['remote_result']['returncode'], 7)
        self.assertEqual(result['remote_observation'], 'exited')
        self.assertEqual((self.root / 'job_1/stdout.log').read_text(), 'wrapper output\n')
        for path in (self.root / 'job_1').iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_another_jobs_receipt_retains_wrapper_exit_without_copying_remote_outcome(self):
        result = self.execute(remote_result(job='foreign'))
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['remote_observation'], 'unverifiable')
        self.assertIn('identity', result['remote_error'])
        self.assertIsNone(result['remote_result'])

    def test_command_digest_mismatch_and_invalid_remote_codes_are_not_accepted(self):
        for change in ({'command_sha256': 'b' * 64}, {'returncode': True}, {'returncode': 256},
                       {'finished_at': 99}, {'spawned': False}, {'runtime': {}}, {'extra': 'untrusted'},
                       {'schema_version': True}, {'started_at': None}, {'finished_at': None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                offload_receipt.remote({**remote_result(), **change}, 'job_1',
                                      offload_receipt.command_digest(COMMAND))

    def test_same_job_cannot_spawn_twice_even_after_success(self):
        self.execute(remote_result())
        with patch.object(offload_receipt.subprocess, 'Popen') as spawn, self.assertRaisesRegex(ValueError, 'intent'):
            self.execute(remote_result())
        spawn.assert_not_called()

    def test_live_wrapper_survives_observer_interruption_without_completion(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        with patch.object(offload_receipt.subprocess, 'Popen', return_value=process), \
                patch.object(process, 'wait', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            offload_receipt.run(self.root, 'job_1', BINDING, COMMAND, ['fixture'])
        self.assertEqual(offload_receipt.inspect(self.root, 'job_1')['observation'], 'live')
        self.assertFalse((self.root / 'job_1/exit.json').exists())

    def test_launch_failure_records_os_failure_not_remote_exit(self):
        result = offload_receipt.run(self.root, 'job_1', BINDING, COMMAND, ['/missing-offload-wrapper'])
        self.assertEqual(result['returncode'], 127)
        self.assertFalse(result['spawned'])
        self.assertEqual(result['remote_observation'], 'unverifiable')
        self.assertIn('/missing-offload-wrapper', result['error'])

    def test_invalid_binding_and_shell_payload_never_spawns(self):
        for change in ({'route': 'paid-cloud'}, {'api_key': 'not-permitted'}, {'workspace_path': 'relative'},
                       {'packet_sha256': 'bad'}, {'run_id': '../escape'}):
            with self.subTest(change=change), patch.object(offload_receipt.subprocess, 'Popen') as spawn, \
                    self.assertRaises(ValueError):
                offload_receipt.run(self.root, 'job_1', {**BINDING, **change}, COMMAND, ['/bin/true'])
            spawn.assert_not_called()
        for value in ('sh -c true', [], [''], ['bad\x00word']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                offload_receipt.command_digest(value)

    def test_receipt_corruption_does_not_change_the_original_job_binding(self):
        self.execute(remote_result())
        path = self.root / 'job_1/exit.json'
        original = json.loads(path.read_text())
        for change in ({'job_id': 'other'}, {'binding': {**BINDING, 'task_id': 'other'}},
                       {'returncode': None}, {'finished_at': -1}, {'command_sha256': 'b' * 64}):
            path.write_text(json.dumps({**original, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                offload_receipt.inspect(self.root, 'job_1')

    def test_record_failure_keeps_original_intent_without_relaunch(self):
        original = execution.persist

        def persist(directory, name, value):
            if name == 'exit.json':
                raise OSError('exit storage unavailable')
            return original(directory, name, value)

        with patch.object(offload_receipt.execution, 'persist', side_effect=persist), \
                self.assertRaisesRegex(OSError, 'exit storage unavailable'):
            self.execute()
        self.assertEqual(offload_receipt.inspect(self.root, 'job_1')['observation'], 'unverifiable')
        with self.assertRaisesRegex(ValueError, 'intent'):
            self.execute()

    def test_remote_observation_corruption_cannot_allow_cleanup(self):
        self.execute(remote_result())
        path = self.root / 'job_1/exit.json'
        original = json.loads(path.read_text())
        for change in ({'remote_observation': 'live'}, {'remote_result': None},
                       {'remote_observation': 'unverifiable', 'remote_result': remote_result()}):
            path.write_text(json.dumps({**original, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                offload_receipt.inspect(self.root, 'job_1')

    def test_child_receipt_digest_and_missing_process_identity_are_not_false_liveness(self):
        offload_receipt.begin(self.root, 'job_1', BINDING, COMMAND)
        directory = self.root / 'job_1'
        execution.persist(directory, 'started.json', {'intent_id': 'wrong', 'child': {}})
        with self.assertRaisesRegex(ValueError, 'child identity'):
            offload_receipt.inspect(self.root, 'job_1')


if __name__ == '__main__':
    unittest.main()
