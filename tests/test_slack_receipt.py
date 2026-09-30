#!/usr/bin/env python3
"""Unit tests for the #lobby pull-request ledger."""
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

spec = importlib.util.spec_from_file_location("slack_receipt", ROOT / "scripts" / "slack-receipt.py")
assert spec and spec.loader
slack_receipt = importlib.util.module_from_spec(spec)
sys.modules["slack_receipt"] = slack_receipt
spec.loader.exec_module(slack_receipt)

NOW = 1_800_000_000
PR = "https://github.com/RedHatOnTop/minecraft-server-project/pull/321"
OTHER = "https://github.com/RedHatOnTop/spectre-xt-worker/pull/7"


def done(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class Fake:
    """Stands in for slack_receipt._run: notify answers come from `notify`, gh from `gh`."""

    def __init__(self, notify=None, gh=None) -> None:
        self.notify = list(notify or [])
        self.gh = dict(gh or {})
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: float):
        self.calls.append(cmd)
        if cmd[0] == "gh":
            return self.gh.get(cmd[3])
        return self.notify.pop(0) if self.notify else done("slack-notify: posted lobby ts=9.9\n")

    def notified(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "notify"]


class RenderTest(unittest.TestCase):
    def test_line_per_fact_and_no_note_line_when_empty(self) -> None:
        entry = {"title": "feat(village): chips", "state": "open", "note": ""}
        self.assertEqual(
            slack_receipt.render(PR, entry),
            f":large_yellow_circle: *open* · minecraft-server-project#321 — feat(village): chips\n{PR}",
        )

    def test_note_is_the_last_line(self) -> None:
        entry = {"title": "t", "state": "failing", "note": "CI: 1 red"}
        text = slack_receipt.render(PR, entry)
        self.assertTrue(text.startswith(":large_red_circle: *failing*"))
        self.assertEqual(text.splitlines()[-1], "CI: 1 red")

    def test_every_state_has_an_emoji(self) -> None:
        self.assertEqual(set(slack_receipt.EMOJI), set(slack_receipt.STATES))


class UpsertTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(slack_receipt, "notify_command", return_value=["notify"])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.receipts: dict[str, dict[str, object]] = {}

    def upsert(self, fake: Fake, url: str = PR, **kw):
        args = {"title": None, "state": None, "note": None, "agent": "claude", "now": NOW}
        args.update(kw)
        with mock.patch.object(slack_receipt, "_run", fake):
            return slack_receipt.upsert(
                self.receipts, url, args["title"], args["state"], args["note"], args["agent"], args["now"]
            )

    def test_first_call_posts_and_remembers_the_ts(self) -> None:
        fake = Fake([done("slack-notify: posted lobby ts=1736188888.123456\n")])
        ok, detail = self.upsert(fake, title="chips", note="CI green")
        self.assertEqual((ok, detail), (True, "posted"))
        self.assertEqual(self.receipts[PR]["ts"], "1736188888.123456")
        self.assertEqual(self.receipts[PR]["state"], "open")
        cmd = fake.notified()[0]
        self.assertIn("--agent", cmd)
        self.assertEqual(cmd[cmd.index("--channel") + 1], "lobby")
        self.assertNotIn("--update-ts", cmd)

    def test_second_call_edits_in_place_and_keeps_what_it_omits(self) -> None:
        fake = Fake([done("slack-notify: posted lobby ts=1.5\n"), done("slack-notify: updated lobby ts=1.5\n")])
        self.upsert(fake, title="chips", note="CI green")
        ok, detail = self.upsert(fake, state="merged", now=NOW + 60)
        self.assertEqual((ok, detail), (True, "updated"))
        cmd = fake.notified()[1]
        self.assertEqual(cmd[cmd.index("--update-ts") + 1], "1.5")
        text = next(a for a in cmd if a.startswith("--text=")).removeprefix("--text=")
        self.assertIn("*merged*", text)
        self.assertIn("chips", text)
        self.assertIn("CI green", text)
        self.assertEqual(self.receipts[PR]["opened"], NOW)
        self.assertEqual(self.receipts[PR]["updated"], NOW + 60)

    def test_deleted_message_is_replaced_by_a_fresh_post(self) -> None:
        fake = Fake(
            [
                done("slack-notify: posted lobby ts=1.5\n"),
                done(stderr="slack-notify: error=message_not_found\n", rc=1),
                done("slack-notify: posted lobby ts=2.5\n"),
            ]
        )
        self.upsert(fake, title="chips")
        ok, detail = self.upsert(fake, state="failing")
        self.assertEqual((ok, detail), (True, "posted"))
        self.assertEqual(self.receipts[PR]["ts"], "2.5")

    def test_other_update_failures_leave_the_receipt_untouched(self) -> None:
        fake = Fake(
            [done("slack-notify: posted lobby ts=1.5\n"), done(stderr="slack-notify: network\n", rc=1)]
        )
        self.upsert(fake, title="chips")
        before = json.dumps(self.receipts, sort_keys=True)
        ok, detail = self.upsert(fake, state="merged")
        self.assertFalse(ok)
        self.assertEqual(detail, "slack-notify: network")
        self.assertEqual(json.dumps(self.receipts, sort_keys=True), before)
        self.assertEqual(len(fake.notified()), 2)

    def test_disabled_slack_records_the_receipt_without_a_ts(self) -> None:
        fake = Fake([done("slack-notify: disabled\n")])
        ok, detail = self.upsert(fake, title="chips")
        self.assertEqual((ok, detail), (True, "disabled"))
        self.assertNotIn("ts", self.receipts[PR])

    def test_title_is_required_the_first_time(self) -> None:
        fake = Fake()
        ok, detail = self.upsert(fake)
        self.assertFalse(ok)
        self.assertIn("--title", detail)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.receipts, {})

    def test_note_can_be_cleared_with_an_empty_string(self) -> None:
        fake = Fake()
        self.upsert(fake, title="chips", note="CI green")
        self.upsert(fake, note="")
        self.assertEqual(self.receipts[PR]["note"], "")

    def test_unparseable_notifier_output_is_an_error(self) -> None:
        ok, detail = self.upsert(Fake([done("something unexpected\n")]), title="chips")
        self.assertFalse(ok)
        self.assertIn("no ts", detail)


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        for target, kwargs in (
            (slack_receipt, {"notify_command": mock.Mock(return_value=["notify"])}),
            (slack_receipt.shutil, {"which": mock.Mock(return_value="/usr/bin/gh")}),
        ):
            for name, value in kwargs.items():
                patcher = mock.patch.object(target, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.receipts: dict[str, dict[str, object]] = {
            PR: {"title": "chips", "state": "open", "note": "", "ts": "1.5", "opened": NOW, "updated": NOW},
            OTHER: {"title": "ledger", "state": "failing", "note": "", "ts": "2.5", "opened": NOW, "updated": NOW},
            "https://github.com/o/r/pull/1": {
                "title": "old", "state": "merged", "note": "", "ts": "3.5", "opened": NOW, "updated": NOW,
            },
        }

    def run_sync(self, gh):
        fake = Fake(gh=gh)
        with mock.patch.object(slack_receipt, "_run", fake):
            return slack_receipt.sync(self.receipts, "claude", NOW + 100), fake

    def test_merged_and_closed_are_settled_and_edited(self) -> None:
        result, fake = self.run_sync({PR: done("MERGED\n"), OTHER: done("CLOSED\n")})
        self.assertEqual(result, (2, 0, 0))
        self.assertEqual(self.receipts[PR]["state"], "merged")
        self.assertEqual(self.receipts[OTHER]["state"], "closed")
        self.assertEqual(len(fake.notified()), 2)
        self.assertTrue(all("--update-ts" in c for c in fake.notified()))

    def test_open_pull_requests_are_left_alone(self) -> None:
        result, fake = self.run_sync({PR: done("OPEN\n"), OTHER: done("OPEN\n")})
        self.assertEqual(result, (0, 0, 0))
        self.assertEqual(fake.notified(), [])

    def test_settled_receipts_are_never_asked_about_again(self) -> None:
        _, fake = self.run_sync({PR: done("OPEN\n"), OTHER: done("OPEN\n")})
        asked = [c[3] for c in fake.calls if c[0] == "gh"]
        self.assertEqual(sorted(asked), sorted([PR, OTHER]))

    def test_gh_trouble_is_skipped_not_failed(self) -> None:
        result, _ = self.run_sync({PR: done(stderr="boom", rc=1), OTHER: None})
        self.assertEqual(result, (0, 2, 0))
        self.assertEqual(self.receipts[PR]["state"], "open")

    def test_missing_gh_skips_every_waiting_receipt(self) -> None:
        with mock.patch.object(slack_receipt.shutil, "which", return_value=None):
            result, fake = self.run_sync({})
        self.assertEqual(result, (0, 2, 0))
        self.assertEqual(fake.calls, [])

    def test_a_failed_slack_edit_counts_as_failed_and_keeps_the_state(self) -> None:
        fake = Fake(notify=[done(stderr="slack-notify: network\n", rc=1)], gh={PR: done("MERGED\n"), OTHER: done("OPEN\n")})
        with mock.patch.object(slack_receipt, "_run", fake), contextlib.redirect_stderr(io.StringIO()):
            result = slack_receipt.sync(self.receipts, "claude", NOW + 100)
        self.assertEqual(result, (0, 0, 1))
        self.assertEqual(self.receipts[PR]["state"], "open")


class PendingAndPruneTest(unittest.TestCase):
    def test_pending_lists_waiting_receipts_oldest_first(self) -> None:
        receipts = {
            OTHER: {"title": "ledger", "state": "failing", "opened": NOW - 90_000},
            PR: {"title": "chips", "state": "open", "opened": NOW - 7_300},
            "https://github.com/o/r/pull/1": {"title": "done", "state": "merged", "opened": NOW - 500_000},
        }
        self.assertEqual(
            slack_receipt.pending_lines(receipts, NOW),
            [
                f"• spectre-xt-worker#7 ledger (1d, checks failing) {OTHER}",
                f"• minecraft-server-project#321 chips (2h) {PR}",
            ],
        )

    def test_nothing_waiting_is_an_empty_list(self) -> None:
        self.assertEqual(slack_receipt.pending_lines({}, NOW), [])

    def test_age_text_units(self) -> None:
        self.assertEqual([slack_receipt.age_text(s) for s in (59, 600, 7200, 200_000)], ["0m", "10m", "2h", "2d"])

    def test_prune_drops_only_old_settled_receipts(self) -> None:
        old = NOW - slack_receipt.KEEP_SETTLED_SEC - 1
        receipts = {
            "a": {"state": "merged", "updated": old},
            "b": {"state": "merged", "updated": NOW},
            "c": {"state": "open", "updated": old},
        }
        slack_receipt.prune(receipts, NOW)
        self.assertEqual(sorted(receipts), ["b", "c"])


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "receipts.json"
        patcher = mock.patch.object(slack_receipt, "notify_command", return_value=["notify"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, fake: Fake, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(slack_receipt, "_run", fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                rc = slack_receipt.main(["--state-file", str(self.state), *args])
            except SystemExit as exc:
                rc = int(exc.code)
        return rc, out.getvalue(), err.getvalue()

    def test_post_persists_and_pending_reads_it_back(self) -> None:
        rc, out, _ = self.main(Fake(), "--pr", PR, "--title", "chips", "--note", "CI green")
        self.assertEqual(rc, 0)
        self.assertIn("posted minecraft-server-project#321", out)
        saved = json.loads(self.state.read_text(encoding="utf-8"))["receipts"][PR]
        self.assertEqual((saved["title"], saved["state"], saved["note"], saved["ts"]), ("chips", "open", "CI green", "9.9"))
        rc, out, _ = self.main(Fake(), "--pending")
        self.assertEqual(rc, 0)
        self.assertIn("chips", out)

    def test_dry_run_prints_the_message_and_touches_nothing(self) -> None:
        fake = Fake()
        rc, out, _ = self.main(fake, "--dry-run", "--pr", PR, "--title", "chips")
        self.assertEqual(rc, 0)
        self.assertIn("*open*", out)
        self.assertEqual(fake.calls, [])
        self.assertFalse(self.state.exists())

    def test_a_non_pull_request_url_is_rejected(self) -> None:
        for bad in ("https://github.com/o/r/issues/3", "not a url", "https://github.com/o/r/pull/x"):
            rc, _, err = self.main(Fake(), "--pr", bad, "--title", "t")
            self.assertEqual(rc, 2, bad)
            self.assertIn("--pr", err)

    def test_no_action_is_an_argument_error(self) -> None:
        rc, _, err = self.main(Fake())
        self.assertEqual(rc, 2)
        self.assertIn("--pr, --sync or --pending", err)

    def test_failed_post_exits_1_and_saves_nothing(self) -> None:
        rc, _, err = self.main(Fake([done(stderr="slack-notify: network\n", rc=1)]), "--pr", PR, "--title", "t")
        self.assertEqual(rc, 1)
        self.assertIn("network", err)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["receipts"], {})

    def test_sync_exit_code_ignores_skips(self) -> None:
        self.main(Fake(), "--pr", PR, "--title", "t")
        with mock.patch.object(slack_receipt.shutil, "which", return_value="/usr/bin/gh"):
            rc, out, _ = self.main(Fake(gh={PR: done(stderr="offline", rc=1)}), "--sync")
        self.assertEqual(rc, 0)
        self.assertIn("0 settled, 1 skipped, 0 failed", out)


if __name__ == "__main__":
    unittest.main()
