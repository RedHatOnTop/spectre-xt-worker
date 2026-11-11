#!/usr/bin/env python3
"""One Slack message per pull request in #lobby — the operator's merge ledger.

A session opens the PR, the operator merges it. This keeps a single message per
PR that says where it stands and is edited in place as it moves, so the channel
answers "what is waiting for me" instead of scrolling as a feed. #lobby is not
answered by the bridge while SLACK_LOBBY_DISCUSSION=0.

Usage:
  spectre-slack-receipt --pr URL --title T [--state open|failing|merged|closed] [--note TEXT]
  spectre-slack-receipt --sync               refresh waiting receipts from GitHub (gh)
  spectre-slack-receipt --pending            print what still waits, one line each
  spectre-slack-receipt --dry-run --pr URL --title T

The first call for a PR posts; later calls for the same URL edit that message and
keep whatever title/state/note they leave out. Posts go through
spectre-slack-notify as the `claude` identity. Unconfigured Slack is a no-op.
Exit codes: 0 posted/updated/disabled, 1 error, 2 bad arguments. Never prints secrets.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = Path.home() / ".local/state/remote-agent/slack-receipts.json"

NOTIFY_TIMEOUT = 30
GH_TIMEOUT = 20
KEEP_SETTLED_SEC = 14 * 24 * 3600

PR_URL = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)$")
NOTIFY_TS = re.compile(r"\bts=(\d{1,20}\.\d{1,10})")

STATES = ("open", "failing", "merged", "closed")
WAITING = ("open", "failing")
EMOJI = {
    "open": ":large_yellow_circle:",
    "failing": ":large_red_circle:",
    "merged": ":large_purple_circle:",
    "closed": ":black_circle:",
}
GITHUB_STATE = {"MERGED": "merged", "CLOSED": "closed"}


def pr_label(url: str) -> str:
    match = PR_URL.match(url)
    return f"{match.group(2)}#{match.group(3)}" if match else url


def age_text(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def render(url: str, entry: dict[str, object]) -> str:
    lines = [
        f"{EMOJI[str(entry['state'])]} *{entry['state']}* · {pr_label(url)} — {entry['title']}",
        url,
    ]
    if entry.get("note"):
        lines.append(str(entry["note"]))
    return "\n".join(lines)


def pending_lines(receipts: dict[str, dict[str, object]], now: int) -> list[str]:
    waiting = sorted(
        (int(entry.get("opened", now)), url, entry)  # type: ignore[call-overload]
        for url, entry in receipts.items()
        if entry.get("state") in WAITING
    )
    lines = []
    for opened, url, entry in waiting:
        flag = ", checks failing" if entry["state"] == "failing" else ""
        lines.append(f"• {pr_label(url)} {entry['title']} ({age_text(now - opened)}{flag}) {url}")
    return lines


def load_receipts(path: Path) -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    receipts = payload.get("receipts") if isinstance(payload, dict) else None
    return receipts if isinstance(receipts, dict) else {}


def save_receipts(path: Path, receipts: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"receipts": receipts}, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def prune(receipts: dict[str, dict[str, object]], now: int) -> None:
    for url in [
        url
        for url, entry in receipts.items()
        if entry.get("state") not in WAITING
        and now - int(entry.get("updated", now)) > KEEP_SETTLED_SEC  # type: ignore[call-overload]
    ]:
        del receipts[url]


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def notify_command() -> list[str]:
    binary = shutil.which("spectre-slack-notify")
    if binary:
        return [binary]
    return ["python3", str(HERE / "slack-notify.py")]


def post_or_update(agent: str, text: str, ts: str | None) -> tuple[bool, str | None, str]:
    """(ok, ts, detail). ts is None when Slack is not configured."""
    base = notify_command() + ["--agent", agent, "--channel", "lobby", "--text=" + text]
    if ts:
        proc = _run(base + ["--update-ts", ts], NOTIFY_TIMEOUT)
        if proc is not None and proc.returncode == 0:
            return True, ts, "updated"
        if proc is None or "message_not_found" not in proc.stderr:
            return False, ts, proc.stderr.strip() if proc else "notify did not run"
    proc = _run(base, NOTIFY_TIMEOUT)
    if proc is None:
        return False, None, "notify did not run"
    if proc.returncode != 0:
        return False, None, proc.stderr.strip() or "notify failed"
    if "slack-notify: disabled" in proc.stdout:
        return True, None, "disabled"
    match = NOTIFY_TS.search(proc.stdout)
    if not match:
        return False, None, f"no ts in notifier output: {proc.stdout.strip()}"
    return True, match.group(1), "posted"


def upsert(
    receipts: dict[str, dict[str, object]],
    url: str,
    title: str | None,
    state: str | None,
    note: str | None,
    agent: str,
    now: int,
) -> tuple[bool, str]:
    previous = receipts.get(url, {})
    entry: dict[str, object] = {
        **previous,
        "title": title or previous.get("title") or "",
        "state": state or previous.get("state") or "open",
        "note": note if note is not None else previous.get("note", ""),
        "opened": previous.get("opened", now),
        "updated": now,
    }
    if not entry["title"]:
        return False, "--title is required the first time a PR is posted"
    ok, ts, detail = post_or_update(agent, render(url, entry), previous.get("ts"))  # type: ignore[arg-type]
    if not ok:
        return False, detail
    if ts:
        entry["ts"] = ts
    else:
        entry.pop("ts", None)
    receipts[url] = entry
    return True, detail


def sync(receipts: dict[str, dict[str, object]], agent: str, now: int) -> tuple[int, int, int]:
    """(settled, skipped, failed) over waiting receipts. A PR GitHub reports merged or
    closed is settled; one gh cannot answer for is skipped and retried next run."""
    settled = skipped = failed = 0
    have_gh = shutil.which("gh") is not None
    for url, entry in list(receipts.items()):
        if entry.get("state") not in WAITING:
            continue
        proc = (
            _run(["gh", "pr", "view", url, "--json", "state", "-q", ".state"], GH_TIMEOUT)
            if have_gh
            else None
        )
        if proc is None or proc.returncode != 0:
            skipped += 1
            continue
        state = GITHUB_STATE.get(proc.stdout.strip().upper())
        if not state:
            continue
        ok, detail = upsert(receipts, url, None, state, None, agent, now)
        if ok:
            settled += 1
        else:
            failed += 1
            print(f"slack-receipt: {pr_label(url)}: {detail}", file=sys.stderr)
    return settled, skipped, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="slack-receipt", description="one #lobby message per pull request"
    )
    parser.add_argument("--pr", help="pull request URL (https://github.com/<owner>/<repo>/pull/<n>)")
    parser.add_argument("--title")
    parser.add_argument("--state", choices=STATES)
    parser.add_argument("--note", help="one line of evidence, e.g. the CI result")
    parser.add_argument("--agent", default="claude", help="identity from the registry")
    parser.add_argument("--sync", action="store_true", help="settle receipts whose PR is merged/closed")
    parser.add_argument("--pending", action="store_true", help="print waiting receipts")
    parser.add_argument("--dry-run", action="store_true", help="print the message, post nothing")
    parser.add_argument("--state-file", default=str(STATE_FILE))
    args = parser.parse_args(argv)

    if not (args.pr or args.sync or args.pending):
        parser.error("give --pr, --sync or --pending")
    if args.pr and not PR_URL.match(args.pr):
        parser.error(f"--pr is not a GitHub pull request URL: {args.pr}")

    path = Path(args.state_file).expanduser()
    now = int(time.time())
    status = 0

    with locked(path):
        receipts = load_receipts(path)

        if args.pr and args.dry_run:
            previous = receipts.get(args.pr, {})
            entry = {
                "title": args.title or previous.get("title", ""),
                "state": args.state or previous.get("state", "open"),
                "note": args.note if args.note is not None else previous.get("note", ""),
            }
            print(render(args.pr, entry))
            return 0

        if args.pr:
            ok, detail = upsert(receipts, args.pr, args.title, args.state, args.note, args.agent, now)
            if ok:
                print(f"slack-receipt: {detail} {pr_label(args.pr)}")
            else:
                print(f"slack-receipt: {detail}", file=sys.stderr)
                status = 1

        if args.sync:
            settled, skipped, failed = sync(receipts, args.agent, now)
            print(f"slack-receipt: synced, {settled} settled, {skipped} skipped, {failed} failed")
            if failed:
                status = 1

        if args.pr or args.sync:
            prune(receipts, now)
            save_receipts(path, receipts)

    if args.pending:
        for line in pending_lines(receipts, now):
            print(line)
    return status


if __name__ == "__main__":
    sys.exit(main())
