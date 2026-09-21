#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "spectre_reaper", ROOT / "scripts" / "spectre-reaper.py"
)
spectre_reaper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(spectre_reaper)


class ReaperDecideTest(unittest.TestCase):
    def test_allowlist_listener_kept(self) -> None:
        self.assertEqual(
            spectre_reaper.decide(
                {"cmd": "orca-ide serve", "listen": {6768}, "handle": ""},
                listen_ports={6768},
                pins=set(),
                flash_done_age=None,
            ),
            "keep",
        )

    def test_paper_jar_reaped_while_listening(self) -> None:
        self.assertEqual(
            spectre_reaper.decide(
                {"cmd": "java -jar paper.jar nogui", "listen": {25569}, "handle": ""},
                listen_ports={25569},
                pins=set(),
                flash_done_age=None,
            ),
            "term",
        )

    def test_minecraft_client_reaped(self) -> None:
        self.assertEqual(
            spectre_reaper.decide(
                {"cmd": "java KnotClient --quickPlayMultiplayer localhost:25569", "listen": {8765}},
                listen_ports={8765},
                pins=set(),
                flash_done_age=None,
            ),
            "term",
        )

    def test_pin_shell_kept(self) -> None:
        self.assertEqual(
            spectre_reaper.decide(
                {"cmd": "bash", "handle": "term_flash", "listen": set()},
                listen_ports=set(),
                pins={"term_flash"},
                flash_done_age=None,
            ),
            "keep",
        )

    def test_in_pin_headless_after_300s_termed(self) -> None:
        self.assertEqual(
            spectre_reaper.decide(
                {
                    "cmd": "dsh --profile headless do-work",
                    "handle": "term_flash",
                    "listen": set(),
                },
                listen_ports=set(),
                pins={"term_flash"},
                flash_done_age=301,
            ),
            "term",
        )
        self.assertEqual(
            spectre_reaper.decide(
                {
                    "cmd": "dsh --profile headless do-work",
                    "handle": "term_flash",
                    "listen": set(),
                },
                listen_ports=set(),
                pins={"term_flash"},
                flash_done_age=10,
            ),
            "keep",
        )

    def test_foreign_tui_kept(self) -> None:
        self.assertEqual(
            spectre_reaper.decide(
                {
                    "cmd": "dsh --profile tui",
                    "handle": "term_other",
                    "listen": set(),
                },
                listen_ports=set(),
                pins={"term_flash"},
                flash_done_age=900,
            ),
            "keep",
        )

    def test_unknown_listen_kept(self) -> None:
        self.assertEqual(
            spectre_reaper.decide(
                {"cmd": "mystery", "listen": set(), "listen_unknown": True},
                listen_ports=set(),
                pins=set(),
                flash_done_age=None,
            ),
            "keep",
        )


class ReaperCliTest(unittest.TestCase):
    def test_fixture_dry_run_uses_decide(self) -> None:
        import json
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "fix.json"
            fixture.write_text(
                json.dumps(
                    {
                        "listen_ports": [25569, 6768],
                        "pins": ["term_flash"],
                        "flash_done_age": 400,
                        "procs": [
                            {"cmd": "java -jar paper.jar", "listen": [25569], "handle": ""},
                            {"cmd": "dsh --profile tui", "listen": [], "handle": "term_other"},
                            {
                                "cmd": "dsh --profile headless x",
                                "listen": [],
                                "handle": "term_flash",
                            },
                            {"cmd": "/usr/bin/orca-ide serve", "listen": [6768], "handle": ""},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "spectre-reaper.py"), "--fixture", str(fixture)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["dry_run"])
            by_cmd = {row["cmd"]: row["action"] for row in payload["decisions"]}
            self.assertEqual(by_cmd["java -jar paper.jar"], "term")
            self.assertEqual(by_cmd["dsh --profile tui"], "keep")
            self.assertEqual(by_cmd["dsh --profile headless x"], "term")
            self.assertEqual(by_cmd["/usr/bin/orca-ide serve"], "keep")


if __name__ == "__main__":
    unittest.main()
