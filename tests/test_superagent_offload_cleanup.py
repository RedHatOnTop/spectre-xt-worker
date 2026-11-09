"""Remote tree cleanup commits review authority first and reconciles ambiguous transport."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from superagent_queue import execution, offload_artifact, offload_receipt
from superagent_queue.store import Journal
from test_superagent_offload_review import MESSAGE, REVIEW
from test_superagent_mail_obligation import TARGET, CALLER, inventory
from test_superagent_offload_receipt import BINDING, COMMAND, remote_result


class OffloadCleanupAdapterTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name) / 'private'
        self.journal = Journal(self.base / 'journal.sqlite3')
        self.addCleanup(self.journal.close)
        self.root = self.base / 'offloads'
        self.record = offload_receipt.run(self.root, 'job_1', BINDING, COMMAND, ['/bin/true'])
        execution.persist(self.root / 'job_1', 'command-result.json', remote_result())
        offload_receipt.reconcile(self.root, 'job_1')
        execution.persist(self.root / 'job_1', 'transport.json', {'schema_version': 1, 'job_id': 'job_1',
            'command_sha256': self.record['command_sha256'], 'studio': 'existing-studio',
            'remote_dir': 'offload/job_1', 'recorded_at': self.record['created_at']})
        self.review = {'job_id': 'job_1', 'dispatch_id': 'dispatch_1', 'artifact_sha256': 'a' * 64,
                       'mail_review': {'review': REVIEW['reviews'][0], 'proof': {'delivery_id': 'delivery_1'}},
                       'native_mutations': False, 'capacity_released': False}
        self.data = {'message_id': 'message_1', 'artifact_sha256': 'a' * 64}

    def module(self):
        from superagent_queue import offload_cleanup
        return offload_cleanup

    def completed(self, request):
        return {'schema_version': 1, 'state': 'completed', 'request_sha256': hashlib.sha256(json.dumps(
            request, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'tree_removed': True, 'studio_stopped': False, 'finished_at': 101}

    def execute(self, action='apply', response=None, data=None):
        module = self.module()
        def query(command, request):
            self.assertTrue((self.root / 'job_1/cleanup/intent.json').exists())
            return subprocess.CompletedProcess(command, 0, json.dumps(self.completed(request)), '')
        with patch.object(module.offload_review, 'prepare', return_value=self.review), \
                patch.object(module, 'query', side_effect=response or query):
            return module.publish(self.journal, TARGET, 'job_1', data or self.data, CALLER,
                                  '/exact/orca', 'existing-studio', reconcile=action == 'reconcile')

    def test_cleanup_writes_intent_before_ssh_and_keeps_native_capacity_and_history(self):
        before = (self.root / 'job_1/exit.json').read_bytes()
        result = self.execute()
        self.assertTrue(result['remote_tree_removed'])
        self.assertFalse(result['capacity_released'])
        self.assertFalse(result['native_mutations'])
        self.assertEqual((self.root / 'job_1/exit.json').read_bytes(), before)
        self.assertTrue((self.root / 'job_1/cleanup/exit.json').exists())

    def test_repeated_completed_cleanup_performs_no_remote_mutation(self):
        first = self.execute()
        module = self.module()
        with patch.object(module.offload_review, 'prepare', return_value=self.review), patch.object(module, 'query') as query:
            repeated = module.publish(self.journal, TARGET, 'job_1', self.data, CALLER, '/exact/orca', 'existing-studio')
        query.assert_not_called()
        self.assertEqual(repeated, first)

    def test_ambiguous_response_requires_inspection_before_same_identity_apply(self):
        module = self.module()
        def failure(command, request):
            return subprocess.CompletedProcess(command, 255, 'partial stdout', 'connection lost')
        with self.assertRaisesRegex(RuntimeError, 'connection lost'):
            self.execute(response=failure)
        with patch.object(module.offload_review, 'prepare', return_value=self.review), \
                patch.object(module, 'query') as query, self.assertRaisesRegex(ValueError, 'reconciliation'):
            module.publish(self.journal, TARGET, 'job_1', self.data, CALLER, '/exact/orca', 'existing-studio')
        query.assert_not_called()
        result = self.execute(action='reconcile')
        self.assertTrue(result['remote_tree_removed'])
        self.assertFalse(result['capacity_released'])

    def test_pending_inspection_does_not_release_or_retry_remote_operation(self):
        def lost(command, request):
            raise subprocess.TimeoutExpired(command, 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute(response=lost)
        def pending(command, request):
            self.assertEqual(command[-1], 'inspect')
            return subprocess.CompletedProcess(command, 0, json.dumps({'state': 'pending', 'tree_removed': False,
                'request_sha256': hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}), '')
        result = self.execute(action='reconcile', response=pending)
        self.assertFalse(result['remote_tree_removed'])
        self.assertEqual(result['state'], 'reconciling')
        self.assertFalse((self.root / 'job_1/cleanup/exit.json').exists())

    def test_foreign_studio_review_or_changed_original_request_never_queries_remote(self):
        module = self.module()
        with patch.object(module.offload_review, 'prepare', side_effect=ValueError('exact review missing')), \
                patch.object(module, 'query') as query, self.assertRaisesRegex(ValueError, 'review missing'):
            module.publish(self.journal, TARGET, 'job_1', self.data, CALLER, '/exact/orca', 'existing-studio')
        query.assert_not_called()
        with patch.object(module.offload_review, 'prepare', return_value=self.review), \
                patch.object(module, 'query') as query, self.assertRaisesRegex(ValueError, 'Studio'):
            module.publish(self.journal, TARGET, 'job_1', self.data, CALLER, '/exact/orca', 'foreign-studio')
        query.assert_not_called()
        self.execute()
        with patch.object(module.offload_review, 'prepare', return_value={**self.review, 'artifact_sha256': 'b' * 64}), \
                patch.object(module, 'query') as query, self.assertRaisesRegex(ValueError, 'original cleanup intent'):
            module.publish(self.journal, TARGET, 'job_1', self.data, CALLER, '/exact/orca', 'existing-studio')
        query.assert_not_called()

    def test_malformed_foreign_or_large_completion_receipt_never_releases_capacity(self):
        for output in ('invalid', 'x' * 65537, json.dumps({'schema_version': 1, 'state': 'completed',
                                                       'request_sha256': 'b' * 64, 'tree_removed': True})):
            with self.subTest(output=output[:20]), self.assertRaises((ValueError, RuntimeError)):
                self.execute(response=lambda command, request: subprocess.CompletedProcess(command, 0, output, ''))
            self.assertFalse((self.root / 'job_1/cleanup/exit.json').exists())
            import shutil
            shutil.rmtree(self.root / 'job_1/cleanup')

    def test_storage_failure_before_claimed_effect_leaves_no_remote_execution(self):
        module = self.module()
        original = execution.persist
        def failing(directory, name, value):
            if name == 'started.json':
                raise OSError('cleanup intent storage unavailable')
            return original(directory, name, value)
        with patch.object(module.offload_review, 'prepare', return_value=self.review), \
                patch.object(execution, 'persist', side_effect=failing), patch.object(module, 'query') as query, \
                self.assertRaisesRegex(OSError, 'storage unavailable'):
            module.publish(self.journal, TARGET, 'job_1', self.data, CALLER, '/exact/orca', 'existing-studio')
        query.assert_not_called()

    def test_cleanup_failure_is_durable_and_never_overwrites_first_ambiguous_attempt(self):
        def failure(command, request):
            return subprocess.CompletedProcess(command, 255, 'partial stdout verbatim', 'lost response verbatim')
        with self.assertRaises(RuntimeError):
            self.execute(response=failure)
        attempt = self.root / 'job_1/cleanup'
        failures = list(attempt.glob('failure-*.json'))
        self.assertEqual(len(failures), 1)
        self.assertIn('lost response verbatim', failures[0].read_text())
        before = failures[0].read_bytes()
        self.execute(action='reconcile')
        self.assertEqual(failures[0].read_bytes(), before)

    def test_null_exit_and_started_identity_mismatch_never_trigger_remote_apply(self):
        module = self.module()
        self.execute()
        directory = self.root / 'job_1/cleanup'
        (directory / 'exit.json').write_text('null')
        with patch.object(module.offload_review, 'prepare', return_value=self.review), \
                patch.object(module, 'query') as query, self.assertRaises(ValueError):
            module.publish(self.journal, TARGET, 'job_1', self.data, CALLER, '/exact/orca', 'existing-studio')
        query.assert_not_called()

    def test_cleanup_changed_helper_refuses_remote_connection(self):
        module = self.module()
        with patch.object(module, 'HELPER_SHA256', 'b' * 64), self.assertRaisesRegex(ValueError, 'reviewed bytes'):
            module.command('existing-studio', 'apply')

    def test_pending_reconciliation_resumes_only_explicit_exact_remote_ready_identity(self):
        module = self.module()
        def lost(command, request):
            raise subprocess.TimeoutExpired(command, 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute(response=lost)
        calls = []
        def ready(command, request):
            calls.append(command[-1])
            sha = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            value = {'state': 'pending', 'tree_removed': False, 'resume_ready': True, 'request_sha256': sha}
            return subprocess.CompletedProcess(command, 0, json.dumps(value if command[-1] == 'inspect'
                                                                     else self.completed(request)), '')
        result = self.execute(action='reconcile', response=ready)
        self.assertEqual(calls, ['inspect', 'apply'])
        self.assertTrue(result['remote_tree_removed'])

    def test_absent_remote_claim_never_authorizes_apply_even_with_untrusted_resume_field(self):
        def lost(command, request):
            raise subprocess.TimeoutExpired(command, 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute(response=lost)
        calls = []
        def absent(command, request):
            calls.append(command[-1])
            return subprocess.CompletedProcess(command, 0, json.dumps({'state': 'absent', 'tree_removed': False,
                'resume_ready': True, 'request_sha256': hashlib.sha256(json.dumps(request, sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest()}), '')
        result = self.execute(action='reconcile', response=absent)
        self.assertEqual(calls, ['inspect'])
        self.assertEqual(result['state'], 'reconciling')

    def test_started_receipt_identity_is_checked_before_reconciliation(self):
        module = self.module()
        def lost(command, request):
            raise subprocess.TimeoutExpired(command, 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute(response=lost)
        path = self.root / 'job_1/cleanup/started.json'
        original = json.loads(path.read_text())
        path.write_text(json.dumps({**original, 'intent_sha256': 'b' * 64}))
        with patch.object(module.offload_review, 'prepare', return_value=self.review), \
                patch.object(module, 'query') as query, self.assertRaisesRegex(ValueError, 'started'):
            module.publish(self.journal, TARGET, 'job_1', self.data, CALLER, '/exact/orca', 'existing-studio', reconcile=True)
        query.assert_not_called()

    def test_native_cleanup_connects_exact_capacity_release_only_after_completed_receipt(self):
        module = self.module()
        from superagent_queue import offload_capacity
        execution.persist(self.root / 'job_1', 'capacity.json', {'stage': 'fixture'})
        with patch.object(offload_capacity, 'release', return_value={'capacity_released': True, 'studio_state': 'retained_shared'}) as release:
            result = self.execute()
        self.assertTrue(result['capacity_released'])
        self.assertEqual(result['resources']['studio_state'], 'retained_shared')
        self.assertTrue(release.call_args.args[2]['receipt']['tree_removed'])

    def test_cleanup_source_event_survives_queue_failure_and_does_not_change_prior_outcome_event(self):
        from superagent_queue import driver
        from test_superagent_offload_source import TARGET as SOURCE_TARGET
        self.assertEqual(driver.ingest_offloads(self.journal, SOURCE_TARGET), 2)
        original = self.journal.effects()
        self.execute()
        self.assertEqual(driver.ingest_offloads(self.journal, SOURCE_TARGET), 1)
        self.assertEqual(driver.ingest_offloads(self.journal, SOURCE_TARGET), 0)
        self.assertEqual(self.journal.effects()[:2], original)
        self.assertIn('offload-cleanup', self.journal.effects()[-1]['payload']['text'])

    def test_cleanup_source_failure_keeps_original_cursor_and_recovers_same_event(self):
        from superagent_queue import driver
        from test_superagent_offload_source import TARGET as SOURCE_TARGET
        self.execute()
        self.journal.connection.execute('''CREATE TRIGGER block_cleanup_event BEFORE INSERT ON effect
            BEGIN SELECT RAISE(ABORT, 'cleanup source unavailable'); END''')
        import sqlite3
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'cleanup source unavailable'):
            driver.ingest_offloads(self.journal, SOURCE_TARGET)
        self.assertIsNone(self.journal.source_cursor('offloads-run_1'))
        self.assertTrue((self.root / 'job_1/cleanup/exit.json').exists())
        self.journal.connection.execute('DROP TRIGGER block_cleanup_event')
        self.assertEqual(driver.ingest_offloads(self.journal, SOURCE_TARGET), 3)

    def test_null_or_changed_cleanup_source_cannot_publish_completion(self):
        from superagent_queue import offload_cleanup_source
        self.execute()
        path = self.root / 'job_1/cleanup/exit.json'
        path.write_text('null')
        with self.assertRaises(ValueError):
            offload_cleanup_source.read(self.root, offload_receipt.inspect(self.root, 'job_1'))

    def test_cleanup_source_rejects_changed_transport_or_cleanup_request(self):
        from superagent_queue import offload_cleanup_source
        self.execute()
        path = self.root / 'job_1/cleanup/intent.json'
        original = json.loads(path.read_text())
        for change in ({'transport': {**original['transport'], 'remote_dir': 'offload/foreign'}},
                       {'request': {**original['request'], 'remote_result_sha256': 'b' * 64}}):
            changed = {**original, **change}
            path.write_text(json.dumps(changed))
            exit_path = path.with_name('exit.json')
            value = json.loads(exit_path.read_text())
            exit_path.write_text(json.dumps({**value, 'intent_sha256': hashlib.sha256(json.dumps(
                changed, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                offload_cleanup_source.read(self.root, offload_receipt.inspect(self.root, 'job_1'))


if __name__ == '__main__':
    unittest.main()
