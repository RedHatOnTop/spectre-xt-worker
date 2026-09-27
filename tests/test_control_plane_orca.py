"""Orca terminals bind to a registered worktree or are not created."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_plane import orca
from control_plane.io import run as run_command

CWD = '/work/mc'
WORKTREE_ID = f'repo-1::{CWD}'


class FakeOrca:
    def __init__(self, rows=None, truncated=False, ps_ok=True, bound_to=WORKTREE_ID,
                 create=None, total=None, whole=None):
        self.rows = ([{'worktreeId': WORKTREE_ID, 'path': CWD, 'isArchived': False}]
                     if rows is None else rows)
        self.truncated = truncated
        self.total = total
        self.whole = whole
        self.ps_ok = ps_ok
        self.bound_to = bound_to
        self.create = create
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get('cwd')))
        if argv[1:3] == ['worktree', 'ps']:
            if not self.ps_ok:
                return {'ok': False, 'parsed': {}}
            if '--limit' in argv:
                return {'ok': True, 'parsed': {'result': {'worktrees': self.whole, 'truncated': False,
                                                          'totalCount': self.total}}}
            return {'ok': True, 'parsed': {'result': {'worktrees': self.rows,
                                                      'truncated': self.truncated,
                                                      'totalCount': self.total}}}
        if argv[1:3] == ['terminal', 'create']:
            if self.create is not None:
                return self.create
            return {'ok': True, 'parsed': {'result': {'terminal': {
                'handle': 'term_new', 'worktreeId': self.bound_to, 'surface': 'background'}}}}
        if argv[1:3] == ['terminal', 'send']:
            # pre-close terminal reset (mouse/alt-screen modes off); best effort
            return {'ok': True, 'parsed': {'ok': True}}
        if argv[1:3] == ['terminal', 'close']:
            return {'ok': True, 'parsed': {'ok': True}}
        raise AssertionError(argv)

    def actions(self):
        return [argv[2] for argv, _ in self.calls]


class CreateTest(unittest.TestCase):
    def test_registered_worktree_creates_with_active_selector_from_inside(self):
        fake = FakeOrca()
        out = orca.create(CWD, 'astra-planner', 'exec codex', fake)
        self.assertEqual(out, {'ok': True, 'handle': 'term_new', 'worktree_id': WORKTREE_ID,
                               'surface': 'background'})
        argv, cwd = fake.calls[-1]
        self.assertEqual(argv, ['orca-ide', 'terminal', 'create', '--worktree', 'active',
                                '--title', 'astra-planner', '--command', 'exec codex', '--json'])
        self.assertEqual(cwd, CWD)
        self.assertFalse(any('path:' in part for argv, _ in fake.calls for part in argv))

    def test_unregistered_or_archived_worktree_is_refused_before_create(self):
        for rows in ([], [{'worktreeId': 'repo-1::/work/other', 'path': '/work/other'}],
                     [{'worktreeId': WORKTREE_ID, 'path': CWD, 'isArchived': True}]):
            fake = FakeOrca(rows=rows)
            self.assertEqual(orca.create(CWD, 't', 'c', fake)['error'],
                             'orca_worktree_unregistered')
            self.assertEqual(fake.actions(), ['ps'])

    def test_duplicate_registration_is_refused(self):
        row = {'worktreeId': WORKTREE_ID, 'path': CWD}
        fake = FakeOrca(rows=[row, {**row, 'worktreeId': f'repo-2::{CWD}'}])
        out = orca.create(CWD, 't', 'c', fake)
        self.assertEqual((out['error'], out['rows']), ('orca_worktree_unregistered', 2))
        self.assertEqual(fake.actions(), ['ps'])

    def test_truncated_listing_is_not_proof_of_absence(self):
        fake = FakeOrca(rows=[], truncated=True)
        self.assertEqual(orca.create(CWD, 't', 'c', fake)['error'], 'orca_ps_truncated')
        self.assertEqual(fake.actions(), ['ps'])

    def test_a_worktree_past_the_first_page_is_found_by_one_whole_read(self):
        other = {'worktreeId': 'repo-1::/work/other', 'path': '/work/other'}
        mine = {'worktreeId': WORKTREE_ID, 'path': CWD, 'isArchived': False}
        fake = FakeOrca(rows=[other], truncated=True, total=2, whole=[other, mine])
        self.assertEqual(orca.create(CWD, 't', 'c', fake)['worktree_id'], WORKTREE_ID)
        self.assertEqual([argv[3:] for argv, _ in fake.calls if argv[2] == 'ps'],
                         [['--json'], ['--json', '--limit', '2']])
        self.assertEqual(fake.actions(), ['ps', 'ps', 'create'])

    def test_a_whole_read_without_the_worktree_is_unregistered(self):
        other = {'worktreeId': 'repo-1::/work/other', 'path': '/work/other'}
        fake = FakeOrca(rows=[other], truncated=True, total=2,
                        whole=[other, {'worktreeId': 'repo-1::/work/x', 'path': '/work/x'}])
        self.assertEqual(orca.create(CWD, 't', 'c', fake)['error'], 'orca_worktree_unregistered')
        self.assertEqual(fake.actions(), ['ps', 'ps'])

    def test_an_implausible_total_is_not_read_whole(self):
        mine = {'worktreeId': WORKTREE_ID, 'path': CWD, 'isArchived': False}
        fake = FakeOrca(rows=[], truncated=True, total=orca.MAX_WORKTREES + 1, whole=[mine])
        self.assertEqual(orca.create(CWD, 't', 'c', fake)['error'], 'orca_ps_truncated')
        self.assertEqual(fake.actions(), ['ps'])

    def test_listing_failure_or_unknown_shape_creates_nothing(self):
        fake = FakeOrca(ps_ok=False)
        self.assertEqual(orca.create(CWD, 't', 'c', fake)['error'], 'orca_ps_failed')
        self.assertEqual(fake.actions(), ['ps'])
        calls = []

        def terminals_shape(argv, **_kwargs):
            calls.append(argv)
            return {'ok': True, 'parsed': {'result': {'terminals': []}}}

        self.assertEqual(orca.create(CWD, 't', 'c', terminals_shape)['error'], 'orca_ps_invalid')
        self.assertEqual(len(calls), 1)

    def test_terminal_bound_to_another_worktree_is_closed(self):
        fake = FakeOrca(bound_to=f'repo-9::{CWD}')
        out = orca.create(CWD, 't', 'c', fake)
        self.assertEqual(out, {'ok': False, 'error': 'orca_terminal_invisible',
                               'handle': 'term_new', 'closed': True,
                               'worktree_id': f'repo-9::{CWD}', 'expected': WORKTREE_ID})
        self.assertEqual(fake.calls[-1][0], ['orca-ide', 'terminal', 'close', '--terminal',
                                             'term_new', '--tab', '--json'])

    def test_uncertain_or_handleless_create_asks_for_inspection(self):
        fake = FakeOrca(create={'ok': False, 'uncertain': True})
        self.assertIn('inspect terminal list', orca.create(CWD, 't', 'c', fake)['error'])
        fake = FakeOrca(create={'ok': False})
        self.assertEqual(orca.create(CWD, 't', 'c', fake)['error'], 'orca_create_failed')
        fake = FakeOrca(create={'ok': True, 'parsed': {'result': {'terminal': {}}}})
        self.assertIn('inspect terminal list', orca.create(CWD, 't', 'c', fake)['error'])
        self.assertNotIn('close', fake.actions())


class RunCwdTest(unittest.TestCase):
    def test_run_executes_in_the_given_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = run_command([sys.executable, '-c',
                               'import json, os; print(json.dumps({"cwd": os.getcwd()}))'],
                              cwd=tmp)
            self.assertTrue(out['ok'])
            self.assertEqual(out['parsed']['cwd'], str(Path(tmp).resolve()))


if __name__ == '__main__':
    unittest.main()
