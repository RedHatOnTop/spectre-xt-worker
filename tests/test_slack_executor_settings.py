#!/usr/bin/env python3
"""Regression guard for the headless executor permission profile.

The deny list is the only file gate (Read/Glob are allowlisted wholesale)
and the Bash allowlist is the only command gate, so a wildcard or a
missing deny entry silently widens the boundary. Probed on the box
2026-09-12: the `Agent` tool spawns subagents whose write gate does not
hold, hence the orchestration-tool denies below.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[1] / "config" / "slack-executor-settings.example.json"

# Reviewed `:*` wildcards: each must have been probed on the box.
REVIEWED_WILDCARDS = {
    "Bash(systemctl --user is-active:*)",
    "Bash(systemctl --user status:*)",
    "Bash(df -h:*)",
}

# Deny entries without which the boundary collapses (secrets, egress,
# in-process escalation).
REQUIRED_DENIES = {
    "Agent",
    "Workflow",
    "CronCreate",
    "CronList",
    "CronDelete",
    "ScheduleWakeup",
    "EnterWorktree",
    "ExitWorktree",
    "Edit",
    "Write",
    "NotebookEdit",
    "Monitor",
    "WebFetch",
    "WebSearch",
    "ImageSearch",
    "ImageGen",
    "Read(**/.env)",
    "Read(**/*.env*)",
    "Read(**/slack.env)",
    "Read(**/*token*)",
    "Read(**/.netrc)",
    "Read(**/.git-credentials)",
    "Read(**/.aws/**)",
    "Read(**/.gnupg/**)",
    "Read(**/.docker/config.json)",
    "Read(**/.kube/**)",
    "Read(**/.npmrc)",
    "Read(**/id_ecdsa)",
    "Read(**/*.key)",
    "Read(**/.ssh/**)",
    "Read(**/.config/remote-agent/**)",
    "Read(**/.qoder/**)",
    "Read(**/.codexpro/**)",
    "Read(**/.zcode/**)",
    "Read(**/.bash_history*)",
    "Read(**/.zsh_history*)",
    "Read(/proc/**)",
    "Bash(journalctl:*)",
}

FORBIDDEN_IN_ALLOW = (
    "journalctl",
    "pgrep",
    "curl",
    "wget",
    "sh ",
    "bash ",
    "python",
    "node ",
    "sudo",
    "eval",
    "rm ",
    "mv ",
    "cp ",
    "xargs",
    "tee",
    "dd ",
    "chmod",
    "chown",
)


class ExecutorSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = json.loads(SETTINGS.read_text(encoding="utf-8"))
        cls.allow = cls.doc["permissions"]["allow"]
        cls.deny = cls.doc["permissions"]["deny"]

    def test_json_shape(self) -> None:
        self.assertEqual(self.doc["permissions"]["defaultMode"], "default")
        self.assertIsInstance(self.allow, list)
        self.assertIsInstance(self.deny, list)

    def test_required_denies_present(self) -> None:
        missing = REQUIRED_DENIES - set(self.deny)
        self.assertEqual(missing, set(), f"missing deny entries: {sorted(missing)}")

    def test_allow_entries_are_specific(self) -> None:
        # a bare `Bash` (or `Bash()`) would allow the whole shell
        allowed = re.compile(r"^(Bash\(.+\)|Read|Glob)$")
        for entry in self.allow:
            self.assertRegex(entry, allowed, f"allow entry is not a specific tool/command: {entry}")
            low = entry.lower()
            for bad in FORBIDDEN_IN_ALLOW:
                self.assertNotIn(bad, low, f"allow entry looks dangerous: {entry}")

    def test_wildcard_allows_are_reviewed(self) -> None:
        # the only wildcard form allowed is the reviewed `Bash(pattern:*)`
        wildcard = re.compile(r"^Bash\(.+:\*\)$")
        for entry in self.allow:
            if "*" not in entry:
                continue
            if not wildcard.match(entry):
                self.fail(f"wildcard allow must be an explicit reviewed 'pattern:*' rule: {entry}")
            self.assertIn(entry, REVIEWED_WILDCARDS, f"unreviewed wildcard: {entry}")


if __name__ == "__main__":
    unittest.main()
