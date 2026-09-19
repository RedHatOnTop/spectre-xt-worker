#!/usr/bin/env python3
"""Unit tests for the deterministic Slack community brief."""
from __future__ import annotations

import importlib.util
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("slack_brief", ROOT / "scripts" / "slack-brief.py")
assert spec and spec.loader
slack_brief = importlib.util.module_from_spec(spec)
sys.modules["slack_brief"] = slack_brief
spec.loader.exec_module(slack_brief)

NOW = 1_800_000_000  # 2027-01-15T08:00:00Z


def zulu(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class RecentLogTest(unittest.TestCase):
    def test_keeps_only_lines_inside_the_window(self) -> None:
        text = "\n".join(
            [
                f"{zulu(NOW - 100)} NOTIFY_NEW proxy_down",
                f"{zulu(NOW - 100_000)} NOTIFY_RECOVER recovered",
                "garbage line without timestamp",
                f"{zulu(NOW - 10)} STILL proxy_down",
            ]
        )
        lines = slack_brief.recent_log_lines(text, NOW)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("NOTIFY_NEW proxy_down"))

    def test_handles_empty_input(self) -> None:
        self.assertEqual(slack_brief.recent_log_lines("", NOW), [])


class SummarizeTest(unittest.TestCase):
    def test_counts_by_category(self) -> None:
        lines = [
            f"{zulu(NOW - 10)} NOTIFY_NEW proxy_down",
            f"{zulu(NOW - 9)} RENOTIFY still failing 1h00m: proxy_down",
            f"{zulu(NOW - 8)} NOTIFY_RECOVER recovered",
            f"{zulu(NOW - 7)} STILL proxy_down",
            f"{zulu(NOW - 6)} STILL proxy_down",
        ]
        summary = slack_brief.summarize_log(lines)
        self.assertEqual(summary["alerts"], 2)
        self.assertEqual(summary["recoveries"], 1)
        self.assertEqual(summary["still"], 2)
        self.assertEqual(summary["total"], 5)


class RenderTest(unittest.TestCase):
    STATUS = "host spectre\nuptime up 1 day\n" + "\n".join(f"row {i}" for i in range(40))

    def test_green_state(self) -> None:
        brief = slack_brief.render_brief(NOW, {"bits": [], "since": 0}, {}, "")
        self.assertIn("*health*: green (no active failures)", brief)
        self.assertIn("*24h log*: quiet", brief)
        self.assertIn('reply "qoder: ..."', brief)

    def test_failing_state_shows_duration(self) -> None:
        health = {"bits": ["proxy_down", "ac_offline"], "since": NOW - 7500}
        brief = slack_brief.render_brief(NOW, health, {}, "")
        self.assertIn("failing 2h05m: ac_offline, proxy_down", brief)

    def test_log_counts_rendered(self) -> None:
        summary = {"alerts": 2, "recoveries": 1, "still": 3, "total": 6}
        brief = slack_brief.render_brief(NOW, None, summary, "")
        self.assertIn("2 alert(s), 1 recovery(ies), 3 still-check(s)", brief)
        self.assertIn("*health*: no state file yet", brief)

    def test_status_text_is_clipped(self) -> None:
        brief = slack_brief.render_brief(NOW, {"bits": []}, {}, self.STATUS)
        body = brief.split("```")[1]
        self.assertLessEqual(len(body.strip().splitlines()), slack_brief.STATUS_LINES_MAX)
        self.assertIn("row 39", body)  # most recent lines survive the clip

    def test_duration_formats(self) -> None:
        self.assertEqual(slack_brief._duration(150), "2m")
        self.assertEqual(slack_brief._duration(7500), "2h05m")
        self.assertEqual(slack_brief._duration(0), "0m")


if __name__ == "__main__":
    unittest.main()
