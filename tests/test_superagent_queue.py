"""Crash-safe operational effects never replace native worker authority."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from superagent_queue.store import Journal


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'private' / 'effects.sqlite3'
        self.queue = Journal(self.path)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.queue.close)

    def enqueue(self, event='event_1', key='wake_1', lane='run_1', body=None):
        return self.queue.ingest('operator', event, {'text': event},
                                 key, 'wake', lane, body or {'run_id': 'run_1'}, now=100)

    def claim(self, now=100):
        return self.queue.claim('driver_1', now=now)

    def test_event_and_effect_are_committed_together(self):
        self.assertTrue(self.enqueue())
        self.assertEqual(self.queue.summary()['events'], 1)
        self.assertEqual(self.queue.summary()['effects']['pending'], 1)

    def test_identical_event_is_idempotent_across_reopen(self):
        self.enqueue()
        with Journal(self.path) as other:
            self.assertFalse(other.ingest('operator', 'event_1', {'text': 'event_1'},
                                         'wake_1', 'wake', 'run_1', {'run_id': 'run_1'}, now=101))
        self.assertEqual(self.queue.summary()['events'], 1)

    def test_conflicting_event_is_not_silently_replaced(self):
        self.enqueue()
        with self.assertRaisesRegex(ValueError, 'event identity conflict'):
            self.queue.ingest('operator', 'event_1', {'text': 'changed'},
                              'wake_1', 'wake', 'run_1', {'run_id': 'run_1'}, now=101)

    def test_replayed_event_cannot_change_delivery(self):
        self.enqueue()
        with self.assertRaisesRegex(ValueError, 'event identity conflict'):
            self.queue.ingest('operator', 'event_1', {'text': 'event_1'},
                              'different', 'wake', 'run_1', {'run_id': 'run_1'}, now=101)

    def test_effect_key_conflict_rolls_back_new_event(self):
        self.enqueue()
        with self.assertRaisesRegex(ValueError, 'effect identity conflict'):
            self.enqueue(event='event_2')
        self.assertEqual(self.queue.summary()['events'], 1)

    def test_claim_is_exclusive_across_connections(self):
        self.enqueue()
        first = self.claim()
        with Journal(self.path) as other:
            self.assertIsNone(other.claim('driver_2', now=100))
        self.assertEqual(first.payload, {'run_id': 'run_1'})

    def test_claim_can_skip_lanes_already_observed_this_tick(self):
        self.enqueue()
        self.enqueue(event='event_2', key='wake_2', lane='independent')
        self.assertEqual(self.queue.claim('driver_1', now=100, excluded_lanes=('run_1',)).key, 'wake_2')
        self.assertIsNone(self.queue.claim('driver_1', now=100, excluded_lanes=('run_1',)))

    def test_claim_has_no_timeout_based_relaunch(self):
        self.enqueue()
        self.claim()
        self.assertIsNone(self.claim(now=10**9))

    def test_claim_record_preserves_original_attempt_after_update(self):
        self.enqueue()
        claim = self.claim()
        self.queue.record_receipt(claim, {'request_id': 'native_1'})
        self.queue.replace(claim.key, {'revision': 2}, now=101)
        record = self.queue.claim_record(claim)
        self.assertEqual(record['receipt'], {'request_id': 'native_1'})
        self.assertEqual(record['started_at'], 100)
        self.assertEqual(record['status'], 'running')
        self.queue.complete(claim, {'request_id': 'native_1'}, now=102)
        self.assertIsNone(self.queue.claim_record(claim))

    def test_reconciliation_read_is_bounded_before_materializing_history(self):
        for index in range(4):
            self.enqueue(event=f'event_{index}', key=f'wake_{index}', lane=f'run_{index}')
            self.claim()
        self.queue.reconcile_running()
        self.assertEqual(len(self.queue.reconciliation_claims(limit=2)), 2)
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.queue.reconciliation_claims(limit=limit)

    def test_restart_preserves_uncertain_effect_for_reconciliation(self):
        self.enqueue()
        claim = self.claim()
        self.queue.close()
        with Journal(self.path) as other:
            self.assertEqual(other.reconcile_running(), 1)
            self.assertIsNone(other.claim('driver_2', now=200))
            self.assertEqual(other.summary()['effects']['reconciling'], 1)
            self.assertTrue(other.complete(claim, {'request_id': 'native_1'}, now=200))

    def test_restart_recovers_exact_original_claim_after_update(self):
        self.enqueue()
        claim = self.claim()
        self.queue.replace('wake_1', {'revision': 2}, now=101)
        self.queue.close()
        with Journal(self.path) as other:
            other.reconcile_running()
            recovered = other.reconciliation_claims()
            self.assertEqual(recovered, [claim])
            self.assertEqual(recovered[0].payload, {'run_id': 'run_1'})
            self.assertFalse(other.complete(recovered[0], {'request_id': 'native_1'}, now=102))
            self.assertEqual(other.claim('driver_2', now=102).payload, {'revision': 2})

    def test_attention_reason_survives_newer_payload(self):
        self.enqueue()
        claim = self.claim()
        self.queue.replace('wake_1', {'revision': 2}, now=101)
        self.queue.attention(claim, 'Authentication requires operator approval', now=102)
        self.assertEqual(self.queue.effects()[0]['error'], 'Authentication requires operator approval')

    def test_failed_transaction_leaves_neither_half_committed(self):
        self.queue.connection.execute('''CREATE TRIGGER reject_effect BEFORE INSERT ON effect
            BEGIN SELECT RAISE(ABORT, 'effect write failed'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'effect write failed'):
            self.enqueue()
        self.assertEqual(self.queue.summary()['events'], 0)
        self.assertEqual(sum(self.queue.summary()['effects'].values()), 0)

    def test_claim_payload_is_immutable_from_consumers(self):
        self.enqueue()
        claim = self.claim()
        edited = {**claim.payload, 'revision': 2}
        self.assertNotEqual(edited, claim.payload)
        self.assertEqual(claim.payload, {'run_id': 'run_1'})

    def test_repeated_uncertain_receipt_cannot_change_new_attempt(self):
        self.enqueue()
        claim = self.claim()
        self.queue.complete(claim, {'ok': True}, now=101)
        self.assertFalse(self.queue.uncertain(claim, 'late response', now=102))
        self.assertEqual(self.queue.summary()['effects']['delivered'], 1)

    def test_claim_replacement_waits_for_original_receipt(self):
        self.enqueue()
        claim = self.claim()
        self.queue.replace('wake_1', {'run_id': 'run_1', 'revision': 2}, now=101)
        self.assertIsNone(self.claim(now=102))
        self.assertFalse(self.queue.complete(claim, {'request_id': 'native_1'}, now=103))
        next_claim = self.claim(now=103)
        self.assertEqual(next_claim.generation, 2)
        self.assertEqual(next_claim.payload['revision'], 2)
        self.assertEqual(self.queue.attempts('wake_1')[0]['receipt'], {'request_id': 'native_1'})

    def test_identical_replacement_is_a_noop(self):
        self.enqueue()
        self.assertFalse(self.queue.replace('wake_1', {'run_id': 'run_1'}, now=101))
        self.assertEqual(self.claim().generation, 1)

    def test_old_receipt_cannot_settle_a_new_claim(self):
        self.enqueue()
        old = self.claim()
        self.queue.replace('wake_1', {'revision': 2}, now=101)
        self.queue.complete(old, {'request_id': 'old'}, now=102)
        current = self.claim(now=102)
        self.assertFalse(self.queue.complete(old, {'request_id': 'duplicate'}, now=103))
        self.assertTrue(self.queue.complete(current, {'request_id': 'new'}, now=104))

    def test_ordered_lane_waits_while_another_lane_can_run(self):
        self.enqueue()
        first = self.claim()
        self.enqueue(event='event_2', key='wake_2')
        self.enqueue(event='event_3', key='wake_3', lane='run_2')
        self.queue.retry(first, 'HTTP 503 upstream unavailable', now=100)
        independent = self.claim()
        self.assertEqual(independent.key, 'wake_3')
        self.queue.complete(independent, {'ok': True}, now=100)
        self.assertIsNone(self.claim(now=101))
        self.assertEqual(self.claim(now=102).key, 'wake_1')

    def test_backoff_is_bounded_and_literal_failure_survives(self):
        self.enqueue()
        now = 100
        delays = []
        for _ in range(8):
            claim = self.claim(now=now)
            due = self.queue.retry(claim, 'HTTP 429\nretry-after: 60', now=now)
            delays = [*delays, due - now]
            now = due
        self.assertEqual(delays, [2, 4, 8, 16, 32, 60, 60, 60])
        self.assertEqual(self.queue.effects()[0]['error'], 'HTTP 429\nretry-after: 60')

    def test_capacity_deferral_does_not_count_as_transport_failure(self):
        self.enqueue()
        self.queue.defer(self.claim(), now=100)
        effect = self.queue.effects()[0]
        self.assertEqual(effect['failures'], 0)
        self.assertEqual(effect['due_at'], 102)

    def test_attention_is_not_completion_or_automatic_retry(self):
        self.enqueue()
        self.queue.attention(self.claim(), 'Authentication requires operator approval', now=100)
        self.assertIsNone(self.claim(now=100000))
        self.assertEqual(self.queue.summary()['effects']['attention'], 1)

    def test_ambiguous_delivery_blocks_replay_even_after_replacement(self):
        self.enqueue()
        claim = self.claim()
        self.queue.uncertain(claim, 'response lost after input acceptance', now=101)
        self.queue.replace('wake_1', {'revision': 2}, now=102)
        self.assertIsNone(self.claim(now=100000))
        self.assertEqual(self.queue.summary()['effects']['reconciling'], 1)

    def test_completed_inputs_are_retained_as_audit_records(self):
        self.enqueue()
        self.queue.complete(self.claim(), {'request_id': 'native_1'}, now=101)
        self.assertEqual(self.queue.summary()['events'], 1)
        self.assertEqual(self.queue.summary()['effects']['delivered'], 1)
        self.assertIsNone(self.claim(now=200))

    def test_private_storage_and_schema(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_newer_schema_is_not_opened_or_migrated(self):
        self.queue.close()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('PRAGMA user_version=999')
        with self.assertRaisesRegex(ValueError, 'unsupported journal schema'):
            Journal(self.path)

    def test_symlink_storage_is_rejected(self):
        link = self.path.parent / 'link.sqlite3'
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'private regular'):
            Journal(link)

    def test_inputs_are_not_mutated(self):
        event, payload = {'text': 'request'}, {'run_id': 'run_1'}
        expected = json.dumps([event, payload], sort_keys=True)
        self.queue.ingest('operator', 'event_1', event, 'wake_1', 'wake', 'run_1', payload, now=100)
        self.assertEqual(json.dumps([event, payload], sort_keys=True), expected)

    def test_invalid_inputs_never_create_event_or_effect(self):
        for payload in ([], {'value': float('nan')}, {'text': 'x' * 65537}):
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(ValueError):
                self.queue.ingest('operator', 'event_1', {}, 'wake_1', 'wake', 'run_1', payload)
        for source in ('', '../operator', 'x' * 257):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.queue.ingest(source, 'event_1', {}, 'wake_1', 'wake', 'run_1', {})
        self.assertEqual(self.queue.summary()['events'], 0)

    def test_invalid_time_kind_error_and_missing_update_fail_explicitly(self):
        for now in (-1, float('inf'), True):
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.queue.ingest('operator', 'event_1', {}, 'wake_1', 'wake', 'run_1', {}, now=now)
        with self.assertRaisesRegex(ValueError, 'effect kind'):
            self.queue.ingest('operator', 'event_1', {}, 'wake_1', 'delete', 'run_1', {})
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            self.queue.replace('missing', {})
        self.enqueue()
        claim = self.claim()
        for error in ('', 'x' * 65537):
            with self.subTest(error_size=len(error)), self.assertRaises(ValueError):
                self.queue.retry(claim, error)
        self.queue.complete(claim, {}, now=101)
        with self.assertRaisesRegex(ValueError, 'no longer current'):
            self.queue.retry(claim, 'late error', now=102)

    def test_nonprivate_parent_and_file_are_not_opened(self):
        for target in ('parent', 'file'):
            with tempfile.TemporaryDirectory() as d:
                parent = Path(d) / 'private'
                parent.mkdir(mode=0o700)
                path = parent / 'unsafe.sqlite3'
                if target == 'parent':
                    parent.chmod(0o755)
                else:
                    path.touch(mode=0o644)
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'private'):
                    Journal(path)

    def test_unversioned_existing_database_is_not_overwritten(self):
        self.queue.close()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('PRAGMA user_version=0')
        with self.assertRaisesRegex(ValueError, 'unversioned existing tables'):
            Journal(self.path)

    def test_real_process_crash_does_not_lose_claim_or_reexecute(self):
        script = '''import os,sys
from pathlib import Path
from superagent_queue.store import Journal
with Journal(Path(sys.argv[1])) as queue:
    queue.ingest('operator','crash_event',{},'crash_wake','wake','run_1',{},now=100)
    queue.claim('dead_driver',now=100)
    os._exit(23)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.path)],
                                env={**os.environ, 'PYTHONPATH': str(SCRIPTS)},
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(self.queue.reconcile_running(), 1)
        self.assertIsNone(self.claim(now=10000))
        self.assertEqual(self.queue.summary()['events'], 1)


if __name__ == '__main__':
    unittest.main()
