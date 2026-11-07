"""Source pages and cursor advancement survive restart as one transaction."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import driver, sources
from superagent_queue.store import Journal

TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': 'Continue approved work.'}


class CursorTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'intake.sqlite'
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute('''CREATE TABLE todos (id TEXT PRIMARY KEY, message_id TEXT, state TEXT,
                kind TEXT, title TEXT, summary TEXT, accepted_by TEXT, updated_at TEXT)''')
            connection.executemany('INSERT INTO todos VALUES (?,?,?,?,?,?,?,?)', [
                (f'todo_{index:04}', f'msg_{index}', 'queued', 'bug', 'Existing authorized work',
                 'Bounded fixture scope', 'operator_1', '2026-11-02T00:00:00Z') for index in range(205)])
        self.source.chmod(0o600)
        self.path = self.root / 'private/journal.sqlite3'
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)

    def collect(self, journal=None):
        return driver.ingest_intake(journal or self.journal, TARGET, self.source, now=100)

    def test_more_than_one_hundred_rows_are_reached_after_process_reopen(self):
        before = self.source.read_bytes()
        self.assertEqual(self.collect(), 100)
        with Journal(self.path) as reopened:
            self.assertEqual(self.collect(reopened), 100)
            self.assertEqual(self.collect(reopened), 5)
            self.assertEqual(self.collect(reopened), 0)
        self.assertEqual(self.journal.summary()['events'], 205)
        self.assertEqual(self.source.read_bytes(), before)

    def test_growth_cannot_prevent_wrap_or_hide_backdated_changed_rows(self):
        self.collect()
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("UPDATE todos SET summary='Updated existing mandate', updated_at='2026-11-01' WHERE id='todo_0001'")
            connection.execute("INSERT INTO todos VALUES ('todo_9999','msg_new','queued','bug','New input','scope','operator_1','2026-11-02')")
        self.assertEqual(self.collect(), 100)
        self.assertEqual(self.collect(), 5)
        self.assertEqual(self.collect(), 1)
        self.collect()
        self.assertEqual(self.collect(), 1)
        self.assertEqual(self.journal.summary()['events'], 207)

    def test_lower_identifier_insert_is_seen_on_the_next_sweep(self):
        self.collect()
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("INSERT INTO todos VALUES ('todo_0000a','msg_lower','queued','bug','New input','scope','operator_1','2026-11-02')")
        self.collect()
        self.collect()
        self.assertEqual(self.collect(), 1)

    def test_terminal_rows_advance_cursor_but_never_reenter_review(self):
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("UPDATE todos SET state='done' WHERE id<'todo_0100'")
        self.assertEqual(self.collect(), 0)
        self.assertEqual(self.collect(), 100)
        self.assertEqual(self.journal.summary()['events'], 100)

    def test_page_failure_rolls_back_events_effects_and_cursor(self):
        self.journal.connection.execute('''CREATE TRIGGER reject_event BEFORE INSERT ON event
            WHEN NEW.event_id = 'reject' BEGIN SELECT RAISE(ABORT, 'page rejected'); END''')
        event = {'source': 'discord-intake', 'event_id': 'reject', 'content': {'text': 'fixture'}}
        deliveries = [driver.context_delivery(TARGET, {'source': 'discord-intake', 'event_id': 'ok', 'content': {}}),
                      driver.context_delivery(TARGET, event)]
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'page rejected'):
            self.journal.ingest_page('source_1', None, {'last_id': 'todo_0099', 'upper_id': 'todo_0204'}, deliveries, now=100)
        self.assertEqual(self.journal.summary()['events'], 0)
        self.assertEqual(sum(self.journal.summary()['effects'].values()), 0)
        self.assertIsNone(self.journal.source_cursor('source_1'))

    def test_stale_page_is_rejected_before_accepting_its_events(self):
        cursor = {'last_id': 'todo_0099', 'upper_id': 'todo_0204'}
        self.journal.ingest_page('source_1', None, cursor, [], now=100)
        with Journal(self.path) as other, self.assertRaisesRegex(ValueError, 'cursor changed'):
            other.ingest_page('source_1', None, cursor,
                [driver.context_delivery(TARGET, {'source': 'discord-intake', 'event_id': 'stale', 'content': {}})], now=100)
        self.assertEqual(self.journal.summary()['events'], 0)

    def test_source_read_uses_a_fixed_upper_bound_and_private_ledger(self):
        page = sources.intake_page(self.source, None)
        self.assertEqual(len(page.events), 100)
        self.assertEqual(page.cursor, {'last_id': 'todo_0099', 'upper_id': 'todo_0204'})
        self.source.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private'):
            sources.intake_page(self.source, page.cursor)

    def test_legacy_journal_migrates_without_losing_pending_effects(self):
        self.journal.ingest('operator', 'kept', {}, 'kept', 'wake', 'run_1', TARGET, now=100)
        self.journal.close()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('DROP TABLE source_cursor')
            connection.execute('DROP TABLE disposition')
            connection.execute('DROP TABLE mail_review')
            connection.execute('DROP TABLE mail_obligation')
            connection.execute('PRAGMA user_version=1')
        with Journal(self.path) as reopened:
            self.assertEqual(reopened.summary()['effects']['pending'], 1)
            self.assertEqual(self.collect(reopened), 100)
            self.assertEqual(reopened.connection.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_invalid_cursor_cannot_skip_the_source(self):
        for cursor in ({'last_id': 'todo_9999', 'upper_id': 'todo_0001'},
                       {'last_id': None, 'upper_id': 'todo_0001'},
                       {'last_id': 'todo_0001', 'upper_id': None}, {'unknown': True}, {}, [], False):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                sources.intake_page(self.source, cursor)


if __name__ == '__main__':
    unittest.main()
