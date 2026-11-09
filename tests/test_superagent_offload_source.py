"""Offload completion facts use transactional review-only delivery."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import driver, offload_receipt, offload_source
from superagent_queue.store import Journal
from test_superagent_offload_receipt import BINDING, COMMAND

TARGET = {'run_id': 'run_1', 'terminal_handle': 'coordinator', 'worktree_id': 'repo::/work',
          'incarnation_id': 'coord-process', 'text': 'Review only existing authorized jobs.'}


class OffloadSourceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private/journal.sqlite3'
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)
        self.root = self.path.parent / 'offloads'

    def execute(self, job='job_1'):
        return offload_receipt.run(self.root, job, BINDING, COMMAND, ['/bin/true'])

    def test_wrapper_exit_is_context_only_even_when_remote_outcome_is_unknown(self):
        self.execute()
        with patch.object(driver.sources, 'inbox', return_value=[]), patch.object(driver.sources, 'gates', return_value=[]):
            first = driver.collect_context(self.journal, {'coordinators': [TARGET]}, {}, '/orca', 100)
            second = driver.collect_context(self.journal, {'coordinators': [TARGET]}, {}, '/orca', 101)
        self.assertEqual((first['queued'], second['queued']), (1, 0))
        payload = self.journal.claim('driver', now=102).payload['text']
        self.assertIn('remote_observation', payload)
        self.assertIn('OS exit is not Task success', payload)
        self.assertIn('Context only', payload)

    def test_another_run_and_a_live_attempt_do_not_create_completion_events(self):
        self.execute()
        self.assertEqual(offload_source.page(self.root, 'other', None).events, ())
        offload_receipt.begin(self.root, 'job_live', BINDING, COMMAND)
        page = offload_source.page(self.root, 'run_1', None)
        self.assertEqual(len(page.events), 1)

    def test_page_commit_failure_retains_receipt_and_original_source_cursor(self):
        self.execute()
        self.journal.connection.execute('''CREATE TRIGGER reject_effect BEFORE INSERT ON effect
            BEGIN SELECT RAISE(ABORT, 'offload queue unavailable'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'offload queue unavailable'):
            driver.ingest_offloads(self.journal, TARGET, now=100)
        self.assertIsNone(self.journal.source_cursor('offloads-run_1'))
        self.assertEqual(len(offload_source.page(self.root, 'run_1', None).events), 1)

    def test_missing_source_is_empty_but_missing_original_intent_is_an_error(self):
        self.assertEqual(offload_source.page(self.root, 'run_1', None).events, ())
        self.root.mkdir(mode=0o700)
        (self.root / 'job_1').mkdir(mode=0o700)
        with self.assertRaises(FileNotFoundError):
            offload_source.page(self.root, 'run_1', None)

    def test_bounded_wrap_discovers_lower_id_completion_and_preserves_review_only_admission(self):
        for job in ('job_b', 'job_c', 'job_d'):
            self.execute(job)
        first = offload_source.page(self.root, 'run_1', None, limit=2)
        self.assertEqual(len(first.events), 2)
        self.execute('job_a')
        second = offload_source.page(self.root, 'run_1', first.cursor, limit=2)
        wrapped = offload_source.page(self.root, 'run_1', second.cursor, limit=2)
        self.assertEqual(wrapped.events[0]['content']['job_id'], 'job_a')
        self.assertEqual(wrapped.events[0]['content']['admission'], 'review_only')

    def test_invalid_limit_and_symlink_never_read_the_source(self):
        for limit in (0, 101, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                offload_source.page(self.root, 'run_1', None, limit=limit)
        self.root.mkdir(mode=0o700)
        link = self.root.parent / 'offload-link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            offload_source.page(link, 'run_1', None)

    def test_offload_queue_error_does_not_discard_independent_native_context(self):
        event = {'source': 'orca-inbox', 'event_id': 'msg_1', 'content': {'type': 'status'}}
        with patch.object(driver, 'ingest_offloads', side_effect=sqlite3.OperationalError('job store busy')), \
                patch.object(driver.sources, 'inbox', return_value=[event]), \
                patch.object(driver.sources, 'gates', return_value=[]):
            result = driver.collect_context(self.journal, {'coordinators': [TARGET]}, {}, '/orca', 100)
        self.assertEqual(result['queued'], 1)
        self.assertIn('job store busy', str(result['issues']))


if __name__ == '__main__':
    unittest.main()
