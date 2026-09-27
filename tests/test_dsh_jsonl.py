#!/usr/bin/env python3
"""DSH adapter: mapping table, encoding, never complete from turn/end."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worker_state.dsh_jsonl import (  # noqa: E402
    collect_dsh_evidence,
    dsh_exit_event,
    dsh_session_dir,
    map_dsh_record,
    parse_exit_file,
)
from worker_state.resolver import resolve  # noqa: E402
from worker_state.types import Event, iso_from  # noqa: E402

NOW = 1_800_000_000.0


class MapTest(unittest.TestCase):
    def test_mapping_table(self) -> None:
        self.assertEqual(map_dsh_record({"type": "tool/call"})["kind"], "tool.started")
        self.assertEqual(
            map_dsh_record({"type": "tool/result", "data": {"isError": False}})["kind"],
            "tool.completed",
        )
        self.assertEqual(
            map_dsh_record({"type": "tool/result", "data": {"isError": True}})["kind"],
            "tool.failed",
        )
        self.assertEqual(map_dsh_record({"type": "turn/start"})["kind"], "model.request.started")
        self.assertEqual(map_dsh_record({"type": "request/header"})["kind"], "model.request.started")
        self.assertEqual(map_dsh_record({"type": "turn/end"})["kind"], "model.request.completed")
        self.assertIsNone(map_dsh_record({"type": "assistant/message"}))
        self.assertIsNone(map_dsh_record({"type": "session"}))

    def test_turn_end_does_not_complete_goal(self) -> None:
        events = [
            Event(
                journal_seq=1,
                event_id="a",
                worker_id="minecraft",
                kind="terminal_write.succeeded",
                source="api",
                authority=1,
                source_timestamp=iso_from(NOW),
                payload={"action": "dispatch_goal", "target": "flash"},
                goal_id="g1",
                dispatch_id="d1",
                attempt_id=1,
            ),
            Event(
                journal_seq=2,
                event_id="b",
                worker_id="minecraft",
                kind="model.request.started",
                source="dsh_jsonl",
                authority=2,
                source_timestamp=iso_from(NOW + 1),
                turn_id="s-1",
            ),
            Event(
                journal_seq=3,
                event_id="c",
                worker_id="minecraft",
                kind="model.request.completed",
                source="dsh_jsonl",
                authority=2,
                source_timestamp=iso_from(NOW + 2),
                turn_id="s-1",
            ),
        ]
        out = resolve(events, NOW + 3, "minecraft")
        self.assertEqual(out["goal"]["state"], "RUNNING")
        self.assertNotEqual(out["goal"]["state"], "COMPLETED")
        self.assertEqual(out["execution"]["target"], "flash")
        self.assertFalse(out["policy"]["continuity_recovery_allowed"])


class EncodingTest(unittest.TestCase):
    def test_harness_dir_uses_double_dashes(self) -> None:
        name = dsh_session_dir("/home/person/Projects/minecraft-server-project")
        self.assertTrue(name.startswith("--"))
        self.assertTrue(name.endswith("--"))
        qoder_style = "/home/person/Projects/minecraft-server-project".replace("/", "-")
        self.assertNotEqual(name, qoder_style)
        self.assertIn("home-person-Projects-minecraft-server-project", name)


class CollectTest(unittest.TestCase):
    def test_collects_jsonl_under_harness_dir_not_qoder_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cwd = "/home/person/Projects/minecraft-server-project"
            good = home / "sessions" / dsh_session_dir(cwd) / "sess-1"
            good.mkdir(parents=True)
            records = [
                {"type": "turn/start", "time": (NOW + 1) * 1000, "turn": 1},
                {"type": "turn/end", "time": (NOW + 2) * 1000, "turn": 1},
            ]
            (good / "session.v3.jsonl").write_text(
                "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
            )
            wrong = home / "sessions" / "-home-person-Projects-minecraft-server-project"
            wrong.mkdir(parents=True)
            (wrong / "session.v3.jsonl").write_text(
                json.dumps({"type": "tool/call", "time": (NOW + 9) * 1000}) + "\n",
                encoding="utf-8",
            )
            events = collect_dsh_evidence("minecraft", cwd, NOW + 10, dsh_home=home)
            kinds = [e["kind"] for e in events]
            self.assertEqual(kinds, ["model.request.started", "model.request.completed"])
            self.assertTrue(all(e["source"] == "dsh_jsonl" for e in events))


class ExitFileTest(unittest.TestCase):
    def test_parse_and_identity_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d1.exit"
            path.write_text("0\n", encoding="utf-8")
            self.assertEqual(parse_exit_file(path), 0)
            path.write_text("nope\n", encoding="utf-8")
            self.assertIsNone(parse_exit_file(path))
        snap = {
            "goal": {
                "goal_id": "g-m",
                "turn_id": "t1",
                "dispatch_id": "d1",
                "attempt_id": 2,
            }
        }
        ev = dsh_exit_event("minecraft", "d1", 0, snap, NOW)
        self.assertEqual(ev["event_id"], "dsh-exit-minecraft-d1")
        self.assertEqual(ev["kind"], "goal.completed")
        self.assertEqual(ev["source"], "dsh_exit")
        self.assertEqual(ev["goal_id"], "g-m")
        self.assertEqual(ev["turn_id"], "t1")
        self.assertEqual(ev["attempt_id"], 2)
        failed = dsh_exit_event("minecraft", "d1", 124, snap, NOW)
        self.assertEqual(failed["kind"], "goal.failed")


if __name__ == "__main__":
    unittest.main()
