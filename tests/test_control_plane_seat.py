"""Seat handoff and free-first quota routing."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_plane import quota, runtime, seat
from control_plane.io import read_json, write_json


class SeatTest(unittest.TestCase):
    def test_prepare_handoff_writes_brief_and_caps_daily(self):
        now = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = seat.default_state('minecraft')
            first = seat.prepare_handoff(state, root, now, goal='Ship Nether',
                                         decisions=['keep flash for implement'],
                                         outcomes=['packet p1 done'])
            self.assertTrue(first['ok'])
            self.assertEqual(first['seat']['owner'], 'kimi')
            self.assertEqual(first['seat']['handoff_count'], 1)
            brief = Path(first['brief']['path'])
            self.assertTrue(brief.is_file())
            self.assertIn('Ship Nether', brief.read_text())
            second = seat.prepare_handoff(first['seat'], root, now + 10)
            self.assertFalse(second['ok'])
            self.assertEqual(second['error'], 'already_owner')
            back = seat.prepare_handoff(first['seat'], root, now + 20, to='codex')
            self.assertTrue(back['ok'])
            self.assertEqual(back['seat']['handoff_count'], 2)
            third = seat.prepare_handoff(back['seat'], root, now + 30, to='kimi')
            self.assertFalse(third['ok'])
            self.assertEqual(third['error'], 'handoff_day_cap')
            next_day = seat.prepare_handoff(back['seat'], root, now + 86_400, to='kimi')
            self.assertTrue(next_day['ok'])
            self.assertEqual(next_day['seat']['handoff_count'], 1)

    def test_commit_and_cold_start_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = seat.seat_path(root)
            state = seat.default_state()
            out = seat.commit(path, {**state, 'owner': 'kimi'})
            self.assertTrue(out['ok'])
            self.assertEqual(read_json(path)['owner'], 'kimi')
            prompt = seat.cold_start_prompt(read_json(path), root)
            self.assertIn('brief.md', prompt)
            self.assertIn('standby seat', prompt)


class QuotaTest(unittest.TestCase):
    def test_free_first_only_for_safe_kinds(self):
        free_ok = {'ok': True, 'exhausted': False}
        free_out = {'ok': False, 'exhausted': True}
        implement = {'id': 'p1', 'assignee': 'flash', 'kind': 'implement'}
        review = {'id': 'p2', 'assignee': 'mimo', 'kind': 'review'}
        mechanical = {'id': 'p3', 'assignee': 'efficient', 'kind': 'mechanical'}
        gated = {'id': 'p4', 'assignee': 'flash', 'kind': 'implement', 'requires_astra_review': True}

        self.assertEqual(quota.pick_assignee(implement, free_ok)['tier'], 'free')
        self.assertEqual(quota.pick_assignee(implement, free_out)['tier'], 'paid')
        self.assertEqual(quota.pick_assignee(review, free_ok)['tier'], 'paid')
        self.assertEqual(quota.pick_assignee(mechanical, free_ok)['tier'], 'free')
        self.assertEqual(quota.pick_assignee(gated, free_ok)['tier'], 'paid')
        routed = quota.route_queue([implement, review], free_ok)
        self.assertEqual(routed[0]['tier'], 'free')
        self.assertEqual(routed[1]['tier'], 'paid')


class WorkerClassTest(unittest.TestCase):
    def test_class_gate(self):
        runtime.validate_workers({'minecraft': {
            'class': 'toplevel', 'cwd': '/work/mc',
            'planner': {'terminal': 'term_a'}}})
        runtime.validate_workers({'qoder': {'class': 'side', 'cwd': '/work/q'}})
        with self.assertRaises(ValueError):
            runtime.validate_workers({'qoder': {'class': 'nope', 'cwd': '/work/q'}})
        with self.assertRaises(ValueError):
            runtime.validate_workers({'other': {
                'class': 'side', 'cwd': '/work/x', 'planner': {'terminal': 't'}}})
        with self.assertRaises(ValueError):
            runtime.validate_workers({'side': {
                'class': 'side', 'cwd': '/work/s', 'seat': {'owner': 'kimi'}}})


if __name__ == '__main__':
    unittest.main()
