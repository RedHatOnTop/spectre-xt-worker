"""Late remote receipts add evidence without replaying commands or rewriting history."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import execution, offload_receipt, offload_source
from superagent_queue.driver import context_delivery
from superagent_queue.store import Journal
from test_superagent_offload_receipt import BINDING, COMMAND, remote_result

TARGET = {'run_id': 'run_1', 'terminal_handle': 'coordinator', 'worktree_id': 'repo::/work',
          'incarnation_id': 'coord-process', 'text': 'Review only existing offload evidence.'}


class OffloadReconcileTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'private/offloads'
        self.journal = Journal(self.root.parent / 'journal.sqlite3')
        self.addCleanup(self.journal.close)
        self.result = offload_receipt.run(self.root, 'job_1', BINDING, COMMAND, ['/bin/true'])
        self.job = self.root / 'job_1'
        self.receipt = self.job / 'command-result.json'

    def late_receipt(self, value=None):
        self.receipt.write_text(json.dumps(remote_result() if value is None else value))
        self.receipt.chmod(0o600)

    def ingest(self):
        cursor = self.journal.source_cursor('offload-source')
        page = offload_source.page(self.root, 'run_1', cursor)
        return self.journal.ingest_page('offload-source', cursor, page.cursor,
                                       [context_delivery(TARGET, row) for row in page.events])

    def test_late_remote_exit_preserves_wrapper_history_and_never_spawns(self):
        before = (self.job / 'exit.json').read_bytes()
        self.late_receipt()
        with patch.object(offload_receipt.subprocess, 'Popen') as spawn:
            result = offload_receipt.reconcile(self.root, 'job_1')
        spawn.assert_not_called()
        self.assertEqual(result['remote_result']['returncode'], 0)
        self.assertEqual((self.job / 'exit.json').read_bytes(), before)
        observed = offload_receipt.inspect(self.root, 'job_1')
        self.assertEqual(observed['remote_observation'], 'unverifiable')
        self.assertEqual(observed['reconciliation']['remote_result']['returncode'], 0)

    def test_late_result_creates_one_new_event_without_conflicting_original_event(self):
        self.assertEqual(self.ingest(), 1)
        original = self.journal.effects()[0]
        self.late_receipt()
        offload_receipt.reconcile(self.root, 'job_1')
        self.assertEqual(self.ingest(), 1)
        self.assertEqual(self.ingest(), 0)
        self.assertEqual(self.journal.effects()[0], original)
        effects = self.journal.effects()
        self.assertEqual(len(effects), 2)
        self.assertIn('offload-reconciled-', effects[1]['payload']['text'])
        self.assertIn('review_only', effects[1]['payload']['text'])
        self.assertNotIn('worker_done', effects[1]['payload']['text'])

    def test_duplicate_reconciliation_is_idempotent_and_conflicting_receipt_is_refused(self):
        self.late_receipt()
        first = offload_receipt.reconcile(self.root, 'job_1')
        self.assertEqual(offload_receipt.reconcile(self.root, 'job_1'), first)
        self.late_receipt(remote_result(code=7))
        with self.assertRaisesRegex(ValueError, 'conflict'):
            offload_receipt.reconcile(self.root, 'job_1')
        self.assertEqual(offload_receipt.inspect(self.root, 'job_1')['reconciliation'], first)

    def test_missing_foreign_or_public_receipt_never_closes_unknown_work(self):
        with self.assertRaises(FileNotFoundError):
            offload_receipt.reconcile(self.root, 'job_1')
        self.late_receipt(remote_result(job='foreign'))
        with self.assertRaisesRegex(ValueError, 'identity'):
            offload_receipt.reconcile(self.root, 'job_1')
        self.late_receipt()
        self.receipt.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private'):
            offload_receipt.reconcile(self.root, 'job_1')
        self.assertFalse((self.job / 'remote-exit.json').exists())

    def test_live_or_unrecorded_wrapper_cannot_be_reconciled_as_completed(self):
        original = self.result
        self.late_receipt()
        for state in ('live', 'unverifiable'):
            with self.subTest(state=state), patch.object(offload_receipt, 'inspect',
                    return_value={**original, 'observation': state}), self.assertRaisesRegex(ValueError, 'wrapper exit'):
                offload_receipt.reconcile(self.root, 'job_1')

    def test_reconciliation_storage_failure_preserves_retryable_read_without_command_replay(self):
        self.late_receipt()
        with patch.object(execution, 'persist', side_effect=OSError('reconciliation disk unavailable')), \
                self.assertRaisesRegex(OSError, 'disk unavailable'):
            offload_receipt.reconcile(self.root, 'job_1')
        self.assertFalse((self.job / 'remote-exit.json').exists())
        self.assertEqual(offload_receipt.reconcile(self.root, 'job_1')['remote_result']['returncode'], 0)

    def test_corrupt_reconciliation_binding_cannot_change_native_or_wrapper_evidence(self):
        self.late_receipt()
        original = offload_receipt.reconcile(self.root, 'job_1')
        path = self.job / 'remote-exit.json'
        for change in ({'intent_id': 'foreign'}, {'wrapper_exit_sha256': 'b' * 64},
                       {'remote_result': remote_result(job='other')}, {'recorded_at': None}):
            path.write_text(json.dumps({**original, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                offload_receipt.inspect(self.root, 'job_1')

    def test_pending_source_page_does_not_skip_late_remote_exit_on_wrap(self):
        for job in ('job_2', 'job_3'):
            offload_receipt.run(self.root, job, BINDING, COMMAND, ['/bin/true'])
        first = offload_source.page(self.root, 'run_1', None, limit=2)
        self.late_receipt()
        offload_receipt.reconcile(self.root, 'job_1')
        second = offload_source.page(self.root, 'run_1', first.cursor, limit=2)
        wrapped = offload_source.page(self.root, 'run_1', second.cursor, limit=2)
        self.assertEqual(sum(event['source'] == 'offload-reconciliation' for event in wrapped.events), 1)

    def test_competing_reconciliation_retains_the_first_committed_receipt(self):
        self.late_receipt()
        persist = execution.persist

        def other_writer(directory, name, value):
            persist(directory, name, {**value, 'recorded_at': value['recorded_at'] - 1})
            raise FileExistsError('other observation committed first')

        with patch.object(execution, 'persist', side_effect=other_writer):
            result = offload_receipt.reconcile(self.root, 'job_1', now=102)
        self.assertEqual(result['recorded_at'], 101)

    def test_competing_conflicting_receipt_cannot_be_overwritten(self):
        self.late_receipt()
        persist = execution.persist

        def other_writer(directory, name, value):
            persist(directory, name, {**value, 'remote_result': remote_result(code=7)})
            raise FileExistsError('other observation committed first')

        with patch.object(execution, 'persist', side_effect=other_writer), self.assertRaisesRegex(ValueError, 'conflict'):
            offload_receipt.reconcile(self.root, 'job_1')

    def test_completed_remote_receipt_cannot_be_reinterpreted_by_a_later_conflicting_result(self):
        original = offload_receipt.run(self.root, 'job_complete', BINDING, COMMAND, ['/bin/true'])
        path = self.root / 'job_complete/exit.json'
        completed = remote_result(job='job_complete')
        path.write_text(json.dumps({**original, 'remote_observation': 'exited', 'remote_result': completed}))
        remote_path = path.with_name('command-result.json')
        remote_path.write_text(json.dumps({**completed, 'returncode': 7}))
        remote_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            offload_receipt.reconcile(self.root, 'job_complete')
        self.assertFalse(path.with_name('remote-exit.json').exists())

    def test_source_row_limit_remains_bounded_when_each_row_has_two_evidence_events(self):
        self.late_receipt()
        offload_receipt.reconcile(self.root, 'job_1')
        page = offload_source.page(self.root, 'run_1', None, limit=1)
        self.assertLessEqual(len(page.events), 2)

    def test_actual_remote_helper_finishes_after_transport_exit_without_command_replay(self):
        helper = Path(__file__).resolve().parents[1] / 'scripts/offload-command.py'
        job = 'job_actual_late'
        command = [sys.executable, '-c', 'import time;print("late actual output");time.sleep(.25);raise SystemExit(7)']
        record = offload_receipt.run(self.root, job, BINDING, command, ['/bin/true'])
        path = self.root / job / 'command-result.json'
        output = self.root / job / 'actual-remote.log'
        with output.open('w') as log:
            process = subprocess.Popen([sys.executable, str(helper), '--job', job,
                                        '--sha256', record['command_sha256'], '--receipt', str(path), '--', *command],
                                       stdout=log, stderr=log, start_new_session=True)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not path.with_name(path.name + '.started').exists():
                if process.poll() is not None:
                    break
                time.sleep(.01)
            self.assertIsNone(process.poll())
            with self.assertRaises(FileNotFoundError):
                offload_receipt.reconcile(self.root, job)
            code = process.wait(timeout=5)
        self.assertEqual(code, 7, output.read_text())
        before = (path.parent / 'exit.json').read_bytes()
        proof = offload_receipt.reconcile(self.root, job)
        self.assertEqual(proof['remote_result']['returncode'], 7)
        self.assertIn('late actual output', output.read_text())
        self.assertEqual((path.parent / 'exit.json').read_bytes(), before)
        duplicate = subprocess.run([sys.executable, str(helper), '--job', job, '--sha256', record['command_sha256'],
                                    '--receipt', str(path), '--', *command], capture_output=True, text=True, timeout=5)
        self.assertEqual(duplicate.returncode, 2)
        self.assertEqual(output.read_text().count('late actual output'), 1)

    def test_publication_requires_current_owner_and_keeps_queue_fault_recoverable(self):
        from superagent_queue import offload_recovery
        from test_superagent_mail_obligation import inventory
        target = {**TARGET, 'terminal_handle': 'term_1', 'incarnation_id': 'process_1'}
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        self.late_receipt()
        with patch('orca_runtime.observe.collect', return_value=inventory()):
            with self.assertRaisesRegex(ValueError, 'current coordinator pane'):
                offload_recovery.publish(self.journal, target, 'job_1', {}, '/orca')
            result = offload_recovery.publish(self.journal, target, 'job_1', caller, '/orca')
        self.assertEqual(result['queued'], 2)
        self.assertFalse(result['capacity_released'])

    def test_publication_with_fetch_reads_only_configured_studio_without_changing_capacity(self):
        from superagent_queue import offload_recovery, offload_transport
        from test_superagent_mail_obligation import inventory
        target = {**TARGET, 'terminal_handle': 'term_1', 'incarnation_id': 'process_1'}
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        self.late_receipt()
        proof = offload_receipt.reconcile(self.root, 'job_1')
        with patch('orca_runtime.observe.collect', return_value=inventory()), \
                patch.object(offload_transport, 'fetch', return_value={'reconciliation': proof}) as fetched:
            result = offload_recovery.publish(self.journal, target, 'job_1', caller, '/orca', studio='existing-studio')
        fetched.assert_called_once_with(self.root, 'job_1', 'existing-studio')
        self.assertFalse(result['capacity_released'])


if __name__ == '__main__':
    unittest.main()
