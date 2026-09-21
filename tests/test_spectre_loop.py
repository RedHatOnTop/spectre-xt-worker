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


class LoopApplyTest(unittest.TestCase):
    def test_dry_run_does_not_call_typer(self) -> None:
        out = spectre_loop.apply_action(
            {"worker": "qoder", "action": "goal", "text": "fix", "target": "efficient"},
            dry=True,
            environ={"SPECTRE_DISPATCH_BIN": "/nope/missing"},
        )
        self.assertFalse(out["applied"])
        self.assertNotIn("io", out)

    def test_live_goal_invokes_dispatch_bin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "dispatch.log"
            bin_ = Path(tmp) / "dispatch"
            bin_.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SPECTRE_DISPATCH_LOG\"\n"
                "printf '%s\\n' '{\"ok\":true,\"evt\":\"dispatch_sent\"}'\n",
                encoding="utf-8",
            )
            bin_.chmod(0o755)
            env = {"SPECTRE_DISPATCH_BIN": str(bin_), "SPECTRE_DISPATCH_LOG": str(log)}
            out = spectre_loop.apply_action(
                {"worker": "qoder", "action": "goal", "text": "fix the parser", "target": "efficient"},
                dry=False,
                environ=env,
            )
            self.assertTrue(out["applied"])
            logged = log.read_text(encoding="utf-8")
            self.assertIn("--dispatch goal qoder fix the parser --target efficient", logged)

    def test_escalate_marks_applied_even_if_notify_missing(self) -> None:
        out = spectre_loop.apply_action(
            {"worker": "qoder", "action": "escalate", "ident": "g1", "reason": "no_next_goal"},
            dry=False,
            environ={"SPECTRE_NOTIFY_BIN": "/nope/missing-notify"},
        )
        self.assertTrue(out["applied"])
        persisted = spectre_loop.persist_from_actions({}, [out])
        self.assertTrue(persisted["escalated"]["g1"])


class LoopCliTest(unittest.TestCase):
    def test_snapshot_file_skips_planner_when_astra_off(self) -> None:
        import json
        import os
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            snaps = Path(tmp) / "snaps.json"
            workers = Path(tmp) / "workers.json"
            snaps.write_text(
                json.dumps(
                    {
                        "minecraft": {
                            "goal": {"state": "COMPLETED", "goal_id": "g1"},
                            "policy": {"grokbot_may_advance": True},
                            "debug": {},
                        }
                    }
                ),
                encoding="utf-8",
            )
            workers.write_text(
                json.dumps(
                    {
                        "workers": {
                            "minecraft": {
                                "cwd": tmp,
                                "planner": {"terminal": "term_a"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["SPECTRE_LOOP"] = "1"
            env.pop("ASTRA_ENABLED", None)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "spectre-loop.py"),
                    "--dry-run",
                    "--snapshot-file",
                    str(snaps),
                    "--workers-file",
                    str(workers),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["actions"][0]["action"], "skip")
            self.assertEqual(payload["actions"][0]["reason"], "planner_pin")

    def test_main_live_dispatches_next_goal_and_persists_escalate_once(self) -> None:
        import os
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "qoder"
            work.mkdir()
            (work / "next_goal.json").write_text(
                json.dumps({"goal": "fix the parser"}), encoding="utf-8"
            )
            snaps = Path(tmp) / "snaps.json"
            workers = Path(tmp) / "workers.json"
            state = Path(tmp) / "loop-state.json"
            log = Path(tmp) / "dispatch.log"
            notify_log = Path(tmp) / "notify.log"
            dispatch = Path(tmp) / "dispatch"
            notify = Path(tmp) / "notify"
            dispatch.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SPECTRE_DISPATCH_LOG\"\n"
                "printf '%s\\n' '{\"ok\":true,\"evt\":\"dispatch_sent\"}'\n",
                encoding="utf-8",
            )
            dispatch.chmod(0o755)
            notify.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SPECTRE_NOTIFY_LOG\"\n"
                "printf '%s\\n' ok\n",
                encoding="utf-8",
            )
            notify.chmod(0o755)
            snaps.write_text(
                json.dumps(
                    {
                        "qoder": {
                            "goal": {"state": "COMPLETED", "goal_id": "g1"},
                            "policy": {"grokbot_may_advance": True},
                            "debug": {},
                        }
                    }
                ),
                encoding="utf-8",
            )
            workers.write_text(
                json.dumps({"workers": {"qoder": {"cwd": str(work)}}}),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["SPECTRE_LOOP"] = "1"
            env.pop("ASTRA_ENABLED", None)
            env["SPECTRE_LOOP_STATE"] = str(state)
            env["SPECTRE_DISPATCH_BIN"] = str(dispatch)
            env["SPECTRE_NOTIFY_BIN"] = str(notify)
            env["SPECTRE_DISPATCH_LOG"] = str(log)
            env["SPECTRE_NOTIFY_LOG"] = str(notify_log)
            cmd = [
                sys.executable,
                str(ROOT / "scripts" / "spectre-loop.py"),
                "--snapshot-file",
                str(snaps),
                "--workers-file",
                str(workers),
            ]
            first = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            payload = json.loads(first.stdout)
            self.assertEqual(payload["actions"][0]["action"], "goal")
            self.assertTrue(payload["actions"][0]["applied"])
            self.assertIn("--dispatch goal qoder fix the parser --target efficient", log.read_text(encoding="utf-8"))
            self.assertFalse(notify_log.exists())

            (work / "next_goal.json").unlink()
            second = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
            self.assertEqual(second.returncode, 0, second.stderr)
            escalate = json.loads(second.stdout)
            self.assertEqual(escalate["actions"][0]["action"], "escalate")
            self.assertTrue(escalate["actions"][0]["applied"])
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertTrue(saved["escalated"]["g1"])
            self.assertIn("lobby", notify_log.read_text(encoding="utf-8"))

            third = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
            self.assertEqual(third.returncode, 0, third.stderr)
            sit = json.loads(third.stdout)
            self.assertEqual(sit["actions"][0]["action"], "sit")
            self.assertFalse(sit["actions"][0]["applied"])
            notify_lines = notify_log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(notify_lines), 1)


if __name__ == "__main__":
    unittest.main()
