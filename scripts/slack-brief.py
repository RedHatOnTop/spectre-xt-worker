#!/usr/bin/env python3
"""Daily deterministic brief to the Slack #lobby commons — no LLM.

Composes three box-local sources into one message:
  - ~/.local/state/remote-agent/health-state.json (active failure streak)
  - the last 24 h of /work/logs/health.log (alert/recovery counts)
  - `spectre-status` output (falls back to scripts/status.sh in the repo)

Posts as the `bridge` identity through spectre-slack-notify.
Usage: spectre-slack-brief [--dry-run]
Exit codes: 0 posted/disabled, 1 error. Never prints secrets.
"""
from __future__ import annotations

import argparse
import calendar
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_FILE = Path("/work/logs/health.log")
STATE_FILE = Path.home() / ".local/state/remote-agent/health-state.json"

WINDOW_SEC = 24 * 3600
STATUS_LINES_MAX = 24
STATUS_CHARS_MAX = 1800
NOTIFY_TIMEOUT = 30
STATUS_TIMEOUT = 25


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def recent_log_lines(text: str, now: int, window: int = WINDOW_SEC) -> list[str]:
    """health.log lines whose leading Zulu timestamp is within the window."""
    out: list[str] = []
    for line in text.splitlines():
        if len(line) < 20:
            continue
        try:
            stamp = calendar.timegm(time.strptime(line[:20], "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        age = now - stamp
        if -300 <= age <= window:
            out.append(line)
    return out


def summarize_log(lines: list[str]) -> dict[str, int]:
    alerts = sum(1 for line in lines if "NOTIFY_NEW" in line or "RENOTIFY" in line)
    recoveries = sum(1 for line in lines if "NOTIFY_RECOVER" in line)
    still = sum(1 for line in lines if " STILL " in line)
    return {"alerts": alerts, "recoveries": recoveries, "still": still, "total": len(lines)}


def read_health_state(path: Path = STATE_FILE) -> dict[str, object] | None:
    raw = _read(path)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _duration(seconds: int) -> str:
    minutes_total = max(0, seconds) // 60
    hours, minutes = divmod(minutes_total, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _clip_lines(text: str, max_lines: int, max_chars: int) -> str:
    lines = text.strip().splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    clipped = "\n".join(lines)
    if len(clipped) > max_chars:
        clipped = clipped[-max_chars:]
    return clipped


def render_brief(
    now: int,
    health: dict[str, object] | None,
    log_summary: dict[str, int],
    status_text: str,
) -> str:
    date = time.strftime("%Y-%m-%d", time.localtime(now))
    lines = [f":desktop_computer: *daily brief — {date}*", ""]

    bits = health.get("bits") if isinstance(health, dict) else None
    if bits:
        since = int(health.get("since", 0))  # type: ignore[arg-type]
        duration = _duration(now - since) if since else "?"
        lines.append(f"*health*: failing {duration}: {', '.join(sorted(str(b) for b in bits))}")
    elif health is not None:
        lines.append("*health*: green (no active failures)")
    else:
        lines.append("*health*: no state file yet")

    if log_summary.get("total"):
        lines.append(
            f"*24h log*: {log_summary.get('alerts', 0)} alert(s), "
            f"{log_summary.get('recoveries', 0)} recovery(ies), "
            f"{log_summary.get('still', 0)} still-check(s)"
        )
    else:
        lines.append("*24h log*: quiet")

    if status_text.strip():
        lines += ["", "*box*", "```", _clip_lines(status_text, STATUS_LINES_MAX, STATUS_CHARS_MAX), "```"]

    lines += ["", '_reply "qoder: ..." in-thread to ask qoder about this._']
    return "\n".join(lines)


def fetch_status() -> str:
    candidates: list[list[str]] = []
    spectre = shutil.which("spectre-status")
    if spectre:
        candidates.append([spectre])
    repo_status = HERE.parent / "scripts" / "status.sh"
    if repo_status.is_file():
        candidates.append(["bash", str(repo_status)])
    for cmd in candidates:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=STATUS_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return ""


def notify_command() -> list[str]:
    binary = shutil.which("spectre-slack-notify")
    if binary:
        return [binary]
    return ["python3", str(HERE / "slack-notify.py")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="slack-brief", description="daily deterministic brief to #lobby"
    )
    parser.add_argument("--dry-run", action="store_true", help="render without posting")
    args = parser.parse_args(argv)

    now = int(time.time())
    health = read_health_state()
    summary = summarize_log(recent_log_lines(_read(LOG_FILE), now))
    status_text = fetch_status()
    brief = render_brief(now, health, summary, status_text)

    if args.dry_run:
        print(brief)
        return 0

    try:
        proc = subprocess.run(
            notify_command()
            + ["--agent", "bridge", "--channel", "lobby", "--text=" + brief],
            capture_output=True,
            text=True,
            timeout=NOTIFY_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"slack-brief: notify failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
