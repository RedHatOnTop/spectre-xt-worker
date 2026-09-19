#!/usr/bin/env python3
"""Guard the read-only agy profile installed for the #lobby debate.

The example is the source of truth for ~/.gemini/antigravity-cli/settings.json
on the box. agy's permission semantics are NOT contractual, so the on-box
probes in RUNBOOK 7.10 decide whether the profile actually holds — these
tests only keep the repo copy honest.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "config" / "agy-slack-settings.example.json"


class AgySlackSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = SETTINGS.read_text(encoding="utf-8")
        self.payload = json.loads(self.raw)
        self.deny = self.payload["permissions"]["deny"]

    def test_is_a_permissions_object_with_a_deny_list(self) -> None:
        self.assertIsInstance(self.payload, dict)
        self.assertIsInstance(self.payload["permissions"], dict)
        self.assertIsInstance(self.deny, list)
        self.assertTrue(all(isinstance(entry, str) and entry for entry in self.deny))

    def test_denies_all_writes(self) -> None:
        self.assertIn("write_file(**)", self.deny)

    def test_denies_egress_commands(self) -> None:
        commands = [entry for entry in self.deny if entry.startswith("command(")]
        self.assertTrue(commands)
        joined = " ".join(commands)
        for binary in ("curl", "wget", "nc", "ssh", "scp", "rsync"):
            self.assertIn(binary, joined)

    def test_denies_secret_paths(self) -> None:
        reads = " ".join(entry for entry in self.deny if entry.startswith("read_file("))
        self.assertIn("slack.env", reads)
        self.assertIn(".ssh", reads)
        self.assertIn(".codexpro", reads)
        self.assertIn("remote-agent", reads)

    def test_never_grants_anything(self) -> None:
        # Read-only posture: nothing pre-approved (headless default is deny).
        # No allow list at all, and never a write/command grant hidden in it.
        allow = self.payload["permissions"].get("allow", [])
        self.assertEqual(allow, [])
        for entry in allow:
            self.assertFalse(entry.startswith(("write_file", "command")), entry)

    def test_bootstrap_installs_the_example_never_the_live_path(self) -> None:
        bootstrap = (ROOT / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("agy-slack-settings.example.json", bootstrap)
        # ~/.gemini is user-owned; the installer must never write there.
        # Comments may mention the path (to say so); executable lines may not.
        code = "\n".join(
            line for line in bootstrap.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn(".gemini", code)


if __name__ == "__main__":
    unittest.main()
