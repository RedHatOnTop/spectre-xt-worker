#!/usr/bin/env python3
"""Bootstrap must disable every retired occupancy classifier (control-plane PR 1)."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap.sh"
DOCTOR = ROOT / "scripts" / "doctor.sh"

RETIRED = (
    "qoder-nudge.timer",
    "qoder-continuity.timer",
    "codex-goal-healer.timer",
    "grokbot-goal-event.timer",
    "grokbot-goal-event.path",
    "local-listener-reaper.timer",
    "qoder-idle-reaper.timer",
    "native-worker-pin-sync.timer",
)


def _legacy_loop(text: str) -> str:
    match = re.search(
        r"for legacy in (.*?); do",
        text,
        flags=re.S,
    )
    if match is None:
        return ""
    return re.sub(r"\s+", " ", match.group(1))


class BootstrapRetiredClassifiers(unittest.TestCase):
    def test_disable_loop_lists_every_retired_unit(self) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        loop = _legacy_loop(text)
        self.assertTrue(loop, "bootstrap.sh has no `for legacy in …` disable loop")
        missing = [name for name in RETIRED if name not in loop]
        self.assertEqual(missing, [], f"retired units missing from disable loop: {missing}")

    def test_bootstrap_does_not_enable_goal_supervisor_timer(self) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertNotIn(
            "enable goal-supervisor.timer",
            text,
            "bootstrap must not enable goal-supervisor.timer even if grok exists",
        )


class DoctorRetiredClassifiers(unittest.TestCase):
    def test_doctor_flags_every_retired_timer_if_enabled(self) -> None:
        text = DOCTOR.read_text(encoding="utf-8")
        match = re.search(
            r"for ws_legacy in (.*?); do",
            text,
            flags=re.S,
        )
        self.assertIsNotNone(match, "doctor.sh has no ws_legacy enablement loop")
        loop = re.sub(r"\s+", " ", match.group(1))
        # .path is a path unit; doctor checks timers that can be enabled.
        expected = [name for name in RETIRED if name.endswith(".timer")]
        missing = [name for name in expected if name not in loop]
        self.assertEqual(missing, [], f"doctor ws_legacy loop missing: {missing}")

    def test_doctor_treats_goal_supervisor_timer_enabled_as_bad(self) -> None:
        text = DOCTOR.read_text(encoding="utf-8")
        self.assertNotRegex(
            text,
            r'ok "goal-supervisor\.timer enabled',
            "enabled goal-supervisor.timer is no longer a healthy state",
        )


if __name__ == "__main__":
    unittest.main()
