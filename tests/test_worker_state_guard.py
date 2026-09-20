#!/usr/bin/env python3
"""Architecture guard: after cutover, consumers must not classify raw events.

Until cutover the legacy classifier (qoder-goal-watch) and its tests stay
allowlisted. New consumer code must not grow those literals.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "hook.finished",
    "permission.resolved",
    "model.request.started",
    "session.phase.finished",
    "UpdateGoal",
)
ALLOW = {
    "tests/test_qoder_goal_watch.py",
    "tests/test_goal_supervisor.py",
    "scripts/worker_state/qoder_jsonl.py",
    "scripts/worker_state/resolver.py",
    "scripts/worker_state/types.py",
    "scripts/worker_state/shadow.py",
    "scripts/worker_state/legacy.py",
    "tests/test_worker_state_resolver.py",
    "tests/test_worker_state_store.py",
    "tests/test_worker_state_api.py",
    "tests/test_worker_state_adapter.py",
    "tests/test_worker_state_guard.py",
    "tests/test_dsh_jsonl.py",
    "tests/test_slack_executor_settings.py",
}
SCAN_ROOTS = (ROOT / "scripts", ROOT / "tests")


class GuardTest(unittest.TestCase):
    def test_no_new_raw_event_classifiers(self):
        offenders: list[str] = []
        for root in SCAN_ROOTS:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix not in {".py", ".mjs", ".js"}:
                    continue
                rel = str(path.relative_to(ROOT))
                if rel in ALLOW or rel.startswith("scripts/worker_state/"):
                    continue
                text = path.read_text(encoding="utf-8")
                for token in FORBIDDEN:
                    if token in text:
                        offenders.append(f"{rel}: {token}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
