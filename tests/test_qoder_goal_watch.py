#!/usr/bin/env python3
"""Unit tests for the qoder goal-park watcher (classification, dedup, CLI)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "qoder-goal-watch.py"

from worker_state.legacy import ACTIVE_FRESH_SEC, position as legacy_position
from worker_state.qoder_jsonl import map_record
from worker_state.store import Store

spec = importlib.util.spec_from_file_location("qoder_goal_watch", SCRIPT)
assert spec and spec.loader
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
CWD = "/home/person/Projects/orca-rust"


def ts_at(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def record(rtype: str, ts: str, turn_id: str = "t1", **data: object) -> dict:
    out: dict = {"type": rtype, "ts": ts, "turn_id": turn_id}
    if data:
        out["data"] = data
    return out


def ingest_into(store: Store, worker: str, records: list[dict], now: float) -> None:
    for i, rec in enumerate(records):
        mapped = map_record(rec)
        if mapped is None:
            continue
        store.ingest({"event_id": f"{worker}-{i}", "worker": worker, **mapped}, now)


def write_segment(
    root: Path,
    records: list[dict],
    cwd: str = CWD,
    session: str = "sess-1",
    name: str = "seg.jsonl",
) -> Path:
    segments = root / watch.session_dir_name(cwd) / session / "segments"
    segments.mkdir(parents=True, exist_ok=True)
    path = segments / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


class SessionPathTest(unittest.TestCase):
    def test_slashes_become_dashes(self) -> None:
        self.assertEqual(watch.session_dir_name("/home/person/Projects/orca-rust"), "-home-person-Projects-orca-rust")

    def test_trailing_slash_is_stripped(self) -> None:
        self.assertEqual(watch.session_dir_name("/work/korea-metro-twin/"), "-work-korea-metro-twin")


class PositionTest(unittest.TestCase):
    def test_no_records_is_unknown(self) -> None:
        self.assertEqual(legacy_position([], NOW), {"state": "unknown"})

    def test_turn_finished_end_turn_is_idle(self) -> None:
        pos = legacy_position([record("turn.finished", ts_at(NOW - 5), reason="end_turn")], NOW)
        self.assertEqual(pos["state"], "idle")
        self.assertEqual(pos["reason"], "end_turn")
        self.assertAlmostEqual(pos["age_sec"], 5)

    def test_turn_finished_max_turns_is_parked_goal_budget(self) -> None:
        pos = legacy_position(
            [record("turn.finished", ts_at(NOW - 60), reason="max_turns", num_turns=1000)], NOW
        )
        self.assertEqual(pos["state"], "parked")
        self.assertEqual(pos["reason"], "goal_budget")
        self.assertEqual(pos["num_turns"], 1000)
        self.assertEqual(pos["turn_id"], "t1")

    def test_exit_plan_mode_hook_is_parked_plan_gate(self) -> None:
        pos = legacy_position(
            [record("hook.finished", ts_at(NOW - 30), hook_name="PreToolUse:ExitPlanMode")], NOW
        )
        self.assertEqual(pos["state"], "parked")
        self.assertEqual(pos["reason"], "plan_gate")

    def test_other_hooks_follow_freshness(self) -> None:
        fresh = legacy_position(
            [record("hook.finished", ts_at(NOW - 100), hook_name="PreToolUse:Bash")], NOW
        )
        self.assertEqual(fresh["state"], "active")
        stale = legacy_position(
            [record("hook.finished", ts_at(NOW - 601), hook_name="PreToolUse:Bash")], NOW
        )
        self.assertEqual(stale["state"], "idle")

    def test_freshness_boundary(self) -> None:
        at_limit = legacy_position(
            [record("model.request.started", ts_at(NOW - ACTIVE_FRESH_SEC))], NOW
        )
        self.assertEqual(at_limit["state"], "active")
        past_limit = legacy_position(
            [record("model.request.started", ts_at(NOW - ACTIVE_FRESH_SEC - 1))], NOW
        )
        self.assertEqual(past_limit["state"], "idle")

    def test_unparseable_ts_has_no_age(self) -> None:
        pos = legacy_position([record("model.request.started", "not-a-timestamp")], NOW)
        self.assertIsNone(pos["age_sec"])
        self.assertEqual(pos["state"], "idle")


class ParkKeyTest(unittest.TestCase):
    def test_parked_key_uses_turn_id(self) -> None:
        pos = {"state": "parked", "reason": "goal_budget", "turn_id": "t9", "ts": "x"}
        self.assertEqual(watch.park_key(pos), "goal_budget:t9")

    def test_parked_key_falls_back_to_ts(self) -> None:
        pos = {"state": "parked", "reason": "plan_gate", "ts": "2026-01-01T00:00:00+00:00"}
        self.assertEqual(watch.park_key(pos), "plan_gate:2026-01-01T00:00:00+00:00")

    def test_not_parked_has_no_key(self) -> None:
        self.assertIsNone(watch.park_key({"state": "active", "turn_id": "t1"}))
        self.assertIsNone(watch.park_key({"state": "unknown"}))


class PlanActionsTest(unittest.TestCase):
    def parked(self, turn_id: str = "t1") -> dict:
        return {
            "state": "parked",
            "reason": "goal_budget",
            "ts": ts_at(NOW - 60),
            "age_sec": 60,
            "turn_id": turn_id,
            "num_turns": 1000,
        }

    def test_new_park_notifies_and_records_key(self) -> None:
        actions, nxt = watch.plan_actions({"w": self.parked()}, {})
        self.assertEqual([a["action"] for a in actions], ["notify_park"])
        self.assertEqual(nxt["w"]["key"], "goal_budget:t1")

    def test_same_park_is_silent_on_rescan(self) -> None:
        _, prev = watch.plan_actions({"w": self.parked()}, {})
        actions, nxt = watch.plan_actions({"w": self.parked()}, prev)
        self.assertEqual(actions, [])
        self.assertEqual(nxt["w"]["key"], "goal_budget:t1")

    def test_new_turn_id_is_a_new_park(self) -> None:
        _, prev = watch.plan_actions({"w": self.parked("t1")}, {})
        actions, _ = watch.plan_actions({"w": self.parked("t2")}, prev)
        self.assertEqual([a["action"] for a in actions], ["notify_park"])

    def test_moving_again_notifies_recovery(self) -> None:
        _, prev = watch.plan_actions({"w": self.parked()}, {})
        moved = {"state": "active", "reason": "model.request.started", "ts": ts_at(NOW)}
        actions, nxt = watch.plan_actions({"w": moved}, prev)
        self.assertEqual([a["action"] for a in actions], ["notify_recovery"])
        self.assertIsNone(nxt["w"]["key"])

    def test_never_parked_is_silent(self) -> None:
        actions, nxt = watch.plan_actions({"w": {"state": "active", "reason": "x", "ts": "t"}}, {})
        self.assertEqual(actions, [])
        self.assertIsNone(nxt["w"]["key"])

    def test_worker_gone_from_scan_is_dropped(self) -> None:
        _, prev = watch.plan_actions({"w": self.parked()}, {})
        actions, nxt = watch.plan_actions({}, prev)
        self.assertEqual(actions, [])
        self.assertEqual(nxt, {})


class TailRecordsTest(unittest.TestCase):
    def test_returns_last_records_and_skips_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seg.jsonl"
            lines = [json.dumps(record("tick", ts_at(NOW - 10), turn_id=f"t{i}")) for i in range(5)]
            lines += ["not json", ""]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            records = watch.tail_records(path, count=3)
            self.assertEqual(len(records), 3)
            self.assertEqual(records[-1]["turn_id"], "t4")

    def test_large_file_still_ends_at_the_last_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seg.jsonl"
            filler = [json.dumps(record("tick", ts_at(NOW - 1000), turn_id=f"f{i}")) for i in range(4000)]
            final = json.dumps(record("turn.finished", ts_at(NOW), reason="max_turns", num_turns=1000))
            path.write_text("\n".join(filler + [final]) + "\n", encoding="utf-8")
            self.assertGreater(path.stat().st_size, watch.TAIL_BYTES)
            records = watch.tail_records(path, count=1)
            self.assertEqual(records[-1]["data"]["reason"], "max_turns")


class LoadWorkersTest(unittest.TestCase):
    def test_valid_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workers.json"
            path.write_text(json.dumps({"workers": {"q": {"cwd": CWD, "tmux": "q"}}}), encoding="utf-8")
            workers = watch.load_workers(path)
            self.assertEqual(workers["q"]["cwd"], CWD)

    def test_missing_file_exits(self) -> None:
        with self.assertRaises(SystemExit):
            watch.load_workers(Path("/nonexistent/workers.json"))

    def test_entry_without_cwd_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workers.json"
            path.write_text(json.dumps({"workers": {"q": {"tmux": "q"}}}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                watch.load_workers(path)

    def test_empty_map_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workers.json"
            path.write_text(json.dumps({"workers": {}}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                watch.load_workers(path)


class FormatTest(unittest.TestCase):
    def test_goal_budget_lines_include_resume_hint(self) -> None:
        text = watch.format_park(
            {"worker": "qoder", "reason": "goal_budget", "ts": "2026-09-14T11:29:03+00:00",
             "age_sec": 15515, "num_turns": 1000, "tmux": True}
        )
        self.assertIn("`qoder`", text)
        self.assertIn("1000 iterations", text)
        self.assertIn("`resume qoder`", text)

    def test_goal_budget_without_tmux_also_offers_resume(self) -> None:
        # Since 2026-09-15 the bridge reaches orca-native workers through
        # `orca-ide terminal send`, so the hint no longer points at the UI.
        text = watch.format_park(
            {"worker": "zzbrush", "reason": "goal_budget", "ts": "2026-09-14T11:29:03+00:00",
             "age_sec": 15515, "num_turns": 1000, "tmux": False}
        )
        self.assertIn("`resume zzbrush`", text)

    def test_plan_gate_says_resume_will_not_help(self) -> None:
        text = watch.format_park(
            {"worker": "zzbrush", "reason": "plan_gate", "ts": "2026-09-14T02:36:29+00:00",
             "age_sec": 22000}
        )
        self.assertIn("ExitPlanMode", text)
        self.assertIn("will not help", text)

    def test_human_age_units(self) -> None:
        self.assertEqual(watch.human_age(None), "age unknown")
        self.assertEqual(watch.human_age(45), "45 s")
        self.assertEqual(watch.human_age(300), "5 min")
        self.assertEqual(watch.human_age(7200), "2.0 h")


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workers_file = self.root / "workers.json"
        self.workers_file.write_text(
            json.dumps({"workers": {"q": {"cwd": CWD, "tmux": "q"}}}), encoding="utf-8"
        )
        self.sessions = self.root / "sessions"
        self.state = self.root / "state.json"
        self.db = self.root / "state.sqlite"
        self.store = Store(self.db)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        out = StringIO()
        with redirect_stdout(out):
            code = watch.main(argv)
        return code, out.getvalue()

    def test_probe_reports_parked_worker(self) -> None:
        ingest_into(
            self.store,
            "q",
            [record("turn.finished", ts_at(NOW), reason="max_turns", num_turns=1000)],
            NOW,
        )
        code, out = self.run_main(
            ["--probe", "q", "--json", "--workers-file", str(self.workers_file),
             "--sessions-dir", str(self.sessions), "--db", str(self.db)]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["worker"], "q")
        self.assertEqual(payload["state"], "parked")
        self.assertEqual(payload["reason"], "goal_budget")

    def test_probe_unknown_worker_exits_1(self) -> None:
        code, out = self.run_main(
            ["--probe", "nope", "--json", "--workers-file", str(self.workers_file),
             "--sessions-dir", str(self.sessions), "--db", str(self.db)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"], "unknown worker")

    def test_dry_run_lists_actions_without_state_write(self) -> None:
        ingest_into(
            self.store,
            "q",
            [record("turn.finished", ts_at(NOW), reason="max_turns", num_turns=1000)],
            NOW,
        )
        code, out = self.run_main(
            ["--dry-run", "--workers-file", str(self.workers_file),
             "--sessions-dir", str(self.sessions), "--state-file", str(self.state),
             "--db", str(self.db)]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([a["action"] for a in payload["actions"]], ["notify_park"])
        self.assertFalse(self.state.exists())

    def test_scan_without_events_writes_quiet_state(self) -> None:
        ingest_into(self.store, "q", [record("model.request.started", ts_at(NOW))], NOW)
        code, out = self.run_main(
            ["--workers-file", str(self.workers_file),
             "--sessions-dir", str(self.sessions), "--state-file", str(self.state),
             "--db", str(self.db)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")
        self.assertTrue(self.state.exists())
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["workers"]["q"]["key"], None)


class NewestSegmentTest(unittest.TestCase):
    def test_picks_latest_mtime_across_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = write_segment(root, [record("tick", ts_at(NOW))], session="sess-old")
            newer = write_segment(root, [record("tick", ts_at(NOW))], session="sess-new")
            os.utime(older, (1000, 1000))
            os.utime(newer, (2000, 2000))
            self.assertEqual(watch.newest_segment(root, CWD), newer)

    def test_missing_directory_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(watch.newest_segment(Path(tmp), "/no/such/cwd"))


class CollectPositionsTest(unittest.TestCase):
    def test_carries_tmux_flag_from_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite")
            try:
                ingest_into(
                    store,
                    "a",
                    [record("turn.finished", ts_at(NOW - 60), reason="max_turns", num_turns=1000)],
                    NOW,
                )
                ingest_into(
                    store,
                    "b",
                    [record("turn.finished", ts_at(NOW - 60), reason="max_turns", num_turns=1000)],
                    NOW,
                )
                workers = {
                    "a": {"cwd": CWD, "tmux": "a"},
                    "b": {"cwd": CWD, "tmux": None},
                    "c": {"cwd": "/no/such/cwd", "tmux": "c"},
                }
                positions = watch.collect_positions(workers, root, NOW, store=store)
                self.assertEqual(positions["a"]["state"], "parked")
                self.assertTrue(positions["a"]["tmux"])
                self.assertFalse(positions["b"]["tmux"])
                self.assertEqual(positions["c"]["state"], "idle")
                self.assertTrue(positions["c"]["tmux"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
