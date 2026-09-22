"""Orca packet-tab tidy: close finished jobs and orphan packet shells."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_plane import tidy


class SelectTest(unittest.TestCase):
    def test_job_tab_needs_exit_and_grace(self):
        now = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            packet_dir = Path(tmp)
            (packet_dir / 'd-1.exit').write_text('0\n')
            (packet_dir / 'd-1.exit').touch()
            # make mtime old enough
            old = now - 400
            import os
            os.utime(packet_dir / 'd-1.exit', (old, old))
            terms = [
                {'handle': 'term_job', 'title': 'flash d-1'},
                {'handle': 'term_running', 'title': 'mimo d-2'},
                {'handle': 'term_shell', 'title': 'flash-packets'},
                {'handle': 'term_pin', 'title': 'efficient worker'},
                {'handle': 'term_pinned_job', 'title': 'flash d-1'},
            ]
            picked = tidy.select(terms, {'term_pinned_job'}, packet_dir, now)
            handles = [row['handle'] for row in picked]
            self.assertIn('term_job', handles)
            self.assertIn('term_shell', handles)
            self.assertNotIn('term_running', handles)
            self.assertNotIn('term_pin', handles)
            self.assertNotIn('term_pinned_job', handles)
            self.assertEqual([row['reason'] for row in picked if row['handle'] == 'term_job'],
                             ['job_exit:d-1:400'])

    def test_fresh_exit_is_kept(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            packet_dir = Path(tmp)
            (packet_dir / 'd-9.exit').write_text('0\n')
            picked = tidy.select([{'handle': 'term', 'title': 'mimo d-9'}], set(), packet_dir, now)
            self.assertEqual(picked, [])

    def test_unknown_titles_are_never_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            terms = [{'handle': 'a', 'title': 'person@spectre: ~/x'},
                     {'handle': 'b', 'title': None},
                     {'handle': 'c', 'title': '◇ Gemini CLI'}]
            self.assertEqual(tidy.select(terms, set(), Path(tmp), time.time()), [])

    def test_parse_title(self):
        self.assertEqual(tidy.parse_job_title('flash d-abc_1'), ('flash', 'd-abc_1'))
        self.assertIsNone(tidy.parse_job_title('flash-packets'))
        self.assertIsNone(tidy.parse_job_title('flash '))
        self.assertTrue(tidy.is_shell_title('mimo-packets'))
        self.assertFalse(tidy.is_shell_title('mimo-packets x'))


class CloseTest(unittest.TestCase):
    def test_close_invokes_orca_tab_close(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return {'ok': True, 'parsed': {'ok': True}}

        result = tidy.close('term_x', run=fake_run)
        self.assertTrue(result['ok'])
        self.assertEqual(calls[0][:3], ['orca-ide', 'terminal', 'close'])
        self.assertIn('--tab', calls[0])
        self.assertIn('term_x', calls[0])


if __name__ == '__main__':
    unittest.main()
