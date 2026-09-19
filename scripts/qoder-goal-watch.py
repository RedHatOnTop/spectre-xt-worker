#!/usr/bin/env python3
"""Notify Slack when spectre-worker-state reports a park or recovery.

Occupancy comes from spectre-state (snapshot.policy / goal.state). This
script does not classify session jsonl. Dedup key = park_reason + turn_id.

Stdlib + the worker_state package. Never prints token values; posting goes
through spectre-slack-notify, which reads slack.env itself.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _parent in (HERE, Path("/usr/local/lib/spectre-worker-state")):
    if (_parent / "worker_state").is_dir() and str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))
        break

from worker_state.client import StateClient  # noqa: E402
from worker_state.qoder_jsonl import (  # noqa: E402
    SESSIONS_DIR as JSONL_SESSIONS,
    load_workers as jsonl_load_workers,
    newest_segment,
    session_dir_name,
    tail_records,
    workers_path as jsonl_workers_path,
)
from worker_state.store import Store  # noqa: E402
from worker_state.view import snapshot_to_pos  # noqa: E402

SESSIONS_DIR = JSONL_SESSIONS
BOX_WORKERS_FILE = Path("/usr/local/share/remote-agent/qoder-workers.json")
REPO_WORKERS_FILE = Path(__file__).resolve().parents[1] / "config" / "qoder-workers.json"
STATE_FILE = Path.home() / ".local/state/remote-agent/qoder-goal-watch.json"
LOG_FILE = Path("/work/logs/qoder-goal-watch.log")
NOTIFY_BIN = "/usr/local/bin/spectre-slack-notify"
REPO_NOTIFY = Path(__file__).resolve().parents[1] / "scripts" / "slack-notify.py"
TAIL_BYTES = 256 * 1024
STATE_KEEP = 200


def workers_path() -> Path:
    return jsonl_workers_path()


def load_workers(path: Path) -> dict[str, dict]:
    try:
        workers = jsonl_load_workers(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"qoder-goal-watch: workers file unreadable: {path}: {exc}") from exc
    for name, entry in workers.items():
        if not isinstance(entry, dict) or not str(entry.get("cwd") or "").strip():
            raise SystemExit(f"qoder-goal-watch: worker {name!r} has no cwd in {path}")
    return workers


def _snapshot_now(store: Store | None, client: StateClient | None, name: str, now: float) -> dict:
    if store is not None:
        return store.snapshot(name, now)
    assert client is not None
    return client.snapshot(name, now)


def collect_positions(
    workers: dict[str, dict],
    sessions_root: Path,
    now: float,
    store: Store | None = None,
    client: StateClient | None = None,
) -> dict[str, dict]:
    del sessions_root
    if store is None and client is None:
        client = StateClient()
    out: dict[str, dict] = {}
    for name, entry in workers.items():
        snap = _snapshot_now(store, client, name, now)
        pos = snapshot_to_pos(snap)
        pos["tmux"] = bool(entry.get("tmux"))
        if snap.get("debug", {}).get("unavailable"):
            pos["state"] = "unknown"
            pos["reason"] = str(snap.get("reason") or "state api unavailable")
        out[name] = pos
    return out


def park_key(pos: dict) -> str | None:
    """Dedup key for a park event; None when the worker is not parked."""
    if pos.get("state") != "parked":
        return None
    ident = pos.get("turn_id") or pos.get("ts") or ""
    return f"{pos.get('reason')}:{ident}"


def plan_actions(
    positions: dict[str, dict], prev: dict[str, dict]
) -> tuple[list[dict], dict[str, dict]]:
    """Slack actions for the observed positions vs the last scan. Pure.

    Notifies on park entry (new key) and on park exit (recovery). A worker
    that stays parked, or that we never saw parked, is silent."""
    actions: list[dict] = []
    next_state: dict[str, dict] = {}
    for name in sorted(positions):
        pos = positions[name]
        entry = prev.get(name) if isinstance(prev.get(name), dict) else {}
        key = park_key(pos)
        had_key = entry.get("key") or None
        if key is not None:
            if key != had_key:
                actions.append({"action": "notify_park", "worker": name, **pos})
            next_state[name] = {"state": "parked", "key": key, "ts": pos.get("ts", "")}
        else:
            if had_key is not None:
                actions.append(
                    {
                        "action": "notify_recovery",
                        "worker": name,
                        "state": str(pos.get("state") or "unknown"),
                        "ts": pos.get("ts", ""),
                    }
                )
            next_state[name] = {
                "state": str(pos.get("state") or "unknown"),
                "key": None,
                "ts": pos.get("ts", ""),
            }
    return actions, next_state


def human_age(age: float | None) -> str:
    if age is None:
        return "age unknown"
    if age < 120:
        return f"{int(age)} s"
    if age < 7200:
        return f"{int(age / 60)} min"
    return f"{age / 3600:.1f} h"


def format_park(action: dict) -> str:
    worker = action["worker"]
    where = f"`{worker}`"
    # Both injection paths (tmux pane, orca terminal) are reachable from
    # #control; a missing/ambiguous orca terminal is refused there with a
    # pointer to the UI, so the hint is the same for either worker kind.
    hint = f"Resume: `resume {worker}` in #control, or `/goal resume` in the TUI."
    if action.get("reason") == "goal_budget":
        turns = action.get("num_turns")
        turns_txt = f"{turns} iterations" if turns else "turn limit"
        return (
            f":octagonal_sign: {where} goal auto-paused — turn budget exhausted "
            f"({turns_txt}) at {action.get('ts')} ({human_age(action.get('age_sec'))} ago). "
            f"The session is alive and will not continue by itself. {hint}"
        )
    return (
        f":octagonal_sign: {where} is blocked on a plan approval (ExitPlanMode) "
        f"since {action.get('ts')} ({human_age(action.get('age_sec'))} ago). "
        f"Nothing clears this but an answer in the TUI — `resume {worker}` will not help."
    )


def format_recovery(action: dict) -> str:
    return (
        f"{action['worker']} is moving again "
        f"(last record {action.get('state')} at {action.get('ts')})."
    )


def log_line(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().astimezone().isoformat()} {text}\n")
    except OSError:
        pass


def notify_command() -> list[str]:
    if Path(NOTIFY_BIN).is_file():
        return [NOTIFY_BIN]
    return [sys.executable, str(REPO_NOTIFY)]


def post_notification(text: str, agent: str, channel: str, recovery: bool) -> tuple[bool, str]:
    """(delivered, detail). 'disabled' (no slack.env yet) is not a failure but
    must not consume the dedup state — the notice has to fire once configured."""
    cmd = notify_command() + ["--agent", agent, "--channel", channel, "--text", text]
    if recovery:
        cmd.append("--recovery")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"spawn: {exc}"
    if "disabled" in proc.stdout:
        return False, "disabled"
    if proc.returncode != 0:
        return False, f"exit={proc.returncode} {proc.stderr.strip()[:200]}"
    return True, proc.stdout.strip()


def load_state(path: Path) -> dict[str, dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    workers = payload.get("workers")
    return workers if isinstance(workers, dict) else {}


def save_state(path: Path, workers: dict[str, dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        trimmed = dict(list(workers.items())[-STATE_KEEP:])
        path.write_text(
            json.dumps({"workers": trimmed}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log_line(LOG_FILE, f"state_write_failed {exc}")


def cmd_probe(
    name: str,
    workers: dict[str, dict],
    sessions_root: Path,
    store: Store | None,
    client: StateClient | None,
) -> int:
    entry = workers.get(name)
    if entry is None:
        payload = {"worker": name, "state": "unknown", "error": "unknown worker"}
        print(json.dumps(payload))
        return 1
    pos = collect_positions(
        {name: entry}, sessions_root, datetime.now().timestamp(), store=store, client=client
    )[name]
    print(json.dumps({"worker": name, **pos}, sort_keys=True))
    return 0 if pos.get("state") != "unknown" else 1


def cmd_scan(
    workers: dict[str, dict],
    sessions_root: Path,
    state_file: Path,
    agent: str,
    channel: str,
    dry_run: bool,
    now: float,
    store: Store | None = None,
    client: StateClient | None = None,
) -> int:
    positions = collect_positions(workers, sessions_root, now, store=store, client=client)
    prev = load_state(state_file)
    actions, next_state = plan_actions(positions, prev)
    if dry_run:
        print(json.dumps({"positions": positions, "actions": actions}, indent=2, sort_keys=True))
        return 0

    delivered_any = False
    for action in actions:
        recovery = action["action"] == "notify_recovery"
        text = format_recovery(action) if recovery else format_park(action)
        delivered, detail = post_notification(text, agent, channel, recovery)
        log_line(
            LOG_FILE,
            f"{action['action']} worker={action['worker']} "
            f"detail={action.get('reason') or action.get('state')} "
            f"delivered={delivered} ({detail})",
        )
        delivered_any = delivered or delivered_any
        if not delivered:
            # Keep the previous entry so the next tick retries (and so a
            # disabled notify path does not silently swallow the event).
            if action["worker"] in prev:
                next_state[action["worker"]] = prev[action["worker"]]
            else:
                next_state.pop(action["worker"], None)
    save_state(state_file, next_state)
    if delivered_any:
        print("qoder-goal-watch: notifications posted")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qoder-goal-watch",
        description="report qodercli goal parks (turn budget, plan gate) to Slack",
    )
    parser.add_argument("--scan", action="store_true", help="scan all workers (default)")
    parser.add_argument("--probe", metavar="WORKER", help="classify one worker and exit")
    parser.add_argument("--json", action="store_true", help="with --probe: JSON output")
    parser.add_argument("--dry-run", action="store_true", help="print, post nothing")
    parser.add_argument("--agent", default="qoder", help="Slack identity for notices")
    parser.add_argument("--channel", default="fleet", help="Slack channel alias")
    parser.add_argument("--sessions-dir", default=str(SESSIONS_DIR))
    parser.add_argument("--workers-file", default=None)
    parser.add_argument("--state-file", default=str(STATE_FILE))
    parser.add_argument("--db", default=None, help="sqlite path; default is the daemon's DB via the API")
    args = parser.parse_args(argv)

    workers_file = Path(args.workers_file).expanduser() if args.workers_file else workers_path()
    workers = load_workers(workers_file)
    sessions_root = Path(args.sessions_dir).expanduser()
    store = Store(args.db) if args.db else None
    client = None if store else StateClient()

    if args.probe:
        return cmd_probe(
            str(args.probe).strip().lower(), workers, sessions_root, store, client
        )
    return cmd_scan(
        workers,
        sessions_root,
        Path(args.state_file).expanduser(),
        args.agent,
        args.channel,
        args.dry_run,
        datetime.now().timestamp(),
        store=store,
        client=client,
    )


if __name__ == "__main__":
    sys.exit(main())
