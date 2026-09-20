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


if __name__ == "__main__":
    unittest.main()
