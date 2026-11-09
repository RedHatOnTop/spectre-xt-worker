"""Concrete execution exits become durable review context, never native outcomes."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import execution, execution_source
from superagent_queue.driver import context_delivery
from superagent_queue.store import Journal

BINDING = {'run_id': 'run_1', 'task_id': 'task_1', 'dispatch_id': 'dispatch_1',
           'terminal_handle': 'term_1', 'pane_key': 'tab_1:leaf_1', 'worktree_id': 'repo::/work',
           'workspace_path': '/work', 'incarnation_id': 'pane_process_1',
           'route': 'clinepass', 'packet_sha256': 'a' * 64}
TARGET = {'run_id': 'run_1', 'terminal_handle': 'coordinator_1', 'worktree_id': 'repo::/coord',
          'incarnation_id': 'coordinator_process', 'text': 'Continue authorized work.'}


class ExecutionSourceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'executions'
        self.journal = Journal(Path(self.directory.name) / 'private/journal.sqlite3')
        self.addCleanup(self.journal.close)

    def test_actual_exit_is_review_only_and_reopen_does_not_duplicate_delivery(self):
        execution.run(self.root, BINDING, ['/bin/true'])
        for _ in range(2):
            cursor = self.journal.source_cursor('execution-source')
            page = execution_source.page(self.root, 'run_1', cursor)
            self.journal.ingest_page('execution-source', cursor, page.cursor,
                                    [context_delivery(TARGET, event) for event in page.events])
        self.assertEqual(self.journal.summary()['events'], 1)
        event = page.events[0]
        self.assertEqual(event['content']['admission'], 'review_only')
        self.assertEqual(event['content']['returncode'], 0)
        self.assertEqual(event['content']['task_id'], 'task_1')
        self.assertNotIn('outcome', event['content'])

    def test_other_run_receipts_do_not_enter_this_coordinator(self):
        execution.run(self.root, BINDING, ['/bin/true'])
        self.assertEqual(execution_source.page(self.root, 'other_run', None).events, ())

    def test_live_execution_does_not_wake_or_spend_another_model_turn(self):
        import subprocess
        from unittest.mock import patch
        proc = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        with patch('superagent_queue.execution.subprocess.Popen', return_value=proc), \
                patch.object(proc, 'wait', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            execution.run(self.root, BINDING, ['fixture'])
        self.assertEqual(execution_source.page(self.root, 'run_1', None).events, ())

    def test_concrete_failure_survives_queue_write_failure_for_later_recovery(self):
        execution.run(self.root, BINDING, ['/missing-runtime'])
        page = execution_source.page(self.root, 'run_1', None)
        self.assertEqual(page.events[0]['content']['returncode'], 127)
        self.journal.connection.execute('''CREATE TRIGGER reject_effect BEFORE INSERT ON effect
            BEGIN SELECT RAISE(ABORT, 'queue unavailable'); END''')
        import sqlite3
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'queue unavailable'):
            self.journal.ingest_page('execution-source', None, page.cursor,
                                    [context_delivery(TARGET, event) for event in page.events])
        self.assertIsNone(self.journal.source_cursor('execution-source'))
        self.assertEqual(execution_source.page(self.root, 'run_1', None).events, page.events)

    def test_bounded_sweep_and_next_wrap_discover_new_lower_id_exit(self):
        for name in ('dispatch_b', 'dispatch_c', 'dispatch_d'):
            execution.run(self.root, {**BINDING, 'dispatch_id': name}, ['/bin/true'])
        page = execution_source.page(self.root, 'run_1', None, limit=2)
        self.assertEqual(len(page.events), 2)
        execution.run(self.root, {**BINDING, 'dispatch_id': 'dispatch_a'}, ['/bin/true'])
        final = execution_source.page(self.root, 'run_1', page.cursor, limit=2)
        self.assertEqual(len(final.events), 1)
        wrapped = execution_source.page(self.root, 'run_1', final.cursor, limit=2)
        self.assertEqual(wrapped.events[0]['content']['dispatch_id'], 'dispatch_a')

    def test_missing_original_intent_surfaces_attention_not_false_empty_source(self):
        self.root.mkdir(mode=0o700)
        (self.root / 'dispatch_1').mkdir(mode=0o700)
        with self.assertRaises(FileNotFoundError):
            execution_source.page(self.root, 'run_1', None)

    def test_invalid_cursor_or_symlink_source_is_refused(self):
        self.root.mkdir(mode=0o700)
        for cursor in ({}, {'last_id': None, 'upper_id': 'dispatch_z'},
                       {'last_id': 'dispatch_z', 'upper_id': 'dispatch_a'}):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                execution_source.page(self.root, 'run_1', cursor)
        link = Path(self.directory.name) / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            execution_source.page(link, 'run_1', None)


if __name__ == '__main__':
    unittest.main()
