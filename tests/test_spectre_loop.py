#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "spectre_loop", ROOT / "scripts" / "spectre-loop.py"
)
spectre_loop = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(spectre_loop)


def snap(**over):
    base = {
        "goal": {"state": "COMPLETED", "goal_id": "g1"},
        "policy": {
            "grokbot_may_advance": True,
            "continuity_recovery_allowed": False,
            "idle_slo_violated": False,
        },
        "debug": {},
    }
    base.update(over)
    return base


class LoopEnableTest(unittest.TestCase):
    def test_kill_switch_off_by_default(self) -> None:
        self.assertFalse(spectre_loop.loop_enabled({}))
        self.assertFalse(spectre_loop.astra_enabled({}))
        self.assertTrue(spectre_loop.loop_enabled({"SPECTRE_LOOP": "1"}))


class PlanTickTest(unittest.TestCase):
    def test_disabled(self) -> None:
        out = spectre_loop.plan_tick("qoder", {}, snap(), loop_on=False, astra_on=False, prev={})
        self.assertEqual(out["action"], "disabled")

    def test_planner_pin_skipped_when_astra_off(self) -> None:
        entry = {"cwd": "/tmp/mc", "planner": {"terminal": "term_a"}}
        out = spectre_loop.plan_tick("minecraft", entry, snap(), loop_on=True, astra_on=False, prev={})
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "planner_pin")

    def test_efficient_dispatches_next_goal_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "next_goal.json").write_text(
                json.dumps({"goal": "fix the parser"}), encoding="utf-8"
            )
            entry = {"cwd": tmp}
            out = spectre_loop.plan_tick("qoder", entry, snap(), loop_on=True, astra_on=False, prev={})
            self.assertEqual(out["action"], "goal")
            self.assertEqual(out["text"], "fix the parser")
            self.assertEqual(out["target"], "efficient")

    def test_missing_next_goal_escalates_once_then_sits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entry = {"cwd": tmp}
            first = spectre_loop.plan_tick(
                "qoder", entry, snap(), loop_on=True, astra_on=False, prev={}
            )
            self.assertEqual(first["action"], "escalate")
            second = spectre_loop.plan_tick(
                "qoder",
                entry,
                snap(),
                loop_on=True,
                astra_on=False,
                prev={"escalated": {"g1": True}},
            )
            self.assertEqual(second["action"], "sit")

    def test_blocked_next_goal_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "next_goal.json").write_text(
                json.dumps({"goal": "x", "blocked": True}), encoding="utf-8"
            )
            out = spectre_loop.plan_tick(
                "qoder", {"cwd": tmp}, snap(), loop_on=True, astra_on=False, prev={}
            )
            self.assertEqual(out["action"], "escalate")

    def test_continuity_skip(self) -> None:
        s = snap(policy={"continuity_recovery_allowed": True, "grokbot_may_advance": False})
        out = spectre_loop.plan_tick("qoder", {}, s, loop_on=True, astra_on=False, prev={})
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "continuity")


if __name__ == "__main__":
    unittest.main()
