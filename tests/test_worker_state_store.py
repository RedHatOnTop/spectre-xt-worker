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
        self.assertIn(snapshot["goal"]["state"], {"INJECTED", "UNCONFIRMED"})
        self.assertFalse(snapshot["policy"]["can_dispatch_goal"])
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
