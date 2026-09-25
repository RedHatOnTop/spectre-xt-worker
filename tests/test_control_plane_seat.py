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
    def test_missing_seat_defaults_to_requested_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = seat.seat_path(Path(tmp), 'minecraft')
            self.assertEqual(seat.load(path)['worker'], 'minecraft')

    def test_corrupt_seat_cannot_reset_ownership_to_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = seat.seat_path(Path(tmp))
            path.parent.mkdir(parents=True)
            for contents in ('{', '{}'):
                path.write_text(contents)
                with self.assertRaises(ValueError):
                    seat.load(path)
            path.unlink()
            path.symlink_to('missing-seat.json')
            with self.assertRaises(ValueError):
                seat.load(path)
            path.unlink()
            write_json(path, {**seat.default_state(), 'history': 'not-a-list'})
            with self.assertRaises(ValueError):
                seat.load(path)

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


class SeatLadderTest(unittest.TestCase):
    def test_shipped_ladder_is_valid_and_the_order_is_locked(self):
        rows = seat.ladder()
        seat.validate_ladder(rows)
        self.assertEqual(
            [(r['rank'], r['harness'], r['provider'], r['model'], r['wire_api'])
             for r in rows],
            [(1, 'claude', 'anyrouter', 'claude-opus-5-5', 'messages'),
             (2, 'codex', 'agentrouter', 'gpt-6-astra', 'responses'),
             (3, 'kimi', 'cline-free', 'cline-free/kimi-k3', 'chat'),
             (4, 'codex', 'openai', 'gpt-6-astra', 'responses')])

    def test_ladder_rejects_malformed_policies(self):
        good = seat.ladder()
        broken = [
            [],                                                  # empty
            'not-a-list',                                        # wrong type
            [good[0], 'not-an-object'],                          # bad entry
            [{**good[0], 'rank': 2}],                            # rank must be 1
            [{**good[0], 'rank': True}],                         # bool is not a rank
            [{**good[0], 'harness': 'nobody'}],                  # unknown harness
            [{**good[0], 'wire_api': 'grpc'}],                   # unknown surface
            [{**good[0], 'wire_api': None}],                     # missing surface
            [{**good[0], 'model': ''}],                          # empty model
            [{**good[0], 'provider': '  '}],                     # blank provider
            [{**good[0], 'harness': 1}],                         # non-string harness
        ]
        for candidate in broken:
            with self.assertRaises(ValueError):
                seat.validate_ladder(candidate)

    def test_owner_lookup_prefers_the_best_rank(self):
        self.assertEqual(seat.owner_rank('claude'), 1)
        self.assertEqual(seat.owner_rank('codex'), 2)
        self.assertEqual(seat.owner_rank('kimi'), 3)
        self.assertEqual([s['rank'] for s in seat.seats_for_owner('codex')], [2, 4])
        self.assertEqual(seat.seats_for_owner('claude')[0]['model'], 'claude-opus-5-5')
        self.assertEqual(seat.seat_for_rank(3)['provider'], 'cline-free')
        with self.assertRaises(ValueError):
            seat.owner_rank('nobody')
        with self.assertRaises(ValueError):
            seat.seat_for_rank(9)

    def test_ladder_accessor_returns_copies(self):
        row = seat.ladder()[0]
        row['model'] = 'mutated'
        self.assertEqual(seat.SEAT_LADDER[0]['model'], 'claude-opus-5-5')

    def test_every_owner_has_a_seat(self):
        for owner in seat.OWNERS:
            self.assertTrue(seat.seats_for_owner(owner), owner)

    def test_every_owner_has_a_planner_role(self):
        self.assertEqual(set(seat.PLANNER_ROLES), set(seat.OWNERS))
        self.assertEqual([seat.planner_role(o) for o in ('codex', 'claude', 'kimi', 'gemini')],
                         ['astra', 'claude', 'kimi', None])

    def test_transition_takes_the_model_from_the_owners_best_seat(self):
        now = 1_800_000_000.0
        state = seat.default_state()
        claude = seat.transition(state, now, to='claude', brief='brief.md')
        self.assertTrue(claude['ok'])
        self.assertEqual((claude['seat']['model'], claude['seat']['provider_chain']),
                         ('claude-opus-5-5', ['anyrouter']))
        kimi = seat.transition(state, now, to='kimi', brief='brief.md')
        self.assertEqual((kimi['seat']['model'], kimi['seat']['provider_chain']),
                         ('cline-free/kimi-k3', ['cline-free']))
        back = seat.transition(claude['seat'], now + 1, to='codex', brief='brief.md')
        self.assertEqual((back['seat']['model'], back['seat']['provider_chain']),
                         ('gpt-6-astra', ['agentrouter', 'anyrouter']))
        self.assertEqual(seat.transition(state, now, to='gemini', brief='brief.md'),
                         {'ok': False, 'error': 'unknown_owner'})
        with tempfile.TemporaryDirectory() as tmp:
            path = seat.seat_path(Path(tmp))
            path.parent.mkdir(parents=True)
            write_json(path, claude['seat'])
            self.assertEqual(seat.load(path)['owner'], 'claude')
            write_json(path, {**claude['seat'], 'owner': 'gemini'})
            with self.assertRaises(ValueError):
                seat.load(path)


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
        self.assertEqual(quota.pick_assignee(mechanical, free_ok)['assignee'], 'flash')
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
