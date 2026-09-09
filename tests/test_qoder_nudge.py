#!/usr/bin/env python3
"""Nudge must keep the agentic loop going after a self-declared complete."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "qoder-nudge.sh"
loader = SourceFileLoader("qoder_nudge", str(MODULE_PATH))
spec = importlib.util.spec_from_loader("qoder_nudge", loader)
assert spec is not None
nudge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = nudge
loader.exec_module(nudge)

TAECHO_COMPLETE = """
 ▪ Bash(python3 -c "
   import sys…)
   └ harbor samples 12 avg 64.2 minmax (63, 65)
 ▪ UpdateGoal {"status":"complete"}
 ▪ Completed Taecho Village rebuild slices:
   - Added plaza polish, fountain, feature trees,
   lanterns.
   - Verified all 14 lots are populated.
──────────────────────────────────────────────────────────
 YOLO Shift+Tab to Auto Mode
  1 AGENTS.md file · 16 skills
──────────────────────────────────────────────────────────
 * harbor→town→civic terraces on axis x=150, seed
   -5411652232113409693, center (150,390). Do not
   flatten the whole site.
──────────────────────────────────────────────────────────
 Efficient Model · ctx ▓▓▓▓▓▓▓░░░ 66% · +3964 -135
"""

METRO_GENERATING = """
 ▪ WebSearch(Searching the web for: "대구도시철도 1호선")
 Thinking
 │ The searches are not returning specific data.
 ▪ WebSearch(Searching the web for: "대구도시철도 1호선 2호선 power")
 ⠼ Generating... (esc to cancel, 1h 9m 23s)
──────────────────────────────────────────────────────────
 YOLO Shift+Tab to Auto Mode | goal on 6/9999
  1 AGENTS.md file · 16 skills
──────────────────────────────────────────────────────────
 *   Type your message or @path/to/file
──────────────────────────────────────────────────────────
 Efficient Model · ctx ▓▓▓▓▓▓▓░░░ 67% · +16743 -832
"""

IDLE_COMPOSER = """
 ▪ BlueMap reloaded.
                                                      ? for shortcuts
─────────────────────────────────────────────────────────────────────
 YOLO Shift+Tab to Auto Mode
  1 AGENTS.md file · 16 skills
─────────────────────────────────────────────────────────────────────
 *   Type your message or @path/to/file
─────────────────────────────────────────────────────────────────────
 Efficient Model · ctx ▓▓▓▓▓▓▓░░░ 71% · ~/Projects/orca-rust
"""

PAUSED = """
 ▪ BlueMap reloaded.
 YOLO Shift+Tab to Auto Mode | goal paused 0/9999
 *   Type your message or @path/to/file
 Efficient Model · ctx ▓▓▓▓▓▓▓░░░ 71%
"""

SPLASH = """
 Qoder CLI
 loading plugins...
"""


class ClassifyTests(unittest.TestCase):
    def test_self_declared_complete_is_ready_despite_old_bash(self):
        self.assertEqual(nudge.classify(TAECHO_COMPLETE), "complete")
        self.assertTrue(nudge.should_nudge(TAECHO_COMPLETE))

    def test_generating_goal_on_is_busy(self):
        self.assertEqual(nudge.classify(METRO_GENERATING), "busy")
        self.assertFalse(nudge.should_nudge(METRO_GENERATING))

    def test_idle_composer_is_ready(self):
        self.assertEqual(nudge.classify(IDLE_COMPOSER), "idle")
        self.assertTrue(nudge.should_nudge(IDLE_COMPOSER))

    def test_paused_goal_in_fleet_path_is_still_nudged(self):
        self.assertEqual(nudge.classify(PAUSED), "idle")
        self.assertTrue(nudge.should_nudge(PAUSED))

    def test_splash_is_not_ready(self):
        self.assertEqual(nudge.classify(SPLASH), "splash")
        self.assertFalse(nudge.should_nudge(SPLASH))


class GoalTextTests(unittest.TestCase):
    def test_nudge_forbids_complete_and_asks_for_critique(self):
        text = nudge.goal_text("/home/person/Projects/minecraft-server-project")
        self.assertTrue(text.startswith("/goal "))
        self.assertIn("--turns 9999", text)
        self.assertIn("not allowed to mark this goal complete", text.lower())
        self.assertIn("self-critique", text.lower())
        self.assertIn("태초마을", text)
        self.assertIn("ZERO-DEFECT", text)
        self.assertIn("Do not flatten", text)
        self.assertNotIn("UpdateGoal complete", text)

    def test_metro_mission_stays_in_loop_text(self):
        text = nudge.goal_text("/work/korea-metro-twin")
        self.assertIn("Daegu Metro", text)
        self.assertIn("Never stop at a summary", text)


if __name__ == "__main__":
    sys.exit(unittest.main())
