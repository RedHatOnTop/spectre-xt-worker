#!/usr/bin/env python3
"""Shipped ingest_workers Flash path: pin-tree samples + journal alive→dead + .exit."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worker_state import process  # noqa: E402
from worker_state.server import ingest_workers  # noqa: E402
from worker_state.store import Store  # noqa: E402
from worker_state.types import iso_from  # noqa: E402

NOW = 1_800_000_000.0
PIN = "term_flash"


def write_proc(
    proc_root: Path,
    pid: int,
    *,
    cmd: str,
    ppid: int = 1,
    handle: str = "",
    cwd: str | None = None,
    utime: int = 10,
    stime: int = 5,
) -> Path:
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    comm = cmd.split()[0][-15:]
    rest = ["S", str(ppid)] + ["0"] * 9 + [str(utime), str(stime)] + ["0"] * 5
    (d / "stat").write_text(f"{pid} ({comm}) {' '.join(rest)}\n", encoding="utf-8")
    (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode("utf-8") + b"\0")
    env = f"ORCA_TERMINAL_HANDLE={handle}\0".encode("utf-8") if handle else b"\0"
    (d / "environ").write_bytes(env)
    (d / "cgroup").write_text("0::/user.slice\n", encoding="utf-8")
    if cwd:
        target = Path(cwd)
        target.mkdir(parents=True, exist_ok=True)
        link = d / "cwd"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(cwd)
    return d


class IngestFlashExitTest(unittest.TestCase):
    def setUp(self) -> None:
        process._LAST.clear()
        process._LAST_EMIT.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.packets = self.root / "packets"
        self.packets.mkdir()
        self.project = self.root / "minecraft-server-project"
        self.project.mkdir()
        self.store = Store(self.root / "state.sqlite")
        self.store.ingest(
            {
                "event_id": "inj",
                "worker": "minecraft",
                "kind": "terminal_write.succeeded",
                "source": "api",
                "goal_id": "g-m",
                "turn_id": "t-m",
                "dispatch_id": "d-claimed",
                "attempt_id": 1,
                "payload": {"action": "dispatch_goal", "target": "flash", "dispatch_id": "d-claimed"},
                "source_timestamp": iso_from(NOW),
            },
            NOW,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()
        process._LAST.clear()
        process._LAST_EMIT.clear()

    def _workers(self) -> dict[str, dict]:
        return {
            "minecraft": {
                "cwd": str(self.project),
                "tmux": None,
                "terminal": "term_efficient",
                "targets": {"flash": {"terminal": PIN}},
            }
        }

    def _ingest(self, now: float) -> dict:
        return ingest_workers(
            self.store,
            self._workers(),
            self.root / "sessions",
            now,
            shadow=False,
            proc_root=self.proc,
            packets_dir=self.packets,
        )

    def _kinds(self) -> list[str]:
        return [e.kind for e in self.store.events_for("minecraft")]

    def test_never_seen_pid_does_not_dsh_exit_at_t31(self) -> None:
        write_proc(self.proc, 1000, cmd="bash", handle=PIN, cwd=str(self.project))
        (self.packets / "d-claimed.exit").write_text("0\n", encoding="utf-8")
        self._ingest(NOW + 31)
        snap = self.store.snapshot("minecraft", NOW + 31)
        self.assertEqual(snap["goal"]["state"], "INJECTED")
        self.assertEqual(snap["goal"]["dispatch_id"], "d-claimed")
        self.assertEqual(snap["execution"]["target"], "flash")
        kinds = self._kinds()
        self.assertIn("process.sample", kinds)
        sample = [e for e in self.store.events_for("minecraft") if e.kind == "process.sample"][-1]
        self.assertFalse(sample.payload.get("alive"))
        self.assertNotIn("goal.completed", kinds)
        self.assertNotIn("goal.failed", kinds)
        self.assertFalse(any(e.source == "dsh_exit" for e in self.store.events_for("minecraft")))

    def test_alive_then_dead_plus_exit_0_completes_with_identity(self) -> None:
        write_proc(self.proc, 1000, cmd="bash", handle=PIN, cwd=str(self.project))
        write_proc(
            self.proc,
            4242,
            cmd="dsh --profile headless do-the-packet",
            ppid=1000,
            handle=PIN,
            cwd=str(self.project),
        )
        write_proc(
            self.proc,
            7777,
            cmd="dsh --profile tui leftover",
            ppid=1,
            handle="term_other",
            cwd=str(self.root / "other"),
        )
        first = self._ingest(NOW + 1)
        live = first["minecraft"]["snapshot"]
        self.assertTrue(live["execution"]["process_alive"])
        self.assertEqual(live["goal"]["state"], "INJECTED")

        shutil.rmtree(self.proc / "4242")
        (self.packets / "d-claimed.exit").write_text("0\n", encoding="utf-8")
        process._LAST.clear()
        process._LAST_EMIT.clear()
        second = self._ingest(NOW + 2)
        snap = second["minecraft"]["snapshot"]
        self.assertEqual(snap["goal"]["state"], "COMPLETED")
        self.assertEqual(snap["goal"]["goal_id"], "g-m")
        self.assertEqual(snap["goal"]["turn_id"], "t-m")
        self.assertEqual(snap["goal"]["dispatch_id"], "d-claimed")
        self.assertEqual(snap["goal"]["attempt_id"], 1)
        self.assertEqual(snap["execution"]["target"], "flash")
        self.assertTrue(snap["policy"]["grokbot_may_advance"])
        self.assertFalse(snap["policy"]["continuity_recovery_allowed"])
        exits = [e for e in self.store.events_for("minecraft") if e.source == "dsh_exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].event_id, "dsh-exit-minecraft-d-claimed")
        self.assertEqual(exits[0].kind, "goal.completed")
        self.assertEqual(exits[0].goal_id, "g-m")
        self.assertEqual(exits[0].dispatch_id, "d-claimed")

    def test_alive_then_dead_missing_exit_fails_after_30s(self) -> None:
        write_proc(self.proc, 1000, cmd="bash", handle=PIN, cwd=str(self.project))
        write_proc(
            self.proc,
            4242,
            cmd="dsh --profile headless do-the-packet",
            ppid=1000,
            handle=PIN,
            cwd=str(self.project),
        )
        self._ingest(NOW + 1)
        shutil.rmtree(self.proc / "4242")
        process._LAST.clear()
        process._LAST_EMIT.clear()
        early = self._ingest(NOW + 2)
        self.assertEqual(early["minecraft"]["snapshot"]["goal"]["state"], "INJECTED")
        later = self._ingest(NOW + 33)
        snap = later["minecraft"]["snapshot"]
        self.assertEqual(snap["goal"]["state"], "FAILED")
        self.assertEqual(snap["goal"]["goal_id"], "g-m")
        self.assertEqual(snap["goal"]["dispatch_id"], "d-claimed")
        exits = [e for e in self.store.events_for("minecraft") if e.source == "dsh_exit"]
        self.assertEqual(exits[0].kind, "goal.failed")
        self.assertEqual(exits[0].event_id, "dsh-exit-minecraft-d-claimed")


class EvidencePinTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        process._LAST.clear()
        process._LAST_EMIT.clear()

    def tearDown(self) -> None:
        process._LAST.clear()
        process._LAST_EMIT.clear()

    def test_flash_missing_pid_emits_dead_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            entry = {
                "cwd": tmp,
                "terminal": "term_efficient",
                "targets": {"flash": {"terminal": PIN}},
            }
            events = process.evidence_for_worker(
                "minecraft", entry, NOW, target="flash", proc_root=proc
            )
            sample = next(e for e in events if e["kind"] == "process.sample")
            self.assertFalse(sample["payload"]["alive"])
            self.assertEqual(sample["kind"], "process.sample")

    def test_flash_samples_pin_tree_headless_not_tui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            project = Path(tmp) / "proj"
            project.mkdir()
            write_proc(proc, 1000, cmd="bash", handle=PIN, cwd=str(project))
            write_proc(
                proc,
                4242,
                cmd="dsh --profile headless task",
                ppid=1000,
                handle=PIN,
                cwd=str(project),
            )
            write_proc(
                proc,
                9,
                cmd="dsh --profile tui other",
                ppid=1,
                handle="term_other",
                cwd=str(Path(tmp) / "other"),
            )
            entry = {
                "cwd": str(project),
                "targets": {"flash": {"terminal": PIN}},
            }
            events = process.evidence_for_worker(
                "minecraft", entry, NOW, target="flash", proc_root=proc
            )
            sample = next(e for e in events if e["kind"] == "process.sample")
            self.assertTrue(sample["payload"]["alive"])
            self.assertEqual(sample["payload"]["pid"], 4242)


if __name__ == "__main__":
    unittest.main()
