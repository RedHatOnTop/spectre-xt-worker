"""Cleanup requires actual accepted completion and native reviewed mail, never OS exit alone."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from superagent_queue import driver, execution, mail_obligation, offload_artifact, offload_receipt
from superagent_queue.store import Journal
from test_superagent_offload_receipt import BINDING, COMMAND, remote_result
from test_superagent_mail_obligation import TARGET, CALLER, inventory

MESSAGE = {'id': 'message_1', 'type': 'worker_done', 'payload': json.dumps({
    'taskId': 'task_1', 'dispatchId': 'dispatch_1', 'outcome': 'succeeded'})}
REVIEW = {'check_request_id': 'check_1', 'ack_request_id': 'ack_1',
          'reviews': [{'message_id': 'message_1', 'action': 'released', 'request_id': 'release_1'}]}


class OffloadReviewTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name) / 'private'
        self.journal = Journal(self.base / 'journal.sqlite3')
        self.addCleanup(self.journal.close)
        self.root = self.base / 'offloads'
        packet = self.base / 'packet.json'
        packet.write_text(json.dumps({'schema_version': 1, 'command': COMMAND, 'artifacts': ['output']}))
        packet.chmod(0o600)
        binding = {**BINDING, 'packet_sha256': hashlib.sha256(packet.read_bytes()).hexdigest()}
        self.record = offload_receipt.run(self.root, 'job_1', binding, COMMAND, ['/bin/true'],
                                        authority={'packet_path': str(packet), 'studio': 'existing-studio'})
        execution.persist(self.root / 'job_1', 'command-result.json', remote_result())
        offload_receipt.reconcile(self.root, 'job_1')
        helper = offload_artifact.helper()
        self.artifacts = helper.prepare(self.base / 'offload-artifacts', 'job_1',
                                        self.record['command_sha256'], ['output'])
        (self.artifacts / 'output').write_text('actual artifact')
        helper.persist(self.artifacts / 'manifest.json', helper.manifest(self.artifacts, 'job_1',
                                                                      self.record['command_sha256'], ['output']))
        helper.verify(self.artifacts, 'job_1', self.record['command_sha256'], ['output'])
        self.artifact = offload_artifact.observe(self.root, offload_receipt.inspect(self.root, 'job_1'))
        self.data = {'message_id': 'message_1', 'artifact_sha256': hashlib.sha256(
            json.dumps(self.artifact, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        driver.enqueue_context(self.journal, TARGET, {'source': 'orca-inbox', 'event_id': 'message_1',
                                                    'content': MESSAGE})
        with patch('orca_runtime.observe.query', side_effect=self.query):
            mail_obligation.publish(self.journal, TARGET, REVIEW, CALLER, inventory(), '/exact/orca')

    def query(self, cli, words):
        self.assertEqual(cli, '/exact/orca')
        if words[:2] == ['orchestration', 'request-show']:
            request = words[words.index('--request') + 1]
            receipt = {'check_1': {'runId': 'run_1', 'deliveryId': 'delivery_1', 'messages': [MESSAGE]},
                       'ack_1': {'runId': 'run_1', 'acknowledged': 'delivery_1'},
                       'release_1': {'dispatchId': 'dispatch_1', 'state': 'released'}}[request]
            return {'requestId': request, 'state': 'completed', 'receipt': receipt,
                    'method': 'orchestration.workerRelease' if request == 'release_1' else 'orchestration.check'}
        if words[:2] == ['orchestration', 'worker-show']:
            return {'dispatch': {'id': 'dispatch_1', 'runId': 'run_1', 'taskId': 'task_1', 'status': 'completed'}}
        raise AssertionError(words)

    def module(self):
        from superagent_queue import offload_review
        return offload_review

    def prepare(self, data=None, caller=CALLER, query=None):
        module = self.module()
        with patch('orca_runtime.observe.collect', return_value=inventory()), \
                patch('orca_runtime.observe.query', side_effect=query or self.query):
            return module.prepare(self.journal, TARGET, 'job_1', data or self.data, caller, '/exact/orca')

    def test_exact_review_rechecks_native_receipts_and_actual_artifact_bytes(self):
        result = self.prepare()
        self.assertEqual(result['dispatch_id'], 'dispatch_1')
        self.assertEqual(result['artifact_sha256'], self.data['artifact_sha256'])
        self.assertEqual(result['mail_review']['review']['request_id'], 'release_1')
        self.assertFalse(result['native_mutations'])
        self.assertFalse(result['capacity_released'])

    def test_missing_review_wrong_message_foreign_owner_or_digest_cannot_authorize_cleanup(self):
        for data in ({**self.data, 'message_id': 'missing'}, {**self.data, 'artifact_sha256': 'b' * 64},
                     {**self.data, 'extra': 'approval'}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.prepare(data=data)
        with self.assertRaisesRegex(ValueError, 'current coordinator pane'):
            self.prepare(caller={})
        self.journal.connection.execute('DELETE FROM mail_review')
        with self.assertRaisesRegex(ValueError, 'reviewed native completion'):
            self.prepare()

    def test_current_native_dispatch_or_request_receipts_must_still_match_review(self):
        def changed(cli, words):
            row = self.query(cli, words)
            return {**row, 'dispatch': {**row['dispatch'], 'status': 'dispatched'}} if 'dispatch' in row else row
        with self.assertRaisesRegex(ValueError, 'outcome'):
            self.prepare(query=changed)

        def wrong_ack(cli, words):
            row = self.query(cli, words)
            return {**row, 'receipt': {**row['receipt'], 'acknowledged': 'foreign'}} if row.get('requestId') == 'ack_1' else row
        with self.assertRaisesRegex(ValueError, 'Delivery'):
            self.prepare(query=wrong_ack)

    def test_native_success_cannot_erase_remote_failure_or_changed_local_artifact(self):
        (self.artifacts / 'output').write_text('corrupted')
        with self.assertRaisesRegex(ValueError, 'tree differs'):
            self.prepare()
        (self.artifacts / 'output').write_text('actual artifact')
        observed = offload_receipt.inspect(self.root, 'job_1')
        from superagent_queue import offload_recovery
        changed = {**observed, 'reconciliation': {**observed['reconciliation'], 'remote_result': remote_result(code=7)}}
        changed_artifact = {**self.artifact, 'remote_result_sha256': hashlib.sha256(json.dumps(
            remote_result(code=7), sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        with patch.object(offload_receipt, 'inspect', return_value=changed), \
                patch.object(offload_artifact, 'observe', return_value=changed_artifact), self.assertRaises(ValueError):
            self.prepare()

    def test_reviewed_delivery_proof_must_match_entire_original_batch(self):
        row = self.journal.connection.execute('SELECT body FROM mail_review').fetchone()[0]
        body = json.loads(row)
        self.journal.connection.execute('UPDATE mail_review SET body=?', (json.dumps({**body,
            'proof': {**body['proof'], 'delivery_id': 'foreign'}}),))
        with self.assertRaisesRegex(ValueError, 'Delivery'):
            self.prepare()

    def test_refused_release_and_ordinary_status_mail_never_become_cleanup_authority(self):
        def changed(cli, words):
            row = self.query(cli, words)
            return {**row, 'receipt': {**row['receipt'], 'state': 'release_unknown'}} if row.get('requestId') == 'release_1' else row
        with self.assertRaisesRegex(ValueError, 'cleanup'):
            self.prepare(query=changed)
        self.journal.connection.execute('UPDATE event SET content=? WHERE source=?',
                                        (json.dumps({**MESSAGE, 'type': 'status'}), 'orca-inbox'))
        with self.assertRaisesRegex(ValueError, 'native completion'):
            self.prepare()

    def test_native_metadata_fields_do_not_change_original_source_projection(self):
        def enriched(cli, words):
            row = self.query(cli, words)
            if row.get('requestId') == 'check_1':
                row = {**row, 'receipt': {**row['receipt'], 'messages': [{**MESSAGE, 'created_at': 'native-time'}]}}
            return row
        stored = json.loads(self.journal.connection.execute('SELECT body FROM mail_review').fetchone()[0])
        receipt = enriched('/exact/orca', ['orchestration', 'request-show', '--request', 'check_1'])['receipt']
        stored = {**stored, 'proof': {**stored['proof'], 'receipt_sha256': hashlib.sha256(json.dumps(
            receipt, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}}
        self.journal.connection.execute('UPDATE mail_review SET body=?', (json.dumps(stored),))
        self.assertEqual(self.prepare(query=enriched)['dispatch_id'], 'dispatch_1')

    def test_native_failure_can_preserve_successful_command_exit_without_relabeling_it(self):
        message = {**MESSAGE, 'payload': json.dumps({'taskId': 'task_1', 'dispatchId': 'dispatch_1', 'outcome': 'failed'})}
        self.journal.connection.execute('UPDATE event SET content=? WHERE source=?', (json.dumps(message), 'orca-inbox'))
        stored = json.loads(self.journal.connection.execute('SELECT body FROM mail_review').fetchone()[0])
        def failed(cli, words):
            row = self.query(cli, words)
            if row.get('requestId') == 'check_1':
                return {**row, 'receipt': {**row['receipt'], 'messages': [message]}}
            if 'dispatch' in row:
                return {**row, 'dispatch': {**row['dispatch'], 'status': 'failed'}}
            return row
        checked = failed('/exact/orca', ['orchestration', 'request-show', '--request', 'check_1'])['receipt']
        stored = {**stored, 'proof': {**stored['proof'], 'receipt_sha256': hashlib.sha256(json.dumps(
            checked, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}}
        self.journal.connection.execute('UPDATE mail_review SET body=?', (json.dumps(stored),))
        self.assertEqual(self.prepare(query=failed)['dispatch_id'], 'dispatch_1')


if __name__ == '__main__':
    unittest.main()
