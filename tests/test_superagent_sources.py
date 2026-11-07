"""Existing intake and native inbox reads provide context, never new authority."""
from pathlib import Path
from contextlib import closing
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import sources
from superagent_queue.store import Journal

TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': 'Continue only approved work.'}


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'intake.sqlite'
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('''CREATE TABLE todos (id TEXT PRIMARY KEY, message_id TEXT, state TEXT,
                kind TEXT, title TEXT, summary TEXT, accepted_by TEXT, updated_at TEXT)''')
            connection.executemany('INSERT INTO todos VALUES (?,?,?,?,?,?,?,?)', [
                ('todo_1', 'msg_1', 'queued', 'bug', 'Repair existing defect', 'approved scope', 'operator_1', '2026-11-02T00:00:00Z'),
                ('todo_2', 'msg_2', 'deferred', 'proposal', 'Unapproved proposal', 'scope expansion', None, '2026-11-02T00:00:00Z')])
        self.path.chmod(0o600)
        self.queue = Journal(self.root / 'private/journal.sqlite3')
        self.addCleanup(self.queue.close)

    def test_existing_intake_read_does_not_change_ledger_or_approve_proposals(self):
        before = self.path.read_bytes()
        events = sources.intake(self.path)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['content']['todo_id'], 'todo_1')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn('approve', events[0])

    def test_new_unreviewed_intake_is_context_and_completion_reviewer_is_not_permission(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE todos SET accepted_by=NULL WHERE id='todo_1'")
        events = sources.intake(self.path)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['content']['admission'], 'review_only')
        self.assertIsNone(events[0]['content']['completion_reviewer'])
        self.assertNotIn('accepted_by', events[0]['content'])
        self.assertNotIn('approval', events[0]['content'])

    def test_terminal_source_states_do_not_reenter_review(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE todos SET state='done' WHERE id='todo_1'")
            connection.execute("UPDATE todos SET state='rejected' WHERE id='todo_2'")
        self.assertEqual(sources.intake(self.path), [])

    def test_source_event_ids_are_stable_and_revision_specific(self):
        first = sources.intake(self.path)[0]['event_id']
        self.assertEqual(first, sources.intake(self.path)[0]['event_id'])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE todos SET updated_at='2026-11-02T01:00:00Z' WHERE id='todo_1'")
        self.assertNotEqual(first, sources.intake(self.path)[0]['event_id'])

    def test_incompatible_intake_schema_is_an_error_not_empty_inbox(self):
        broken = self.root / 'broken.sqlite'
        with closing(sqlite3.connect(broken)) as connection, connection:
            connection.execute('CREATE TABLE unrelated (id TEXT)')
        broken.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'intake schema'):
            sources.intake(broken)

    def test_native_inbox_is_peeked_without_consumption_or_ack(self):
        with patch('superagent_queue.sources.observe.query', return_value={
                'runId': 'run_1', 'messages': [{'id': 'msg_1', 'type': 'question', 'body': 'Need existing gate'}]}) as query:
            events = sources.inbox('/orca', TARGET)
        self.assertEqual(events[0]['event_id'], 'msg_1')
        self.assertIn('--peek', query.call_args.args[1])
        self.assertNotIn('--ack', query.call_args.args[1])

    def test_native_inbox_for_another_run_is_rejected(self):
        with patch('superagent_queue.sources.observe.query', return_value={'runId': 'other', 'messages': []}):
            with self.assertRaisesRegex(ValueError, 'native inbox'):
                sources.inbox('/orca', TARGET)

    def test_invalid_native_message_is_an_explicit_source_error_not_partial_admission(self):
        for row in (None, 'message', {}, {'id': None}, {'id': 'invalid id'}):
            with self.subTest(row=row), patch('superagent_queue.sources.observe.query', return_value={
                    'runId': 'run_1', 'messages': [{'id': 'valid', 'type': 'status'}, row]}):
                with self.assertRaises(ValueError):
                    sources.inbox('/orca', TARGET)

    def test_gate_resolution_wakes_waiting_approval_without_resolving_gate(self):
        disposition = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}
        with patch('superagent_queue.sources.observe.query', return_value={
                'runId': 'run_1', 'gates': [{'id': 'gate_1', 'status': 'resolved', 'resolution': 'Keep read-only'}]}) as query:
            events = sources.gates('/orca', TARGET, disposition)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['content']['resolution'], 'Keep read-only')
        self.assertEqual(query.call_args.args[1][:2], ['orchestration', 'gate-list'])

    def test_pending_gate_does_not_generate_repeated_wake(self):
        disposition = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}
        with patch('superagent_queue.sources.observe.query', return_value={
                'runId': 'run_1', 'gates': [{'id': 'gate_1', 'status': 'pending'}]}):
            self.assertEqual(sources.gates('/orca', TARGET, disposition), [])


if __name__ == '__main__':
    unittest.main()
