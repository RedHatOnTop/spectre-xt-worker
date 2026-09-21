#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "spectre_continuity", ROOT / "scripts" / "spectre-continuity.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class ContinuityFlashTest(unittest.TestCase):
    def test_flash_stalled_is_not_resumed(self) -> None:
        mod = _load()
        actions = mod.plan_actions(
            {
                "minecraft": {
                    "goal": {"state": "RUNNING"},
                    "policy": {
                        "continuity_recovery_allowed": False,
                        "can_resume": False,
                    },
                    "reason": "stalled flash",
                    "execution": {"target": "flash", "stalled": True},
                },
                "qoder": {
                    "goal": {"state": "RUNNING"},
                    "policy": {"continuity_recovery_allowed": True},
                    "reason": "stall",
                },
            }
        )
        by = {a["worker"]: a["action"] for a in actions}
        self.assertEqual(by["minecraft"], "skip")
        self.assertEqual(by["qoder"], "resume")

    def test_cli_snapshot_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snaps.json"
            path.write_text(
                json.dumps(
                    {
                        "minecraft": {
                            "goal": {"state": "RUNNING"},
                            "policy": {"continuity_recovery_allowed": False},
                        }
                    }
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "spectre-continuity.py"),
                    "--dry-run",
                    "--snapshot-file",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["actions"][0]["action"], "skip")
            self.assertNotEqual(payload["actions"][0]["action"], "resume")


if __name__ == "__main__":
    unittest.main()
