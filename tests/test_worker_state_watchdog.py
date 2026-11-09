"""Recovery of a live process whose authoritative socket stopped accepting calls."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worker_state import watchdog


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "watchdog.json"

    def test_one_failure_does_not_restart(self):
        calls = []
        result = watchdog.tick(self.state, 100, lambda: False,
                               lambda: calls.append("restart") or True)
        self.assertEqual(result["action"], "observe")
        self.assertEqual(calls, [])
        self.assertEqual(json.loads(self.state.read_text())["failures"], 1)

    def test_second_failure_restarts_and_requires_health(self):
        calls = []
        health = iter([False, True])
        watchdog.tick(self.state, 100, lambda: False, lambda: True)
        result = watchdog.tick(self.state, 160, lambda: next(health),
                               lambda: calls.append("restart") or True)
        self.assertEqual(result["action"], "recovered")
        self.assertEqual(calls, ["restart"])
        self.assertEqual(json.loads(self.state.read_text())["failures"], 0)

    def test_restart_without_recovery_keeps_failure_and_counts_attempt(self):
        watchdog.tick(self.state, 100, lambda: False, lambda: True)
        result = watchdog.tick(self.state, 160, lambda: False, lambda: True)
        self.assertEqual(result["action"], "restart_failed")
        state = json.loads(self.state.read_text())
        self.assertEqual(state["failures"], 2)
        self.assertEqual(state["restarts"], [160])

    def test_hourly_cap_blocks_restart_but_recovers_after_window(self):
        self.state.write_text(json.dumps({"failures": 2, "restarts": [1, 2, 3]}))
        calls = []
        result = watchdog.tick(self.state, 100, lambda: False,
                               lambda: calls.append("restart") or True)
        self.assertEqual(result["action"], "restart_cap")
        self.assertEqual(calls, [])
        health = iter([False, True])
        result = watchdog.tick(self.state, 3700, lambda: next(health),
                               lambda: calls.append("restart") or True)
        self.assertEqual(result["action"], "recovered")
        self.assertEqual(calls, ["restart"])

    def test_healthy_probe_clears_failure_streak(self):
        self.state.write_text(json.dumps({"failures": 1, "restarts": [10]}))
        result = watchdog.tick(self.state, 100, lambda: True, lambda: False)
        self.assertEqual(result["action"], "healthy")
        self.assertEqual(json.loads(self.state.read_text())["failures"], 0)

    def test_corrupt_state_fails_closed(self):
        self.state.write_text("not json")
        with self.assertRaises(ValueError):
            watchdog.tick(self.state, 100, lambda: False, lambda: True)

    def test_symlink_state_fails_closed(self):
        target = Path(self.temp.name) / "other.json"
        target.write_text('{"failures": 0, "restarts": []}')
        self.state.symlink_to(target)
        with self.assertRaises(ValueError):
            watchdog.tick(self.state, 100, lambda: True, lambda: True)

    def test_invalid_state_fails_closed(self):
        self.state.write_text('{"failures": -1, "restarts": []}')
        with self.assertRaises(ValueError):
            watchdog.tick(self.state, 100, lambda: True, lambda: True)

    def test_state_is_private(self):
        watchdog.save(self.state, {"failures": 0, "restarts": []})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_probe_requires_healthy_http_response(self):
        with patch.object(watchdog, 'StateClient') as client:
            client.return_value.health.return_value = (503, {"ok": True})
            self.assertFalse(watchdog._probe())
            client.return_value.health.return_value = (200, {"ok": True})
            self.assertTrue(watchdog._probe())

    def test_restart_command_and_timeout(self):
        with patch.object(watchdog.subprocess, 'run') as run, patch.object(watchdog.time, 'sleep'):
            run.return_value.returncode = 0
            self.assertTrue(watchdog._restart())
            self.assertEqual(run.call_args.args[0][-1], 'spectre-worker-state.service')
            run.side_effect = watchdog.subprocess.TimeoutExpired('systemctl', 30)
            self.assertFalse(watchdog._restart())

    def test_main_disabled_and_corrupt_state(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(watchdog.main(), 0)
        self.state.write_text('broken')
        with patch.dict(os.environ, {'SPECTRE_STATE_WATCHDOG': '1'}), \
             patch.object(watchdog, 'STATE', self.state):
            self.assertEqual(watchdog.main(), 1)

    def test_main_notifies_on_recovery(self):
        self.state.write_text(json.dumps({"failures": 1, "restarts": []}))
        health = iter([False, True])
        with patch.dict(os.environ, {'SPECTRE_STATE_WATCHDOG': '1'}), \
             patch.object(watchdog, 'STATE', self.state), \
             patch.object(watchdog, '_probe', side_effect=lambda: next(health)), \
             patch.object(watchdog, '_restart', return_value=True), \
             patch.object(watchdog, '_notify') as notify:
            self.assertEqual(watchdog.main(), 0)
            notify.assert_called_once_with('recovered')


if __name__ == "__main__":
    unittest.main()
