#!/usr/bin/env python3
"""Regression guard for the headless executor permission profile.

Posture changed 2026-09-16 (user decision, RUNBOOK 7.10): the executor is no
longer a read-only reporter. Every practical tool is allowed — Read/Glob/Grep,
the write tools, the web tools, subagents, and the whole shell through a bare
`Bash` — so the deny list is now the only thing between a Slack-triggered run
and an unrecoverable action. That makes the deny list the boundary in both
directions: a missing destructive entry silently widens it back to the full
shell, and a missing secret entry re-opens the file gate.

Honest limit, unchanged in kind from the guard hooks: deny rules are
prefix-matched, so `Bash(rm:*)` catches `rm -rf x` but not `cd x && rm -rf .`,
and qodercli 1.1.47 runs no PreToolUse hooks delivered through `--settings`.
The deny list stops accidents and the direct form of an injected instruction,
not indirection. Probed semantics are not contractual — re-run the RUNBOOK 7.10
probes after every qodercli upgrade.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[1] / "config" / "slack-executor-settings.example.json"

# The reviewed allow set — the work tools qodercli 1.1.47 actually exposes, read
# off the on-box stream-json tool dump on 2026-09-16. A new entry here is a
# posture change and has to be a deliberate edit of this list, not a drive-by.
#
# Deliberately NOT here, and why: the session/escalation mechanics
# (CronCreate/CronDelete/CronList/ScheduleWakeup/EnterWorktree/ExitWorktree) are
# not "work" — they let a run outlive itself or leave its sandbox, which is the
# one escalation surface the 2026-09-12 profile denied for cause and the
# relaxation did not need to reopen. They stay denied by default; say so out
# loud if that ever changes.
CAPABILITY_ALLOWS = {
    "Read",
    "Glob",
    "Grep",
    "Write",
    "Edit",
    "NotebookEdit",
    "Bash",
    "WebFetch",
    "WebSearch",
    "ImageGen",
    "ImageSearch",
    "Agent",
    "Workflow",
    "Skill",
    "Monitor",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskStop",
    "TaskUpdate",
    "CreateGoal",
    "GetGoal",
    "UpdateGoal",
}

# Secret material stays unreadable through the file tools. Reading a credential
# is not "work", so the 2026-09-16 relaxation deliberately stops here.
SECRET_DENIES = {
    "Read(**/.env)",
    "Read(**/*.env*)",
    "Read(**/slack.env)",
    "Read(**/health.env)",
    "Read(**/*token*)",
    "Read(**/hosts.yml)",
    "Read(**/id_rsa)",
    "Read(**/id_ecdsa)",
    "Read(**/*.pem)",
    "Read(**/*.key)",
    "Read(**/.netrc)",
    "Read(**/.git-credentials)",
    "Read(**/.aws/**)",
    "Read(**/.gnupg/**)",
    "Read(**/.docker/config.json)",
    "Read(**/.kube/**)",
    "Read(**/.npmrc)",
    "Read(**/.ssh/**)",
    "Read(**/.config/remote-agent/**)",
    "Read(**/.qoder/**)",
    "Read(**/.codex/modes/**)",
    "Read(**/.devspace/**)",
    "Read(**/.bash_history*)",
    "Read(**/.zsh_history*)",
    "Read(/proc/**)",
}

# The destructive floor. Each entry is a first-word or subcommand prefix the
# engine checks literally.
DESTRUCTIVE_DENIES = {
    "Bash(rm:*)",
    "Bash(rmdir:*)",
    "Bash(shred:*)",
    "Bash(dd:*)",
    "Bash(mkfs:*)",
    "Bash(fdisk:*)",
    "Bash(parted:*)",
    "Bash(wipefs:*)",
    "Bash(shutdown:*)",
    "Bash(reboot:*)",
    "Bash(poweroff:*)",
    "Bash(halt:*)",
    "Bash(systemctl poweroff:*)",
    "Bash(systemctl reboot:*)",
    "Bash(sudo:*)",
    "Bash(kill:*)",
    "Bash(killall:*)",
    "Bash(pkill:*)",
    "Bash(mount:*)",
    "Bash(umount:*)",
    "Bash(usermod:*)",
    "Bash(passwd:*)",
    "Bash(crontab:*)",
    "Bash(nft:*)",
    "Bash(iptables:*)",
    "Bash(ufw:*)",
    "Bash(git filter-branch:*)",
    "Bash(git filter-repo:*)",
    "Bash(git push --force:*)",
    "Bash(git push -f:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean -f:*)",
    "Bash(git branch -D:*)",
    "Bash(git reflog expire:*)",
    "Bash(gh repo delete:*)",
    "Bash(gh release delete:*)",
    "Bash(npm publish:*)",
}


class ExecutorSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = json.loads(SETTINGS.read_text(encoding="utf-8"))
        cls.allow = cls.doc["permissions"]["allow"]
        cls.deny = cls.doc["permissions"]["deny"]

    def test_json_shape(self) -> None:
        # defaultMode stays "default": the bridge passes --permission-mode
        # default explicitly, and deny rules carry the destructive floor.
        self.assertEqual(self.doc["permissions"]["defaultMode"], "default")
        self.assertIsInstance(self.allow, list)
        self.assertIsInstance(self.deny, list)

    def test_allow_is_the_reviewed_capability_set(self) -> None:
        self.assertEqual(set(self.allow), CAPABILITY_ALLOWS)

    def test_allow_entries_are_bare_tool_names(self) -> None:
        # The widened posture gives the shell wholesale through a bare `Bash`;
        # a leftover per-command allow would be misleading documentation.
        for entry in self.allow:
            self.assertNotIn("(", entry, f"allow entry is not a bare tool name: {entry}")
            self.assertNotIn("*", entry, f"allow entry carries a wildcard: {entry}")

    def test_secret_denies_present(self) -> None:
        missing = SECRET_DENIES - set(self.deny)
        self.assertEqual(missing, set(), f"missing secret deny entries: {sorted(missing)}")

    def test_destructive_denies_present(self) -> None:
        missing = DESTRUCTIVE_DENIES - set(self.deny)
        self.assertEqual(missing, set(), f"missing destructive deny entries: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()