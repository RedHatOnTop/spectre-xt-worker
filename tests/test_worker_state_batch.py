"""Poller evidence is persisted once and rebuilt once per worker batch."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worker_state.batch import ingest_batch
from worker_state.store import Store, StoreError
from worker_state.types import iso_from

NOW = 1_800_000_000.0


def event(ident: str, kind: str, *, worker: str = "minecraft") -> dict:
    return {"event_id": ident, "worker": worker, "kind": kind,
            "source": "session_jsonl", "source_timestamp": iso_from(NOW),
            "turn_id": "t1", "payload": {}}


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        self.addCleanup(self.store.close)

    def test_two_new_events_rebuild_once(self):
        calls = []
        original = self.store._rebuild_locked

        def tracked(worker, now):
            calls.append(worker)
            return original(worker, now)

        self.store._rebuild_locked = tracked
        result = ingest_batch(self.store, "minecraft", [
            event("start", "model.request.started"),
            event("end", "turn.ended")], NOW)
        self.assertEqual(result["added"], 2)
        self.assertEqual(calls, ["minecraft"])
        self.assertEqual(self.store.snapshot("minecraft", NOW, rebuild=False)["goal"]["state"], "IDLE")

    def test_duplicate_batch_does_not_rebuild(self):
        rows = [event("start", "model.request.started")]
        ingest_batch(self.store, "minecraft", rows, NOW)
        self.store._rebuild_locked = lambda worker, now: self.fail("duplicate replay rebuilt")
        result = ingest_batch(self.store, "minecraft", rows, NOW + 1)
        self.assertEqual(result["added"], 0)

    def test_mixed_worker_rejected_without_partial_write(self):
        with self.assertRaises(StoreError):
            ingest_batch(self.store, "minecraft", [event("one", "turn.ended"),
                event("two", "turn.ended", worker="qoder")], NOW)
        self.assertEqual(self.store.events_for("minecraft"), [])

    def test_assignment_event_is_not_allowed_in_poller_batch(self):
        with self.assertRaises(StoreError):
            ingest_batch(self.store, "minecraft", [event("assignment", "assignment.started")], NOW)


if __name__ == "__main__":
    unittest.main()
