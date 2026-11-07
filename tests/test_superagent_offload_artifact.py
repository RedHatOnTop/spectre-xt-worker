"""Artifact evidence remains native-bound and independent of wrapper delivery."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from superagent_queue import driver, execution, offload_receipt, offload_source
from superagent_queue.store import Journal
from test_superagent_offload_receipt import BINDING, COMMAND, remote_result
from test_superagent_offload_source import TARGET


class OffloadArtifactEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name) / 'private'
        self.journal = Journal(self.base / 'journal.sqlite3')
        self.addCleanup(self.journal.close)
        self.root = self.base / 'offloads'
        self.packet = self.base / 'packet.json'
        self.spec = {'schema_version': 1, 'command': COMMAND, 'artifacts': ['results']}
        self.packet.write_text(json.dumps(self.spec))
        self.packet.chmod(0o600)
        self.binding = {**BINDING, 'packet_sha256': hashlib.sha256(self.packet.read_bytes()).hexdigest()}
        self.record = offload_receipt.run(self.root, 'job_1', self.binding, COMMAND, ['/bin/true'],
            authority={'packet_path': str(self.packet)})
        execution.persist(self.root / 'job_1', 'command-result.json', remote_result())
        offload_receipt.reconcile(self.root, 'job_1')
        spec = importlib.util.spec_from_file_location('artifact_helper', ROOT / 'scripts/offload-artifacts.py')
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        self.destination = self.helper.prepare(self.base / 'offload-artifacts', 'job_1',
                                               self.record['command_sha256'], ['results'])
        (self.destination / 'results').mkdir()
        (self.destination / 'results/output.json').write_text('actual result')

    def verified(self):
        value = self.helper.manifest(self.destination, 'job_1', self.record['command_sha256'], ['results'])
        self.helper.persist(self.destination / 'manifest.json', value)
        return self.helper.verify(self.destination, 'job_1', self.record['command_sha256'], ['results'])

    def artifact_module(self):
        from superagent_queue import offload_artifact
        return offload_artifact

    def test_verified_artifact_adds_one_distinct_event_without_changing_prior_delivery(self):
        self.assertEqual(driver.ingest_offloads(self.journal, TARGET), 2)
        original = self.journal.effects()
        self.verified()
        self.assertEqual(driver.ingest_offloads(self.journal, TARGET), 1)
        self.assertEqual(driver.ingest_offloads(self.journal, TARGET), 0)
        self.assertEqual(self.journal.effects()[:2], original)
        latest = self.journal.effects()[-1]['payload']['text']
        self.assertIn('offload-artifact', latest)
        self.assertIn('review_only', latest)
        self.assertIn('capacity_released', latest)
        self.assertNotIn('worker_done', latest)

    def test_missing_verification_is_not_an_artifact_event(self):
        page = offload_source.page(self.root, 'run_1', None)
        self.assertEqual([row['source'] for row in page.events], ['offload-execution', 'offload-reconciliation'])

    def test_first_observation_rereads_bytes_and_refuses_changed_file(self):
        self.verified()
        (self.destination / 'results/output.json').write_text('changed result')
        with self.assertRaisesRegex(ValueError, 'artifact tree differs'):
            offload_source.page(self.root, 'run_1', None)
        self.assertFalse((self.root / 'job_1/artifact-exit.json').exists())

    def test_artifact_snapshot_is_immutable_and_survives_later_local_resource_cleanup(self):
        self.verified()
        page = offload_source.page(self.root, 'run_1', None)
        artifact = next(row for row in page.events if row['source'] == 'offload-artifact')
        original = (self.root / 'job_1/artifact-exit.json').read_bytes()
        (self.destination / 'results/output.json').unlink()
        repeated = offload_source.page(self.root, 'run_1', None)
        self.assertEqual(next(row for row in repeated.events if row['source'] == 'offload-artifact'), artifact)
        self.assertEqual((self.root / 'job_1/artifact-exit.json').read_bytes(), original)

    def test_unknown_remote_completion_never_accepts_artifact_proof(self):
        self.verified()
        (self.root / 'job_1/remote-exit.json').unlink()
        page = offload_source.page(self.root, 'run_1', None)
        self.assertEqual(len(page.events), 1)
        self.assertFalse((self.root / 'job_1/artifact-exit.json').exists())

    def test_packet_selection_digest_and_command_cannot_expand_artifact_authority(self):
        self.verified()
        for change in ({'artifacts': ['outside']}, {'command': ['different']}, {'extra': 'permission'}):
            self.packet.write_text(json.dumps({**self.spec, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                offload_source.page(self.root, 'run_1', None)
        self.assertFalse((self.root / 'job_1/artifact-exit.json').exists())

    def test_queue_failure_preserves_snapshot_original_cursor_and_later_replay(self):
        self.verified()
        self.journal.connection.execute('''CREATE TRIGGER reject_artifact BEFORE INSERT ON effect
            BEGIN SELECT RAISE(ABORT, 'artifact queue unavailable'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'artifact queue unavailable'):
            driver.ingest_offloads(self.journal, TARGET)
        self.assertIsNone(self.journal.source_cursor('offloads-run_1'))
        self.assertTrue((self.root / 'job_1/artifact-exit.json').exists())
        self.journal.connection.execute('DROP TRIGGER reject_artifact')
        self.assertEqual(driver.ingest_offloads(self.journal, TARGET), 3)

    def test_corrupt_snapshot_cannot_change_original_job_wrapper_or_verification(self):
        self.verified()
        offload_source.page(self.root, 'run_1', None)
        path = self.root / 'job_1/artifact-exit.json'
        original = json.loads(path.read_text())
        for change in ({'intent_id': 'foreign'}, {'wrapper_exit_sha256': 'b' * 64},
                       {'remote_result_sha256': 'b' * 64}, {'artifact_root': '/outside'}, {'schema_version': True}):
            path.write_text(json.dumps({**original, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                offload_source.page(self.root, 'run_1', None)

    def test_helper_hash_change_is_refused_before_artifact_code_runs(self):
        module = self.artifact_module()
        with patch.object(module, 'HELPER_SHA256', 'b' * 64), self.assertRaisesRegex(ValueError, 'reviewed bytes'):
            module.helper()

    def test_publication_requires_current_owner_and_never_mutates_native_or_capacity(self):
        module = self.artifact_module()
        from test_superagent_mail_obligation import inventory
        self.verified()
        target = {**TARGET, 'terminal_handle': 'term_1', 'incarnation_id': 'process_1'}
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        with patch('orca_runtime.observe.collect', return_value=inventory()):
            with self.assertRaisesRegex(ValueError, 'current coordinator pane'):
                module.publish(self.journal, target, 'job_1', {}, '/orca')
            result = module.publish(self.journal, target, 'job_1', caller, '/orca')
        self.assertEqual(result['queued'], 3)
        self.assertFalse(result['native_mutations'])
        self.assertFalse(result['capacity_released'])

    def test_snapshot_failure_and_competing_identical_writer_preserve_verified_source(self):
        module = self.artifact_module()
        self.verified()
        original = execution.persist
        with patch.object(execution, 'persist', side_effect=OSError('artifact evidence disk unavailable')), \
                self.assertRaisesRegex(OSError, 'disk unavailable'):
            offload_source.page(self.root, 'run_1', None)
        self.assertTrue((self.destination / 'verified.json').exists())

        def competing(directory, name, value):
            original(directory, name, value)
            raise FileExistsError('other observer committed')

        with patch.object(execution, 'persist', side_effect=competing):
            result = offload_source.page(self.root, 'run_1', None)
        self.assertEqual(len(result.events), 3)

    def test_invalid_proof_and_snapshot_destination_are_refused(self):
        module = self.artifact_module()
        self.verified()
        offload_source.page(self.root, 'run_1', None)
        path = self.root / 'job_1/artifact-exit.json'
        value = json.loads(path.read_text())
        record = offload_receipt.inspect(self.root, 'job_1')
        for proof in ({**value['verification'], 'verified_at': None},
                      {**value['verification'], 'native_settlement': True},
                      {**value['verification'], 'manifest_sha256': 'b' * 64}):
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                module.validate(self.root, record, {**value, 'verification': proof})
        with self.assertRaises(ValueError):
            module.destination(self.root, record, self.base / 'outside/job_1')

    def test_corrupt_manifest_types_are_validation_errors_not_unhandled_exceptions(self):
        module = self.artifact_module()
        self.verified()
        offload_source.page(self.root, 'run_1', None)
        value = json.loads((self.root / 'job_1/artifact-exit.json').read_text())
        record = offload_receipt.inspect(self.root, 'job_1')
        for change in ({'artifacts': None}, {'artifacts': [1]}, {'entries': 'not-a-list'}):
            manifest = {**value['manifest'], **change}
            proof = {**value['verification'], 'manifest_sha256': hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.validate(self.root, record, {**value, 'manifest': manifest, 'verification': proof})

    def test_missing_recovery_claim_does_not_hide_an_existing_snapshot(self):
        module = self.artifact_module()
        self.verified()
        offload_source.page(self.root, 'run_1', None)
        path = self.root / 'job_1/artifact-exit.json'
        value = json.loads(path.read_text())
        path.write_text(json.dumps({**value, 'artifact_root': str(self.root / 'job_1/artifact-recovery/recovery-1/job_1')}))
        with self.assertRaises(FileNotFoundError):
            module.observe(self.root, offload_receipt.inspect(self.root, 'job_1'))

    def test_foreign_artifact_destination_is_refused_before_readback_or_metadata_creation(self):
        module = self.artifact_module()
        record = offload_receipt.inspect(self.root, 'job_1')
        foreign = self.base / 'foreign/job_1'
        foreign.mkdir(parents=True)
        with patch.object(module, 'helper') as helper, self.assertRaisesRegex(ValueError, 'destination'):
            module.capture(self.root, record, foreign)
        helper.assert_not_called()

    def test_null_snapshot_is_corruption_not_missing_evidence(self):
        module = self.artifact_module()
        (self.root / 'job_1/artifact-exit.json').write_text('null')
        with self.assertRaises(ValueError):
            module.observe(self.root, offload_receipt.inspect(self.root, 'job_1'))

    def test_publication_missing_verified_artifacts_and_foreign_run_are_refused(self):
        module = self.artifact_module()
        from test_superagent_mail_obligation import inventory
        target = {**TARGET, 'terminal_handle': 'term_1', 'incarnation_id': 'process_1'}
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        with patch('orca_runtime.observe.collect', return_value=inventory()), \
                self.assertRaisesRegex(ValueError, 'verified returned artifacts'):
            module.publish(self.journal, target, 'job_1', caller, '/orca')
        record = offload_receipt.inspect(self.root, 'job_1')
        with patch('orca_runtime.observe.collect', return_value=inventory()), \
                patch.object(offload_receipt, 'inspect', return_value={**record, 'binding': {**record['binding'], 'run_id': 'foreign'}}), \
                self.assertRaisesRegex(ValueError, 'original Run'):
            module.publish(self.journal, target, 'job_1', caller, '/orca')

    def test_publication_with_fetch_keeps_exact_studio_and_review_only_boundaries(self):
        module = self.artifact_module()
        from superagent_queue import offload_artifact_fetch
        from test_superagent_mail_obligation import inventory
        self.verified()
        proof = module.observe(self.root, offload_receipt.inspect(self.root, 'job_1'))
        target = {**TARGET, 'terminal_handle': 'term_1', 'incarnation_id': 'process_1'}
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        with patch('orca_runtime.observe.collect', return_value=inventory()), \
                patch.object(offload_artifact_fetch, 'fetch', return_value={'artifact': proof}) as fetch:
            result = module.publish(self.journal, target, 'job_1', caller, '/orca', studio='existing-studio')
        fetch.assert_called_once_with(self.root, 'job_1', 'existing-studio')
        self.assertEqual(result['queued'], 3)
        self.assertFalse(result['capacity_released'])


if __name__ == '__main__':
    unittest.main()
