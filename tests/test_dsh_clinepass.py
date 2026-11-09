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
    def test_timeout_cleanup_kills_group_after_leader_exits(self):
        import runpy
        from unittest.mock import MagicMock, patch
        wrapper = runpy.run_path(str(WRAPPER))
        proc = MagicMock(pid=12345)
        with patch('os.killpg') as kill:
            wrapper['stop_group'](proc)
        self.assertEqual([call.args[1] for call in kill.call_args_list], [15, 9])

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
            fake_dsh.write_text(
                "#!" + sys.executable + "\nimport time\ntime.sleep(30)\n",
                encoding="utf-8",
            )
            fake_dsh.chmod(0o755)
            packet = Path(tmp) / "abc.txt"
            packet.write_text("do the work\n", encoding="utf-8")
            env = {**os.environ, "SPECTRE_PACKET_DIR": tmp}
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
            fake_dsh.write_text("#!" + sys.executable + "\nimport sys\nsys.exit(0)\n", encoding="utf-8")
            fake_dsh.chmod(0o755)
            packet = Path(tmp) / "ok.txt"
            packet.write_text("done\n", encoding="utf-8")
            env = {**os.environ, "SPECTRE_PACKET_DIR": tmp}
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

    def test_free_tier_uses_isolated_profile_and_proxy_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / 'client_key'
            key.write_text('proxy-client-key\n', encoding='utf-8')
            key.chmod(0o600)
            home = root / 'free-home'
            home.mkdir()
            config = home / 'settings.yaml'
            config.write_text('free profile\n', encoding='utf-8')
            config.chmod(0o600)
            binary = root / 'dsh'
            binary.write_text('#!' + sys.executable + '\n'
                'import os\n'
                'assert os.environ["CLINE_API_KEY"] == "proxy-client-key"\n'
                'assert os.environ["DSH_HOME"] == ' + repr(str(home)) + '\n')
            binary.chmod(0o755)
            packet = root / 'free.txt'
            packet.write_text('free work\n', encoding='utf-8')
            env = {**os.environ, 'SPECTRE_PACKET_DIR': tmp,
                   'SPECTRE_DSH_FREE_KEY': str(key),
                   'SPECTRE_DSH_FREE_HOME': str(home),
                   'SPECTRE_DSH_BIN': str(binary)}
            proc = subprocess.run([sys.executable, str(WRAPPER), '--file', str(packet),
                                   '--tier', 'free'], env=env, capture_output=True,
                                  text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_free_tier_refuses_missing_private_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / 'client_key'
            key.write_text('key', encoding='utf-8')
            key.chmod(0o600)
            home = root / 'free-home'
            home.mkdir()
            packet = root / 'free.txt'
            packet.write_text('work', encoding='utf-8')
            env = {**os.environ, 'SPECTRE_PACKET_DIR': tmp,
                   'SPECTRE_DSH_FREE_KEY': str(key),
                   'SPECTRE_DSH_FREE_HOME': str(home),
                   'SPECTRE_DSH_BIN': '/bin/true'}
            proc = subprocess.run([sys.executable, str(WRAPPER), '--file', str(packet),
                                   '--tier', 'free'], env=env, capture_output=True,
                                  text=True, timeout=5)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(packet.with_suffix('.exit').read_text().strip(), '2')

    def test_key_mode_not_600_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("x\n", encoding="utf-8")
            key.chmod(0o644)
            packet = Path(tmp) / "p.txt"
            packet.write_text("x\n", encoding="utf-8")
            env = {**os.environ, "SPECTRE_PACKET_DIR": tmp}
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

    def test_packet_outside_spool_is_rejected_without_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet = Path(tmp) / "outside.txt"
            packet.write_text("do not execute")
            proc = subprocess.run([sys.executable, str(WRAPPER), "--file", str(packet)],
                env={**os.environ, "SPECTRE_PACKET_DIR": str(Path(tmp) / "spool")},
                capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(packet.with_suffix(".exit").exists())

    def test_output_stays_visible_and_sidecar_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("fixture-key")
            key.chmod(0o600)
            packet = Path(tmp) / "visible.txt"
            packet.write_text("work")
            binary = Path(tmp) / "dsh"
            binary.write_text("#!" + sys.executable + "\nprint('visible progress')\n")
            binary.chmod(0o755)
            env = {**os.environ, "SPECTRE_PACKET_DIR": tmp, "SPECTRE_DSH_KEY": str(key),
                   "SPECTRE_DSH_BIN": str(binary)}
            proc = subprocess.run([sys.executable, str(WRAPPER), "--file", str(packet)],
                                  env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("visible progress", proc.stdout)
            self.assertEqual(packet.with_suffix(".exit").stat().st_mode & 0o777, 0o600)

    def test_wrapper_is_executable_bit_in_tree(self) -> None:
        mode = stat.S_IMODE(WRAPPER.stat().st_mode)
        self.assertTrue(mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
