#!/usr/bin/env python3
"""Unit tests for the spectre-slack-notify CLI."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "slack-notify.py"

spec = importlib.util.spec_from_file_location("slack_notify", SCRIPT)
assert spec and spec.loader
slack_notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slack_notify)

FAKE_TOKEN = "xoxb-test-secret-token-0000000000"
FAKE_APP_TOKEN = "xapp-test-secret-token-0000000000"

CHANNELS = {
    "SLACK_CHANNEL_ALERTS": "C0123ALERTS",
    "SLACK_CHANNEL_FLEET": "C0123FLEET",
    "SLACK_CHANNEL_CONTROL": "C0123CONTRO",
    "SLACK_CHANNEL_LOBBY": "C0123LOBBY",
}


def write_env(path: Path) -> None:
    lines = [f"SLACK_BOT_TOKEN={FAKE_TOKEN}", f"SLACK_APP_TOKEN={FAKE_APP_TOKEN}"]
    lines += [f"{key}={value}" for key, value in CHANNELS.items()]
    lines.append("SLACK_ALLOWED_USERS=U0123ABCDE")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class AliasTest(unittest.TestCase):
    def test_alias_resolves_to_channel_id(self) -> None:
        self.assertEqual(slack_notify.resolve_channel("alerts", CHANNELS), "C0123ALERTS")
        self.assertEqual(slack_notify.resolve_channel("lobby", CHANNELS), "C0123LOBBY")

    def test_raw_channel_id_passes_through(self) -> None:
        self.assertEqual(slack_notify.resolve_channel("C0ABCDEFGHI", {}), "C0ABCDEFGHI")

    def test_unknown_alias_raises(self) -> None:
        with self.assertRaises(SystemExit):
            slack_notify.resolve_channel("ops", CHANNELS)

    def test_missing_channel_value_raises(self) -> None:
        with self.assertRaises(SystemExit):
            slack_notify.resolve_channel("alerts", {})


class IdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agents = slack_notify.load_registry(ROOT / "config" / "slack-agents.json")

    def test_registry_has_the_seven_agents(self) -> None:
        self.assertEqual(
            sorted(self.agents),
            ["bridge", "claude", "healthcheck", "orca", "qoder", "spectre", "zcode"],
        )

    def test_known_agent_lookup(self) -> None:
        ident = slack_notify.identity_for(self.agents, "healthcheck")
        self.assertEqual(ident["username"], "healthcheck")
        self.assertTrue(ident["icon_emoji"].startswith(":"))

    def test_unknown_agent_raises(self) -> None:
        with self.assertRaises(SystemExit):
            slack_notify.identity_for(self.agents, "nope")

    def test_payload_shape_uses_customize_fields(self) -> None:
        ident = slack_notify.identity_for(self.agents, "orca")
        payload = slack_notify.build_payload("C0123LOBBY", "hi", ident)
        self.assertEqual(payload["username"], "orca")
        self.assertEqual(payload["icon_emoji"], ":satellite:")
        self.assertNotIn("as_user", payload)
        self.assertIs(payload["unfurl_links"], False)
        self.assertIs(payload["unfurl_media"], False)
        self.assertNotIn("thread_ts", payload)

    def test_payload_thread_ts_included_when_given(self) -> None:
        ident = slack_notify.identity_for(self.agents, "bridge")
        payload = slack_notify.build_payload("C0123LOBBY", "hi", ident, "1736188888.123456")
        self.assertEqual(payload["thread_ts"], "1736188888.123456")


class EnvFileTest(unittest.TestCase):
    def test_parse_skips_comments_blanks_and_bad_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slack.env"
            path.write_text(
                "# comment\n\nSLACK_BOT_TOKEN=xoxb-abc\nNO_EQUALS_SIGN\n",
                encoding="utf-8",
            )
            got = slack_notify.load_env_file(path)
        self.assertEqual(got, {"SLACK_BOT_TOKEN": "xoxb-abc"})

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(slack_notify.load_env_file(Path("/nonexistent/slack.env")), {})


class CliSubprocessTest(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_dry_run_builds_payload_and_never_prints_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "slack.env"
            write_env(env_file)
            proc = self.run_cli(
                "--dry-run",
                "--env-file",
                str(env_file),
                "--agent",
                "healthcheck",
                "--channel",
                "alerts",
                "--text",
                "slack online",
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(FAKE_TOKEN, proc.stdout)
        self.assertNotIn(FAKE_APP_TOKEN, proc.stdout)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["channel"], "C0123ALERTS")
        self.assertEqual(payload["username"], "healthcheck")
        self.assertEqual(payload["text"], "slack online")

    def test_disabled_when_env_file_missing(self) -> None:
        proc = self.run_cli(
            "--dry-run", "--env-file", "/nonexistent/slack.env", "--channel", "lobby", "--text", "hi"
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("slack-notify: disabled", proc.stdout)

    def test_recovery_prefixes_check_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "slack.env"
            write_env(env_file)
            proc = self.run_cli(
                "--dry-run",
                "--env-file",
                str(env_file),
                "--channel",
                "alerts",
                "--recovery",
                "--text",
                "recovered",
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["text"], ":white_check_mark: recovered")

    def test_bad_thread_ts_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "slack.env"
            write_env(env_file)
            proc = self.run_cli(
                "--env-file",
                str(env_file),
                "--channel",
                "lobby",
                "--thread-ts",
                "not-a-ts",
                "--text",
                "hi",
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bad --thread-ts", proc.stderr)

    def test_self_test_ok_on_full_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "slack.env"
            write_env(env_file)
            env_file.chmod(0o600)
            proc = self.run_cli("--self-test", "--env-file", str(env_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK (4 channels, 7 agents)", proc.stdout)
        self.assertNotIn(FAKE_TOKEN, proc.stdout)


if __name__ == "__main__":
    unittest.main()
