"""Orca packet-tab tidy: only tabs the bridge recorded are ever touched."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_plane import tidy

NOW = 1_800_000_000.0


def refuse_run(argv, **_kwargs):
    raise AssertionError(argv)


class TidyCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.packet_dir = self.root / 'packets'
        (self.packet_dir / 'tabs').mkdir(parents=True)

    def record(self, handle, **fields):
        record = {'handle': handle, 'role': 'flash', 'kind': 'job', 'dispatch_id': None,
                  'cwd': '/work/mc', 'worktree_id': 'repo-1::/work/mc',
                  'created_at': int(NOW) - 3600, **fields}
        path = self.packet_dir / 'tabs' / f'{handle}.json'
        path.write_text(json.dumps(record) + '\n')
        return path

    def exit_sidecar(self, directory, dispatch_id, age):
        path = directory / f'{dispatch_id}.exit'
        path.write_text('0\n')
        os.utime(path, (NOW - age, NOW - age))


class SelectTest(TidyCase):
    def test_recorded_job_is_selected_after_exit_and_grace(self):
        self.record('term_job', dispatch_id='d-1')
        self.record('term_running', dispatch_id='d-2')
        self.record('term_pinned_job', dispatch_id='d-1')
        self.record('term_shell', kind='shell', role='mimo')
        self.exit_sidecar(self.packet_dir, 'd-1', 400)
        terms = [{'handle': 'term_job', 'title': 'person@spectre: ~/x'},
                 {'handle': 'term_running', 'title': 'flash d-2'},
                 {'handle': 'term_pinned_job', 'title': 'flash d-1'},
                 {'handle': 'term_shell', 'title': 'mimo-packets'},
                 {'handle': 'term_untracked', 'title': 'flash d-1'},
                 {'handle': 'term_untracked_shell', 'title': 'flash-packets'}]
        self.assertEqual(tidy.select(terms, {'term_pinned_job'}, self.packet_dir, NOW),
                         [{'handle': 'term_job', 'role': 'flash', 'dispatch_id': 'd-1',
                           'reason': 'job_exit:d-1:400'}])

    def test_fresh_exit_is_kept(self):
        self.record('term_job', dispatch_id='d-9')
        self.exit_sidecar(self.packet_dir, 'd-9', 10)
        self.assertEqual(tidy.select([{'handle': 'term_job'}], set(), self.packet_dir, NOW), [])

    def test_malformed_records_are_ignored_and_kept(self):
        tabs = self.packet_dir / 'tabs'
        (tabs / 'term_a.json').write_text('{not json')
        (tabs / 'term_b.json').write_text('[1]')
        (tabs / 'term_c.json').write_text(json.dumps(
            {'handle': 'term_other', 'kind': 'job', 'dispatch_id': 'd-1'}))
        (tabs / 'bad name.json').write_text(json.dumps(
            {'handle': 'bad name', 'kind': 'job', 'dispatch_id': 'd-1'}))
        self.record('term_d', dispatch_id='../d-1')
        self.exit_sidecar(self.packet_dir, 'd-1', 400)
        self.exit_sidecar(self.root, 'd-1', 400)
        terms = [{'handle': handle} for handle in ('term_a', 'term_b', 'term_c', 'term_d')]
        self.assertEqual(list(tidy.records(self.packet_dir)), ['term_d'])
        self.assertEqual(tidy.select(terms, set(), self.packet_dir, NOW), [])
        self.assertEqual(len(list(tabs.iterdir())), 5)

    def test_missing_record_directory_selects_nothing(self):
        self.assertEqual(tidy.records(self.root / 'absent'), {})
        self.assertEqual(tidy.select([{'handle': 'term_job'}], set(), self.root / 'absent', NOW), [])


class StaleTest(TidyCase):
    def test_only_old_records_of_unlisted_tabs_are_stale(self):
        self.record('term_listed', created_at=int(NOW) - 900)
        self.record('term_young', created_at=int(NOW) - 10)
        self.record('term_gone', kind='shell', created_at=int(NOW) - 400)
        self.record('term_bad', created_at='yesterday')
        self.assertEqual(tidy.stale([{'handle': 'term_listed'}], self.packet_dir, NOW),
                         [{'handle': 'term_gone', 'role': 'flash', 'kind': 'shell',
                           'dispatch_id': None, 'reason': 'tab_gone'}])


class RetireTest(TidyCase):
    def test_retire_unlinks_and_tolerates_a_missing_record(self):
        path = self.record('term_job')
        self.assertEqual(tidy.retire('term_job', self.packet_dir), {'ok': True, 'handle': 'term_job'})
        self.assertFalse(path.exists())
        self.assertEqual(tidy.retire('term_job', self.packet_dir), {'ok': True, 'handle': 'term_job'})

    def test_bad_handle_is_refused(self):
        outside = self.packet_dir / 'x.json'
        outside.write_text('{}')
        self.assertEqual(tidy.retire('../x', self.packet_dir),
                         {'ok': False, 'handle': '../x', 'error': 'bad_handle'})
        self.assertTrue(outside.exists())

    def test_unlink_failure_is_reported(self):
        (self.packet_dir / 'tabs' / 'term_dir.json').mkdir()
        out = tidy.retire('term_dir', self.packet_dir)
        self.assertEqual(out, {'ok': False, 'handle': 'term_dir',
                               'error': 'record_retire_failed: Is a directory'})


class CloseTest(unittest.TestCase):
    def test_close_invokes_orca_tab_close(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return {'ok': True, 'parsed': {'ok': True}}

        self.assertTrue(tidy.close('term_x', run=fake_run)['ok'])
        self.assertEqual(calls, [
            ['orca-ide', 'terminal', 'send', '--terminal', 'term_x', '--interrupt'],
            ['orca-ide', 'terminal', 'send', '--terminal', 'term_x', '--text',
             tidy.TERM_RESET_LINE, '--enter'],
            ['orca-ide', 'terminal', 'close', '--terminal', 'term_x', '--tab', '--json'],
        ])

    def test_reset_rejects_a_bad_handle(self):
        out = tidy.reset('not-a-handle', run=refuse_run)
        self.assertEqual((out['ok'], out['error']), (False, 'bad_handle'))

    def test_close_failure_is_an_error(self):
        out = tidy.close('term_x', run=lambda argv, **kwargs: {'ok': False})
        self.assertEqual((out['ok'], out['error']), (False, 'orca_close_failed'))


class SweepTest(TidyCase):
    def setUp(self):
        super().setUp()
        self.job = self.record('term_job', dispatch_id='d-1')
        self.gone = self.record('term_gone', kind='shell', created_at=int(NOW) - 400)
        self.exit_sidecar(self.packet_dir, 'd-1', 400)
        self.terms = [{'handle': 'term_job'}]

    def test_dry_run_selects_without_side_effects(self):
        closed, retired = tidy.sweep(self.terms, set(), self.packet_dir, NOW, apply=False,
                                     truncated=False, run=refuse_run)
        self.assertEqual([(row['handle'], row['dry_run']) for row in closed], [('term_job', True)])
        self.assertEqual([(row['handle'], row['dry_run']) for row in retired], [('term_gone', True)])
        self.assertTrue(self.job.exists() and self.gone.exists())

    def test_apply_closes_jobs_and_retires_gone_records(self):
        calls = []

        def fake_run(argv, **kwargs):
            if argv[2] == 'close':          # ignore the pre-close terminal reset
                calls.append(argv[4])
            return {'ok': True, 'parsed': {'ok': True}}

        closed, retired = tidy.sweep(self.terms, set(), self.packet_dir, NOW, apply=True,
                                     truncated=False, run=fake_run)
        self.assertEqual(calls, ['term_job'])
        self.assertEqual([(row['handle'], row['ok']) for row in closed], [('term_job', True)])
        self.assertEqual(retired, [{'handle': 'term_gone', 'role': 'flash', 'kind': 'shell',
                                    'dispatch_id': None, 'reason': 'tab_gone', 'ok': True}])
        self.assertTrue(self.job.exists())
        self.assertFalse(self.gone.exists())

    def test_truncated_listing_retires_nothing(self):
        closed, retired = tidy.sweep(self.terms, set(), self.packet_dir, NOW, apply=True,
                                     truncated=True,
                                     run=lambda argv, **kwargs: {'ok': True, 'parsed': {}})
        self.assertEqual([row['handle'] for row in closed], ['term_job'])
        self.assertEqual(retired, [])
        self.assertTrue(self.gone.exists())


if __name__ == '__main__':
    unittest.main()
