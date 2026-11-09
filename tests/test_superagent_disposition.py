"""Only the exact native owner can publish versioned continuation decisions."""
from pathlib import Path
import sys
import tempfile
import unittest
import sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import disposition
from superagent_queue.store import Journal

TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': 'Continue approved work.'}
CALLER = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'tab_1:leaf_1'}


def inventory():
    return {'runtime': {'runtime': {'reachable': True, 'state': 'ready'}},
            'runs': [{'id': 'run_1', 'legacy': 0, 'coordinator_handle': 'term_1'}],
            'terminals': [{'handle': 'term_1', 'incarnationId': 'process_1', 'worktreeId': 'repo::/work',
                           'paneKey': 'tab_1:leaf_1', 'connected': True, 'writable': True}],
            'worktrees': [{'worktreeId': 'repo::/work'}]}


class DispositionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private/journal.sqlite3'
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)

    def publish(self, value, revision=0, caller=None, data=None):
        return disposition.publish(self.journal, TARGET, value, revision,
                                   caller or CALLER, data or inventory(), now=100)

    def test_same_owner_publishes_and_reopen_preserves_current_state_and_history(self):
        value = {'state': 'waiting_approval', 'gate_ids': ['gate_1']}
        self.assertEqual(self.publish(value)['revision'], 1)
        with Journal(self.path) as reopened:
            current = reopened.current_dispositions()['run_1']
            self.assertEqual(current['body'], value)
            self.assertEqual(current['revision'], 1)
        self.publish({'state': 'working'}, revision=1)
        self.assertEqual(self.journal.current_dispositions()['run_1']['body'], {'state': 'working'})
        self.assertEqual(self.journal.connection.execute('SELECT COUNT(*) FROM disposition').fetchone()[0], 2)

    def test_stale_publication_cannot_overwrite_a_newer_decision(self):
        self.publish({'state': 'working'})
        with self.assertRaisesRegex(ValueError, 'revision changed'):
            self.publish({'state': 'finished'})
        self.assertEqual(self.journal.current_dispositions()['run_1']['body'], {'state': 'working'})

    def test_missing_or_other_pane_cannot_impersonate_native_coordinator(self):
        for caller in ({}, {'ORCA_TERMINAL_HANDLE': 'other', 'ORCA_PANE_KEY': 'tab_1:leaf_1'},
                       {**CALLER, 'ORCA_PANE_KEY': 'other'}):
            with self.subTest(caller=caller), self.assertRaisesRegex(ValueError, 'native owner'):
                disposition.publish(self.journal, TARGET, {'state': 'working'}, 0, caller, inventory(), now=100)
        self.assertEqual(self.journal.current_dispositions(), {})

    def test_native_incarnation_change_never_grants_publication(self):
        data = inventory()
        data = {**data, 'terminals': [{**data['terminals'][0], 'incarnationId': 'changed'}]}
        with self.assertRaisesRegex(ValueError, 'incarnation'):
            self.publish({'state': 'working'}, data=data)

    def test_unknown_fields_irrelevant_refs_and_invalid_revision_are_rejected(self):
        for value in ({'state': 'working', 'gate_ids': ['gate_1']}, {'state': 'working', 'extra': True},
                      {'state': 'waiting_provider', 'retry_at': True},
                      {'state': 'waiting_approval', 'gate_ids': ['gate_1', 'gate_1']},
                      {'state': 'waiting_job', 'job_ids': [{}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.publish(value)
        for revision in (-1, True, '0'):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.publish({'state': 'working'}, revision=revision)

    def test_journal_publication_validates_even_without_the_cli_boundary(self):
        for revision in (-1, True, '0'):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.journal.publish_disposition('run_1', {'state': 'working'}, revision, {}, now=100)
        with self.assertRaises(ValueError):
            self.journal.publish_disposition('run_1', {'state': 'unknown'}, 0, {}, now=100)

    def test_crash_before_disposition_commit_preserves_prior_revision(self):
        self.publish({'state': 'working'})
        self.journal.connection.execute('''CREATE TRIGGER reject_decision BEFORE INSERT ON disposition
            WHEN NEW.revision=2 BEGIN SELECT RAISE(ABORT, 'decision write failed'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'decision write failed'):
            self.publish({'state': 'waiting_provider', 'retry_at': 200}, revision=1)
        with Journal(self.path) as reopened:
            self.assertEqual(reopened.current_dispositions()['run_1']['revision'], 1)

    def test_published_state_overrides_only_its_run_in_private_baseline(self):
        baseline = {'run_1': {'state': 'working'}, 'run_2': {'state': 'waiting_job', 'job_ids': ['job_2']}}
        self.publish({'state': 'waiting_provider', 'retry_at': 200})
        current = disposition.effective(self.journal, baseline)
        self.assertEqual(current['run_1'], {'state': 'waiting_provider', 'retry_at': 200})
        self.assertEqual(current['run_2'], baseline['run_2'])
        self.assertEqual(baseline['run_1'], {'state': 'working'})


if __name__ == '__main__':
    unittest.main()
