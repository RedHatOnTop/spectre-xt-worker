#!/usr/bin/env python3
"""Unit tests for the spectre-slack-notify CLI."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_registry_has_the_known_agents(self) -> None:
        self.assertEqual(
            sorted(self.agents),
            [
                "antigravity",
                "bridge",
                "claude",
                "grok",
                "healthcheck",
                "loop",
                "orca",
                "qoder",
                "spectre",
                "zcode",
            ],
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

    def test_update_payload_carries_only_channel_ts_and_text(self) -> None:
        payload = slack_notify.build_update_payload("C0123LOBBY", "merged", "1736188888.123456")
        self.assertEqual(
            payload, {"channel": "C0123LOBBY", "ts": "1736188888.123456", "text": "merged"}
        )


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
        self.assertIn("OK (4 channels, 10 agents)", proc.stdout)
        self.assertNotIn(FAKE_TOKEN, proc.stdout)



class UpdateTsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = Path(self.tmp.name) / "slack.env"
        write_env(self.env_file)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--env-file", str(self.env_file), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_dry_run_prints_the_update_payload(self) -> None:
        proc = self.cli(
            "--dry-run", "--channel", "lobby", "--update-ts", "1736188888.123456", "--text", "merged"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            {"channel": "C0123LOBBY", "ts": "1736188888.123456", "text": "merged"},
        )

    def test_bad_update_ts_rejected(self) -> None:
        proc = self.cli("--channel", "lobby", "--update-ts", "nope", "--text", "hi")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bad --update-ts", proc.stderr)

    def test_update_and_thread_are_exclusive(self) -> None:
        proc = self.cli(
            "--channel", "lobby", "--update-ts", "1.5", "--thread-ts", "1.4", "--text", "hi"
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("exclusive", proc.stderr)

    def test_update_goes_to_chat_update_and_reports_updated(self) -> None:
        calls: list[tuple[str, dict[str, object], str]] = []

        def fake_post(token: str, payload: dict[str, object], url: str = slack_notify.API_URL):
            calls.append((token, payload, url))
            return True, "1736188888.123456"

        out = io.StringIO()
        with mock.patch.object(slack_notify, "post", fake_post), contextlib.redirect_stdout(out):
            rc = slack_notify.main(
                [
                    "--env-file", str(self.env_file),
                    "--channel", "lobby",
                    "--update-ts", "1736188888.123456",
                    "--text", "merged",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], slack_notify.UPDATE_URL)
        self.assertEqual(out.getvalue().strip(), "slack-notify: updated lobby ts=1736188888.123456")

    def test_plain_post_still_uses_post_message(self) -> None:
        urls: list[str] = []

        def fake_post(token: str, payload: dict[str, object], url: str = slack_notify.API_URL):
            urls.append(url)
            return True, "1.5"

        with mock.patch.object(slack_notify, "post", fake_post), contextlib.redirect_stdout(io.StringIO()):
            slack_notify.main(
                ["--env-file", str(self.env_file), "--channel", "lobby", "--text", "hi"]
            )
        self.assertEqual(urls, [slack_notify.API_URL])


if __name__ == "__main__":
    unittest.main()
