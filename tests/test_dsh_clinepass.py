#!/usr/bin/env python3
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "dsh-clinepass"


class WrapperTest(unittest.TestCase):
    def test_unknown_flag_exits_2(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(WRAPPER), "--file", "/nope", "--dispatch-id", "x"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)

    def test_wall_timeout_writes_124(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("not-a-real-key\n", encoding="utf-8")
            key.chmod(0o600)
            fake_dsh = Path(tmp) / "dsh"
            fake_dsh.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
            fake_dsh.chmod(0o755)
            packet = Path(tmp) / "abc.txt"
            packet.write_text("do the work\n", encoding="utf-8")
            env = os.environ.copy()
            env["SPECTRE_DSH_KEY"] = str(key)
            env["SPECTRE_DSH_BIN"] = str(fake_dsh)
            env["SPECTRE_DSH_HOME"] = tmp
            env["SPECTRE_DSH_WALL_SEC"] = "0.3"
            proc = subprocess.run(
                [sys.executable, str(WRAPPER), "--file", str(packet)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
            self.assertEqual(proc.returncode, 124)
            exit_path = packet.with_suffix(".exit")
            self.assertEqual(exit_path.read_text(encoding="utf-8").strip(), "124")

    def test_zero_exit_writes_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("not-a-real-key\n", encoding="utf-8")
            key.chmod(0o600)
            fake_dsh = Path(tmp) / "dsh"
            fake_dsh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_dsh.chmod(0o755)
            packet = Path(tmp) / "ok.txt"
            packet.write_text("done\n", encoding="utf-8")
            env = os.environ.copy()
            env["SPECTRE_DSH_KEY"] = str(key)
            env["SPECTRE_DSH_BIN"] = str(fake_dsh)
            env["SPECTRE_DSH_HOME"] = tmp
            env["SPECTRE_DSH_WALL_SEC"] = "5"
            proc = subprocess.run(
                [sys.executable, str(WRAPPER), "--file", str(packet)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(packet.with_suffix(".exit").read_text(encoding="utf-8").strip(), "0")

    def test_key_mode_not_600_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("x\n", encoding="utf-8")
            key.chmod(0o644)
            packet = Path(tmp) / "p.txt"
            packet.write_text("x\n", encoding="utf-8")
            env = os.environ.copy()
            env["SPECTRE_DSH_KEY"] = str(key)
            env["SPECTRE_DSH_BIN"] = "/bin/true"
            proc = subprocess.run(
                [sys.executable, str(WRAPPER), "--file", str(packet)],
                env=env,
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(proc.returncode, 2)

    def test_wrapper_is_executable_bit_in_tree(self) -> None:
        mode = stat.S_IMODE(WRAPPER.stat().st_mode)
        self.assertTrue(mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
