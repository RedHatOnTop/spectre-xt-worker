"""Recover only exact existing offload receipts without starting or deleting remote work."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import execution, offload_receipt, offload_transport
from test_superagent_offload_receipt import BINDING, COMMAND, remote_result


class OffloadTransportTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'private/offloads'
        self.record = offload_receipt.run(self.root, 'job_1', BINDING, COMMAND, ['/bin/true'])
        self.job = self.root / 'job_1'
        execution.persist(self.job, 'transport.json', {'schema_version': 1, 'job_id': 'job_1',
            'command_sha256': self.record['command_sha256'], 'studio': 'existing-studio',
            'remote_dir': 'offload/job_1', 'recorded_at': self.record['created_at']})

    def pulled(self, value=None):
        return subprocess.CompletedProcess([], 0, json.dumps(value or remote_result()), '')

    def test_fetch_uses_only_exact_existing_remote_receipt_and_preserves_wrapper(self):
        before = (self.job / 'exit.json').read_bytes()
        with patch.object(offload_transport.subprocess, 'run', return_value=self.pulled()) as query:
            result = offload_transport.fetch(self.root, 'job_1', 'existing-studio')
        self.assertEqual(result['remote_result']['returncode'], 0)
        command = query.call_args.args[0]
        self.assertEqual(command[:5], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10'])
        self.assertEqual(command[-2], 'existing-studio')
        self.assertEqual(command[-1], 'head -c 65537 -- "$HOME"/offload/job_1/.spectre-offload/command-result.json')
        self.assertEqual((self.job / 'exit.json').read_bytes(), before)
        self.assertFalse(any(word in ' '.join(command) for word in ('rm -rf', 'studio start', 'mkdir', 'worker-start')))

    def test_foreign_configured_studio_never_queries_or_accepts_a_receipt(self):
        with patch.object(offload_transport.subprocess, 'run') as query, self.assertRaisesRegex(ValueError, 'Studio'):
            offload_transport.fetch(self.root, 'job_1', 'other-studio')
        query.assert_not_called()

    def test_transport_failure_or_invalid_json_never_creates_remote_exit_evidence(self):
        for response in (subprocess.CompletedProcess([], 255, '', 'Connection lost verbatim'),
                         subprocess.CompletedProcess([], 0, 'not-json', ''), self.pulled(remote_result(job='foreign'))):
            with self.subTest(response=response), patch.object(offload_transport.subprocess, 'run', return_value=response), \
                    self.assertRaises((ValueError, RuntimeError)):
                offload_transport.fetch(self.root, 'job_1', 'existing-studio')
        self.assertFalse((self.job / 'remote-exit.json').exists())

    def test_timeout_keeps_original_claim_and_receipt_unresolved(self):
        with patch.object(offload_transport.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 30)), \
                self.assertRaises(subprocess.TimeoutExpired):
            offload_transport.fetch(self.root, 'job_1', 'existing-studio')
        self.assertFalse((self.job / 'remote-exit.json').exists())

    def test_unsafe_transport_path_never_becomes_a_shell_command(self):
        path = self.job / 'transport.json'
        original = json.loads(path.read_text())
        for change in ({'remote_dir': '../escape'}, {'studio': 'user@foreign'}, {'schema_version': True},
                       {'remote_dir': 'a;touch /tmp/a'}, {'job_id': 'another'}, {'command_sha256': 'b' * 64}):
            path.write_text(json.dumps({**original, **change}))
            with self.subTest(change=change), patch.object(offload_transport.subprocess, 'run') as query, \
                    self.assertRaises(ValueError):
                offload_transport.fetch(self.root, 'job_1', 'existing-studio')
            query.assert_not_called()

    def test_overlong_remote_receipt_is_rejected_without_local_publication(self):
        result = subprocess.CompletedProcess([], 0, 'x' * 65537, '')
        with patch.object(offload_transport.subprocess, 'run', return_value=result), \
                self.assertRaisesRegex(ValueError, 'byte limit'):
            offload_transport.fetch(self.root, 'job_1', 'existing-studio')
        self.assertFalse((self.job / 'command-result.json').exists())

    def test_fetch_replay_preserves_identical_result_and_refuses_changed_remote_result(self):
        with patch.object(offload_transport.subprocess, 'run', return_value=self.pulled()):
            first = offload_transport.fetch(self.root, 'job_1', 'existing-studio')
            self.assertEqual(offload_transport.fetch(self.root, 'job_1', 'existing-studio'), first)
        with patch.object(offload_transport.subprocess, 'run', return_value=self.pulled(remote_result(code=7))), \
                self.assertRaisesRegex(ValueError, 'conflict'):
            offload_transport.fetch(self.root, 'job_1', 'existing-studio')

    def test_live_wrapper_never_probes_another_remote_execution(self):
        with patch.object(offload_receipt, 'inspect', return_value={**self.record, 'observation': 'live'}), \
                patch.object(offload_transport.subprocess, 'run') as query, \
                self.assertRaisesRegex(ValueError, 'wrapper exit'):
            offload_transport.fetch(self.root, 'job_1', 'existing-studio')
        query.assert_not_called()

    def test_existing_local_receipt_does_not_hide_exact_fetch_error(self):
        failure = subprocess.CompletedProcess([], 255, 'partial output verbatim', 'host closed connection')
        with patch.object(offload_transport.subprocess, 'run', return_value=failure), \
                self.assertRaisesRegex(RuntimeError, 'stdout:\npartial output verbatim\nstderr:\nhost closed connection'):
            offload_transport.fetch(self.root, 'job_1', 'existing-studio')


if __name__ == '__main__':
    unittest.main()
