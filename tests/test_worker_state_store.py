#!/usr/bin/env python3
"""SQLite store: ingest, replay, claims, completion once-only, reconcile."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worker_state.store import Store, StoreError
from worker_state.types import iso_from

NOW = 1_800_000_000.0


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_get_unseen_worker_is_unknown_without_rebuild(self):
        snap = self.store.snapshot("pugc", NOW, rebuild=False)
        self.assertEqual(snap["goal"]["state"], "UNKNOWN")
        self.assertFalse(snap["policy"]["can_dispatch_goal"])

    def test_get_does_not_begin_immediate(self):
        self.store.snapshot("pugc", NOW, rebuild=True)
        rebuilds: list[str] = []
        orig = self.store._rebuild_locked

        def wrapped(worker, now):
            rebuilds.append(worker)
            return orig(worker, now)

        self.store._rebuild_locked = wrapped  # type: ignore[method-assign]
        try:
            later = self.store.snapshot("pugc", NOW + 10, rebuild=False)
        finally:
            self.store._rebuild_locked = orig  # type: ignore[method-assign]
        self.assertEqual(rebuilds, [])
        self.assertEqual(later["goal"]["state"], "IDLE")

    def test_cached_get_preserves_uncertain_delivery_without_ingest(self):
        self.store.ingest(
            {
                "event_id": "inj",
                "worker": "pugc",
                "kind": "terminal_write.succeeded",
                "source": "api",
                "goal_id": "g-1",
                "dispatch_id": "d-1",
                "attempt_id": 1,
                "payload": {"action": "dispatch_goal", "target": "efficient"},
                "source_timestamp": iso_from(NOW),
            },
            NOW,
        )
        later = self.store.snapshot("pugc", NOW + 241, rebuild=False)
        self.assertEqual(later["goal"]["state"], "UNCONFIRMED")
        self.assertFalse(later["policy"]["can_dispatch_goal"])
        self.assertTrue(later["policy"]["idle_slo_violated"])

    def test_flash_stalled_blocks_continuity_resume(self):
        self.store.ingest(
            {
                "event_id": "inj",
                "worker": "minecraft",
                "kind": "terminal_write.succeeded",
                "source": "api",
                "goal_id": "g-m",
                "dispatch_id": "d-m",
                "attempt_id": 1,
                "payload": {"action": "dispatch_goal", "target": "flash"},
                "source_timestamp": iso_from(NOW - 10),
            },
            NOW - 10,
        )
        self.store.ingest(
            {
                "event_id": "run",
                "worker": "minecraft",
                "kind": "model.request.started",
                "source": "dsh_jsonl",
                "turn_id": "t1",
                "goal_id": "g-m",
                "source_timestamp": iso_from(NOW - 2000),
            },
            NOW - 2000,
        )
        snap = self.store.snapshot("minecraft", NOW, rebuild=True)
        self.assertEqual(snap["execution"]["target"], "flash")
        self.assertTrue(snap["execution"]["stalled"])
        self.assertFalse(snap["policy"]["continuity_recovery_allowed"])

    def test_claim_target_lives_on_claimed_event_not_result_json(self):
        self.store.snapshot("pugc", NOW, rebuild=True)
        claim = self.store.claim(
            "pugc",
            "dispatch_goal",
            0,
            "k-target",
            NOW,
            extra={"target": "flash"},
        )
        self.assertEqual(claim["state"], "claimed")
        row = [
            e
            for e in self.store.events_for("pugc")
            if e.kind == "action.claimed"
        ][0]
        self.assertEqual(row.payload.get("target"), "flash")
        self.assertIsNone(claim.get("result_json"))

    def test_advance_result_does_not_inject(self):
        self.store.ingest(
            {
                "event_id": "done",
                "worker": "qoder",
                "kind": "goal.completed",
                "source": "session_jsonl",
                "goal_id": "g-q",
                "turn_id": "t-q",
                "source_timestamp": iso_from(NOW),
            },
            NOW,
        )
        snap = self.store.snapshot("qoder", NOW, rebuild=True)
        self.assertTrue(snap["policy"]["grokbot_may_advance"])
        claim = self.store.claim(
            "qoder",
            "advance",
            snap["snapshot_version"],
            "k-adv",
            NOW + 1,
        )
        result = self.store.action_result(claim["action_id"], True, NOW + 2)
        states = [e.kind for e in self.store.events_for("qoder")]
        self.assertIn("advance.completed", states)
        self.assertNotIn("terminal_write.succeeded", states)
        after = result["snapshot"]
        # folded snapshot; apply_now_effects may still show COMPLETED
        self.assertNotEqual(after.get("goal", {}).get("state"), "INJECTED")

    def test_wal_pragmas(self):
        p = self.store.pragmas()
        self.assertEqual(p["journal_mode"], "WAL")
        self.assertEqual(p["foreign_keys"], "1")

    def test_duplicate_event_id_is_idempotent(self):
        event = {
            "event_id": "e1",
            "worker": "pugc",
            "kind": "model.request.started",
            "source": "session_jsonl",
            "turn_id": "t1",
            "source_timestamp": iso_from(NOW),
        }
        a = self.store.ingest(event, NOW)
        b = self.store.ingest(event, NOW + 10)
        self.assertEqual(a["goal"]["state"], "RUNNING")
        self.assertEqual(len(self.store.events_for("pugc")), 1)
        self.assertEqual(a["snapshot_version"], b["snapshot_version"])

    def test_replay_matches_live_snapshot(self):
        self.store.ingest(
            {
                "event_id": "e1",
                "worker": "pugc",
                "kind": "model.request.started",
                "source": "session_jsonl",
                "turn_id": "t1",
                "source_timestamp": iso_from(NOW),
            },
            NOW,
        )
        self.store.ingest(
            {
                "event_id": "e2",
                "worker": "pugc",
                "kind": "goal.parked",
                "source": "session_jsonl",
                "turn_id": "t1",
                "source_timestamp": iso_from(NOW + 1),
                "payload": {"park_reason": "goal_budget"},
            },
            NOW + 1,
        )
        live = self.store.snapshot("pugc", NOW + 2)
        replayed = self.store.replay("pugc", NOW + 2)
        live.pop("debug", None)
        replayed.pop("debug", None)
        self.assertEqual(live, replayed)

    def test_claim_concurrency_one_winner(self):
        snap = self.store.snapshot("pugc", NOW, rebuild=True)
        self.assertEqual(snap["snapshot_version"], 0)
        winners = []
        errors = []

        def attempt(key):
            try:
                winners.append(
                    self.store.claim("pugc", "dispatch_goal", 0, key, NOW)
                )
            except StoreError as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=attempt, args=(f"k{i}",))
            for i in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(errors), 7)
        self.assertTrue(all(err.http == 409 for err in errors))

    def test_claim_idempotency_key_reuses(self):
        self.store.snapshot("pugc", NOW, rebuild=True)
        a = self.store.claim("pugc", "dispatch_goal", 0, "same", NOW)
        b = self.store.claim("pugc", "dispatch_goal", 0, "same", NOW + 1)
        self.assertEqual(a["action_id"], b["action_id"])
        self.assertTrue(b["reused"])

    def test_version_mismatch_conflict(self):
        self.store.snapshot("pugc", NOW, rebuild=True)
        with self.assertRaises(StoreError) as ctx:
            self.store.claim("pugc", "dispatch_goal", 99, "k", NOW)
        self.assertEqual(ctx.exception.http, 409)

    def test_write_result_injects(self):
        self.store.snapshot("pugc", NOW, rebuild=True)
        claim = self.store.claim("pugc", "dispatch_goal", 0, "k", NOW)
        out = self.store.action_result(claim["action_id"], True, NOW + 1)
        self.assertEqual(out["state"], "succeeded")
        self.assertEqual(out["snapshot"]["goal"]["state"], "INJECTED")
        self.assertEqual(out["snapshot"]["goal"]["goal_id"], claim["goal_id"])

    def test_duplicate_result_is_idempotent(self):
        self.store.snapshot("pugc", NOW, rebuild=True)
        claim = self.store.claim("pugc", "dispatch_goal", 0, "k", NOW)
        self.store.action_result(claim["action_id"], True, NOW + 1)
        again = self.store.action_result(claim["action_id"], False, NOW + 2)
        self.assertEqual(again["state"], "succeeded")
        self.assertTrue(again["reused"])

    def test_reconcile_unknown_does_not_retry(self):
        self.store.snapshot("pugc", NOW, rebuild=True)
        claim = self.store.claim("pugc", "dispatch_goal", 0, "k", NOW)
        snapshot = self.store.reconcile("pugc", NOW + 31)
        # Missing claim target fail-closes the inject (never default-efficient).
        self.assertEqual(snapshot["goal"]["state"], "FAILED")
        self.assertTrue(snapshot["policy"]["can_dispatch_goal"])
        row = self.store._conn.execute(
            "SELECT state, delivery_state FROM action_claims WHERE action_id = ?",
            (claim["action_id"],),
        ).fetchone()
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["delivery_state"], "unknown")

    def test_completion_once_only(self):
        event = {
            "event_id": "c1",
            "worker": "pugc",
            "kind": "goal.completed",
            "source": "session_jsonl",
            "goal_id": "g-1",
            "turn_id": "t1",
            "attempt_id": 1,
            "payload": {"completion_identity": "t1"},
            "source_timestamp": iso_from(NOW),
        }
        a = self.store.ingest(event, NOW)
        event2 = dict(event)
        event2["event_id"] = "c2"
        b = self.store.ingest(event2, NOW + 1)
        self.assertEqual(a["goal"]["state"], "COMPLETED")
        self.assertEqual(b["goal"]["state"], "COMPLETED")
        n = self.store._conn.execute(
            "SELECT COUNT(*) AS n FROM completion_claims"
        ).fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertTrue(b["policy"]["grokbot_may_advance"])


if __name__ == "__main__":
    unittest.main()

class PublishedSnapshotTest(unittest.TestCase):
    setUp = StoreTest.setUp
    tearDown = StoreTest.tearDown

    def test_stale_resolver_cache_is_not_published_on_restart(self):
        self.store.snapshot('pugc', NOW)
        self.store._conn.execute("UPDATE worker_snapshots SET resolver_version = 'old'")
        self.store.close()
        self.store = Store(Path(self.tmp.name) / 'state.sqlite')
        self.assertEqual(self.store.snapshot('pugc', NOW, rebuild=False)['goal']['state'], 'UNKNOWN')
        self.assertEqual(self.store.replay('pugc', NOW)['goal']['state'], 'IDLE')

    def test_completion_advance_is_scoped_to_attempt(self):
        event = {'worker': 'minecraft', 'kind': 'goal.completed', 'source': 'qoder_jsonl',
                 'goal_id': 'g1', 'turn_id': 't1', 'attempt_id': 1, 'payload': {}}
        snapshot = self.store.ingest({**event, 'event_id': 'first'}, NOW)
        claim = self.store.claim('minecraft', 'advance', snapshot['snapshot_version'], 'first', NOW)
        self.store.action_result(claim['action_id'], True, NOW)
        self.store.ingest({**event, 'event_id': 'resume', 'kind': 'input.resume', 'source': 'api',
                           'turn_id': 't2', 'attempt_id': 2}, NOW + 1)
        next_ = self.store.ingest({**event, 'event_id': 'second', 'turn_id': 't2', 'attempt_id': 2}, NOW + 1)
        self.assertTrue(next_['policy']['grokbot_may_advance'])

    def test_get_does_not_wait_for_writer_lock_or_read_sqlite(self):
        self.store.snapshot('pugc', NOW)
        done = threading.Event()
        with self.store._lock:
            thread = threading.Thread(target=lambda: (
                self.store.snapshot('pugc', NOW + 1, rebuild=False), done.set()))
            thread.start()
            self.assertTrue(done.wait(0.5), 'GET waits for the SQLite writer lock')
        thread.join()

    def test_assignment_requires_current_advance_and_failure_reopens(self):
        self.store.ingest({'event_id': 'complete', 'worker': 'minecraft',
            'kind': 'goal.completed', 'source': 'qoder_jsonl', 'goal_id': 'g1',
            'attempt_id': 1, 'payload': {}}, NOW)
        snap = self.store.snapshot('minecraft', NOW)
        claim = self.store.claim('minecraft', 'advance', snap['snapshot_version'], 'advance:g1', NOW)
        self.store.action_result(claim['action_id'], True, NOW)
        base = {'worker': 'minecraft', 'source': 'api', 'goal_id': 'g1', 'attempt_id': 1,
                'payload': {'action_id': claim['action_id'], 'request_id': 'r1'}}
        assigning = self.store.ingest({**base, 'event_id': 'start', 'kind': 'assignment.started'}, NOW)
        self.assertEqual(assigning['goal']['state'], 'ASSIGNING')
        self.assertEqual(assigning['goal']['assignment_id'], 'r1')
        failed = self.store.ingest({**base, 'event_id': 'fail', 'kind': 'assignment.failed'}, NOW + 301)
        self.assertTrue(failed['policy']['grokbot_may_advance'])
        with self.assertRaises(StoreError):
            self.store.ingest({**base, 'event_id': 'late', 'kind': 'assignment.finished'}, NOW + 302)
