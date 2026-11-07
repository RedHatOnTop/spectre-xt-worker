"""Late artifact recovery reads an existing job without reexecuting or deleting it."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from superagent_queue import execution, offload_receipt, offload_source
from test_superagent_offload_receipt import BINDING, COMMAND, remote_result


class OffloadArtifactFetchTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name) / 'private'
        self.base.mkdir(mode=0o700)
        self.root = self.base / 'offloads'
        self.packet = self.base / 'packet.json'
        self.packet.write_text(json.dumps({'schema_version': 1, 'command': COMMAND, 'artifacts': ['results']}))
        self.packet.chmod(0o600)
        binding = {**BINDING, 'packet_sha256': hashlib.sha256(self.packet.read_bytes()).hexdigest()}
        self.record = offload_receipt.run(self.root, 'job_1', binding, COMMAND, ['/bin/true'],
            authority={'packet_path': str(self.packet), 'studio': 'existing-studio'})
        execution.persist(self.root / 'job_1', 'transport.json', {'schema_version': 1, 'job_id': 'job_1',
            'command_sha256': self.record['command_sha256'], 'studio': 'existing-studio',
            'remote_dir': 'offload/job_1', 'recorded_at': self.record['created_at']})
        execution.persist(self.root / 'job_1', 'command-result.json', remote_result())
        offload_receipt.reconcile(self.root, 'job_1')
        self.remote = self.base / 'remote'
        (self.remote / 'results/empty').mkdir(parents=True)
        (self.remote / 'results/output.json').write_text('actual remote artifact')
        spec = importlib.util.spec_from_file_location('artifact_helper', ROOT / 'scripts/offload-artifacts.py')
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        self.manifest = helper.manifest(self.remote, 'job_1', self.record['command_sha256'], ['results'])
        self.calls = []

    def module(self):
        from superagent_queue import offload_artifact_fetch
        return offload_artifact_fetch

    def transport(self, command, **options):
        self.calls.append(command)
        if command[0] == 'ssh':
            return subprocess.CompletedProcess(command, 0, json.dumps(self.manifest), '')
        if command[0] == 'rsync':
            return subprocess.run(['/usr/bin/rsync', '-az', '--chmod=D700,F600', str(self.remote / 'results'),
                                   command[-1]], capture_output=True, text=True, check=False)
        raise AssertionError(command)

    def test_actual_returned_files_create_snapshot_without_changing_wrapper_or_remote_source(self):
        module = self.module()
        before = (self.root / 'job_1/exit.json').read_bytes()
        with patch.object(module, 'query', side_effect=self.transport):
            result = module.fetch(self.root, 'job_1', 'existing-studio')
        destination = Path(result['artifact']['artifact_root'])
        self.assertEqual((destination / 'results/output.json').read_text(), 'actual remote artifact')
        self.assertTrue((destination / 'results/empty').is_dir())
        self.assertEqual((self.root / 'job_1/exit.json').read_bytes(), before)
        self.assertEqual((self.remote / 'results/output.json').read_text(), 'actual remote artifact')
        self.assertFalse(result['remote_execution'])
        self.assertFalse(result['capacity_released'])
        self.assertEqual([call[0] for call in self.calls], ['ssh', 'rsync'])
        self.assertTrue(any(row['source'] == 'offload-artifact' for row in offload_source.page(self.root, 'run_1', None).events))

    def test_accepted_snapshot_replay_performs_no_network_or_new_recovery_attempt(self):
        module = self.module()
        with patch.object(module, 'query', side_effect=self.transport):
            first = module.fetch(self.root, 'job_1', 'existing-studio')
        with patch.object(module, 'query') as query:
            repeated = module.fetch(self.root, 'job_1', 'existing-studio')
        query.assert_not_called()
        self.assertEqual(repeated['artifact'], first['artifact'])

    def test_failed_transport_records_exact_failure_and_later_fetch_uses_a_new_attempt(self):
        module = self.module()
        failure = subprocess.CompletedProcess([], 255, 'partial manifest verbatim', 'connection lost verbatim')
        with patch.object(module, 'query', return_value=failure), \
                self.assertRaisesRegex(RuntimeError, 'partial manifest verbatim\nstderr:\nconnection lost verbatim'):
            module.fetch(self.root, 'job_1', 'existing-studio')
        attempts = list((self.root / 'job_1/artifact-recovery').iterdir())
        self.assertEqual(len(attempts), 1)
        failed = attempts[0] / 'failure.json'
        before = failed.read_bytes()
        with patch.object(module, 'query', side_effect=self.transport):
            module.fetch(self.root, 'job_1', 'existing-studio')
        self.assertEqual(failed.read_bytes(), before)
        self.assertEqual(len(list((self.root / 'job_1/artifact-recovery').iterdir())), 2)

    def test_timeout_malformed_overlong_foreign_manifest_never_publishes_artifact(self):
        module = self.module()
        for output in ('invalid-json', 'x' * 65537, json.dumps({**self.manifest, 'job_id': 'foreign'})):
            with self.subTest(output=output[:30]), patch.object(module, 'query',
                    return_value=subprocess.CompletedProcess([], 0, output, '')), self.assertRaises(ValueError):
                module.fetch(self.root, 'job_1', 'existing-studio')
        with patch.object(module, 'query', side_effect=subprocess.TimeoutExpired('ssh', 30)), \
                self.assertRaises(subprocess.TimeoutExpired):
            module.fetch(self.root, 'job_1', 'existing-studio')
        self.assertFalse((self.root / 'job_1/artifact-exit.json').exists())
        self.assertTrue((self.remote / 'results/output.json').exists())

    def test_foreign_studio_live_wrapper_unknown_remote_and_changed_packet_refuse_before_network(self):
        module = self.module()
        with patch.object(module, 'query') as query, self.assertRaisesRegex(ValueError, 'Studio'):
            module.fetch(self.root, 'job_1', 'foreign-studio')
        query.assert_not_called()
        with patch.object(module.offload_receipt, 'inspect', return_value={**self.record, 'observation': 'live'}), \
                patch.object(module, 'query') as query, self.assertRaisesRegex(ValueError, 'wrapper exit'):
            module.fetch(self.root, 'job_1', 'existing-studio')
        query.assert_not_called()
        (self.root / 'job_1/remote-exit.json').unlink()
        with patch.object(module, 'query') as query, self.assertRaisesRegex(ValueError, 'remote command exit'):
            module.fetch(self.root, 'job_1', 'existing-studio')
        query.assert_not_called()
        offload_receipt.reconcile(self.root, 'job_1')
        self.packet.write_text('{}')
        with patch.object(module, 'query') as query, self.assertRaises(ValueError):
            module.fetch(self.root, 'job_1', 'existing-studio')
        query.assert_not_called()

    def test_failed_rsync_and_corrupt_copy_preserve_remote_data_and_no_verification(self):
        module = self.module()
        original = self.transport

        def failed(command, **options):
            return subprocess.CompletedProcess(command, 24, '', 'partial rsync transfer') if command[0] == 'rsync' else original(command, **options)

        with patch.object(module, 'query', side_effect=failed), self.assertRaisesRegex(RuntimeError, 'partial rsync transfer'):
            module.fetch(self.root, 'job_1', 'existing-studio')

        def corrupt(command, **options):
            result = original(command, **options)
            if command[0] == 'rsync':
                Path(command[-1], 'results/output.json').write_text('corrupted')
            return result

        with patch.object(module, 'query', side_effect=corrupt), self.assertRaisesRegex(ValueError, 'tree differs'):
            module.fetch(self.root, 'job_1', 'existing-studio')
        self.assertEqual((self.remote / 'results/output.json').read_text(), 'actual remote artifact')
        self.assertFalse((self.root / 'job_1/artifact-exit.json').exists())

    def test_unknown_side_effect_claim_and_failure_receipt_storage_do_not_mask_original_error(self):
        module = self.module()
        original = execution.persist

        def fail_receipt(directory, name, value):
            if name == 'failure.json':
                raise OSError('failure evidence disk unavailable')
            return original(directory, name, value)

        with patch.object(module, 'query', return_value=subprocess.CompletedProcess([], 255, '', 'original fetch error')), \
                patch.object(execution, 'persist', side_effect=fail_receipt), self.assertRaisesRegex(RuntimeError, 'original fetch error') as caught:
            module.fetch(self.root, 'job_1', 'existing-studio')
        self.assertTrue(any('failure evidence disk unavailable' in note for note in caught.exception.__notes__))

    def test_crash_after_verified_return_reuses_completed_recovery_without_network(self):
        module = self.module()
        with patch.object(module, 'query', side_effect=self.transport), \
                patch.object(module.offload_artifact, 'capture', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            module.fetch(self.root, 'job_1', 'existing-studio')
        with patch.object(module, 'query') as query:
            result = module.fetch(self.root, 'job_1', 'existing-studio')
        query.assert_not_called()
        self.assertTrue(Path(result['artifact']['artifact_root'], 'verified.json').exists())

    def test_current_recovery_lock_refuses_duplicate_transfer(self):
        module = self.module()
        from superagent_queue import settings
        with settings.lock(self.root / 'job_1/artifact-recovery.lock'), \
                patch.object(module, 'query') as query, self.assertRaises(BlockingIOError):
            module.fetch(self.root, 'job_1', 'existing-studio')
        query.assert_not_called()

    def test_unsafe_job_identifier_cannot_create_a_lock_outside_job_storage(self):
        module = self.module()
        with patch.object(module.settings, 'lock') as lock, self.assertRaises(ValueError):
            module.fetch(self.root, '../foreign', 'existing-studio')
        lock.assert_not_called()

    def test_completed_recovery_intent_cannot_be_changed_or_redirected(self):
        module = self.module()
        with patch.object(module, 'query', side_effect=self.transport), \
                patch.object(module.offload_artifact, 'capture', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            module.fetch(self.root, 'job_1', 'existing-studio')
        attempt = next((self.root / 'job_1/artifact-recovery').iterdir())
        value = json.loads((attempt / 'intent.json').read_text())
        (attempt / 'intent.json').write_text(json.dumps({**value, 'job_id': 'foreign'}))
        with patch.object(module, 'query') as query, self.assertRaisesRegex(ValueError, 'intent'):
            module.fetch(self.root, 'job_1', 'existing-studio')
        query.assert_not_called()

    def test_literal_artifact_home_prefix_never_becomes_remote_shell_expansion(self):
        module = self.module()
        spec = {'artifacts': ['$HOME/$(touch injected)']}
        manifest = {**self.manifest, 'artifacts': spec['artifacts']}
        target = {'studio': 'existing-studio', 'remote_dir': 'offload/job_1'}
        with patch.object(module, 'query', return_value=subprocess.CompletedProcess([], 0, json.dumps(manifest), '')) as query:
            module.fetch_manifest(self.record, target, spec, module.offload_artifact.helper())
        self.assertIn("--artifact '$HOME/$(touch injected)'", query.call_args.args[0][-1])


if __name__ == '__main__':
    unittest.main()
