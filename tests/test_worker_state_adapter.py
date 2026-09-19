#!/usr/bin/env python3
"""qoder jsonl adapter + shadow comparison."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worker_state.qoder_jsonl import (
    collect_worker_evidence,
    event_id_for,
    map_record,
    session_dir_name,
)
from worker_state.resolver import resolve
from worker_state.shadow import compare, occupancy_from_snapshot
from worker_state.store import Store
from worker_state.types import Event, authority_for, iso_from

NOW = 1_800_000_000.0


def ts_at(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def record(rtype, ts, turn_id="t1", **data):
    out = {"type": rtype, "ts": ts, "turn_id": turn_id}
    if data:
        out["data"] = data
    return out


class MapRecordTest(unittest.TestCase):
    def test_max_turns(self):
        mapped = map_record(record("turn.finished", ts_at(NOW), reason="max_turns", num_turns=1000))
        self.assertEqual(mapped["kind"], "goal.parked")
        self.assertEqual(mapped["payload"]["park_reason"], "goal_budget")

    def test_end_turn(self):
        mapped = map_record(record("turn.finished", ts_at(NOW), reason="end_turn"))
        self.assertEqual(mapped["kind"], "turn.ended")

    def test_exit_plan_mode(self):
        mapped = map_record(
            record("hook.finished", ts_at(NOW), hook_name="PreToolUse:ExitPlanMode")
        )
        self.assertEqual(mapped["kind"], "goal.parked")
        self.assertEqual(mapped["payload"]["park_reason"], "plan_gate")

    def test_goal_resume_prompt(self):
        mapped = map_record(
            record("input.prompt.received", ts_at(NOW), prompt="/goal resume")
        )
        self.assertEqual(mapped["kind"], "input.resume")

    def test_event_id_stable(self):
        rec = record("model.request.started", ts_at(NOW))
        path = Path("/tmp/seg.jsonl")
        self.assertEqual(event_id_for(path, rec), event_id_for(path, rec))

    def test_session_phase_finished_is_not_a_turn_boundary(self):
        # phase=input.attachments.collect happens inside a live /goal loop.
        mapped = map_record(
            record("session.phase.finished", ts_at(NOW), phase="input.attachments.collect")
        )
        self.assertIsNone(mapped)


class LoopIterationTest(unittest.TestCase):
    """A live /goal loop must never read IDLE between iterations.

    Regression for the 2026-09-19 shadow rows: zzbrush's raw stream is
    loop.iteration.started -> model.request.started -> tool.requested ->
    hook.finished -> session.phase.finished (input.attachments.collect) ->
    loop.iteration.finished -> ... The phase record was mapped to turn.ended,
    so the resolver reported IDLE with can_dispatch_goal=true while the loop
    was still running.
    """

    def _snapshot_from(self, records):
        store = Store(Path(tempfile.mkdtemp()) / "db.sqlite")
        try:
            snap = None
            for rec in records:
                mapped = map_record(rec)
                if mapped is None:
                    continue
                item = {"event_id": f"e{len(records)}-{rec['type']}-{rec['ts']}", "worker": "zzbrush", **mapped}
                snap = store.ingest(item, NOW)
            return snap
        finally:
            store.close()

    def test_phase_boundary_does_not_idle_a_running_loop(self):
        records = [
            record("loop.iteration.started", ts_at(NOW - 30), request_index=7),
            record("model.request.started", ts_at(NOW - 29), request_index=7),
            record("tool.requested", ts_at(NOW - 20), tool_name="Bash"),
            record("hook.finished", ts_at(NOW - 18), hook_name="PreToolUse:Bash"),
            record("session.phase.finished", ts_at(NOW - 6), phase="input.attachments.collect"),
        ]
        snap = self._snapshot_from(records)
        self.assertEqual(snap["goal"]["state"], "RUNNING")
        self.assertFalse(snap["policy"]["can_dispatch_goal"])

    def test_stale_hook_alone_leaves_an_idle_worker_idle(self):
        records = [
            record("hook.finished", ts_at(NOW - 13 * 3600), hook_name="PreToolUse:Bash"),
        ]
        snap = self._snapshot_from(records)
        self.assertEqual(snap["goal"]["state"], "IDLE")
        self.assertTrue(snap["policy"]["can_dispatch_goal"])


class IngestAdapterTest(unittest.TestCase):
    def test_collect_and_resolve_stale_model_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = "/home/person/Projects/orca-rust"
            seg = root / session_dir_name(cwd) / "sess" / "segments"
            seg.mkdir(parents=True)
            rec = record("model.request.started", ts_at(NOW - 601))
            path = seg / "a.jsonl"
            path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            items = collect_worker_evidence("qoder", {"cwd": cwd}, root)
            self.assertEqual(len(items), 1)
            store = Store(root / "db.sqlite")
            try:
                snap = store.ingest(items[0], NOW)
            finally:
                store.close()
            self.assertEqual(snap["goal"]["state"], "RUNNING")
            self.assertEqual(occupancy_from_snapshot(snap), "active")


class ShadowTest(unittest.TestCase):
    def test_old_false_idle_is_flagged(self):
        events = [
            Event(
                journal_seq=1,
                event_id="e1",
                worker_id="qoder",
                kind="model.request.started",
                source="session_jsonl",
                authority=authority_for("session_jsonl", "model.request.started"),
                source_timestamp=iso_from(NOW - 601),
                turn_id="t1",
                payload={},
            )
        ]
        snapshot = resolve(events, NOW, "qoder")
        old = {"state": "idle", "reason": "model.request.started", "age_sec": 601}
        result = compare(old, snapshot)
        self.assertIn("old_false_idle", result["kinds"])
        self.assertFalse(result["dangerous"])

    def test_dangerous_false_idle(self):
        snapshot = resolve([], NOW, "qoder")
        old = {"state": "active", "reason": "model.request.started"}
        result = compare(old, snapshot)
        self.assertIn("dangerous_false_idle", result["kinds"])
        self.assertTrue(result["dangerous"])

    def test_plan_gate_matches(self):
        events = [
            Event(
                journal_seq=1,
                event_id="e1",
                worker_id="qoder",
                kind="goal.parked",
                source="session_jsonl",
                authority=1,
                source_timestamp=iso_from(NOW),
                turn_id="t1",
                payload={"park_reason": "plan_gate"},
            )
        ]
        snapshot = resolve(events, NOW, "qoder")
        old = {"state": "parked", "reason": "plan_gate"}
        result = compare(old, snapshot)
        self.assertNotIn("park_reason_mismatch", result["kinds"])
        self.assertFalse(snapshot["policy"]["can_resume"])


if __name__ == "__main__":
    unittest.main()
