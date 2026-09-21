#!/usr/bin/env python3
"""Unattended occupancy loop. Reads snapshot.policy only. Kill switch SPECTRE_LOOP."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from worker_state.client import StateClient
from worker_state.qoder_jsonl import load_workers, workers_path

STATE_PATH = Path.home() / ".local/state/remote-agent/spectre-loop.json"
NEXT_GOAL = "next_goal.json"
DISPATCH_BIN = "spectre-slack-bridge"
NOTIFY_BIN = "spectre-slack-notify"


def loop_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    raw = str(env.get("SPECTRE_LOOP") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def astra_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    raw = str(env.get("ASTRA_ENABLED") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def has_planner(entry: dict) -> bool:
    planner = entry.get("planner")
    if isinstance(planner, dict) and planner.get("terminal"):
        return True
    return bool(entry.get("planner"))


def read_next_goal(cwd: str) -> str | None:
    path = Path(cwd) / NEXT_GOAL
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text.splitlines()[0][:600]
    if isinstance(payload, dict):
        if payload.get("blocked") is True:
            return None
        goal = str(payload.get("goal") or payload.get("next") or "").strip()
        return goal[:600] or None
    return None


def plan_tick(
    name: str,
    entry: dict,
    snap: dict[str, Any],
    *,
    loop_on: bool,
    astra_on: bool,
    prev: dict[str, Any],
) -> dict[str, Any]:
    """Pure: one worker → one action dict. No I/O."""
    if not loop_on:
        return {"worker": name, "action": "disabled"}
    if snap.get("debug", {}).get("unavailable") or snap.get("goal", {}).get("state") == "UNKNOWN":
        return {"worker": name, "action": "skip", "reason": "unknown"}
    policy = snap.get("policy") or {}
    state = str((snap.get("goal") or {}).get("state") or "")
    if policy.get("continuity_recovery_allowed"):
        return {"worker": name, "action": "skip", "reason": "continuity"}
    planner = has_planner(entry)
    if planner and not astra_on:
        return {"worker": name, "action": "skip", "reason": "planner_pin"}
    if policy.get("grokbot_may_advance") and planner and astra_on:
        return {"worker": name, "action": "plan"}
    if policy.get("grokbot_may_advance") and not planner:
        goal = read_next_goal(str(entry.get("cwd") or ""))
        ident = str((snap.get("goal") or {}).get("goal_id") or "")
        escalated = prev.get("escalated") or {}
        if not goal:
            if ident and escalated.get(ident):
                return {"worker": name, "action": "sit", "reason": "no_next_goal"}
            return {"worker": name, "action": "escalate", "reason": "no_next_goal", "ident": ident}
        return {"worker": name, "action": "goal", "text": goal, "target": "efficient"}
    if policy.get("idle_slo_violated"):
        return {"worker": name, "action": "slo", "reason": "idle_slo_violated"}
    return {"worker": name, "action": "skip", "reason": state or "ok"}


def state_path(environ: dict[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("SPECTRE_LOOP_STATE") or "").strip()
    return Path(override) if override else STATE_PATH


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _run_cmd(
    argv: list[str],
    timeout: int = 60,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    if environ:
        env.update(environ)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "error": f"missing {argv[0]}: {exc}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout running {argv[0]}"}
    out = (proc.stdout or "").strip()
    parsed: Any = None
    if out:
        try:
            parsed = json.loads(out.splitlines()[-1])
        except json.JSONDecodeError:
            parsed = None
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": out[:2000],
        "stderr": (proc.stderr or "")[:500],
        "parsed": parsed,
    }


def apply_action(
    action: dict[str, Any],
    *,
    dry: bool,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Live I/O for one plan_tick. Dry-run types nothing and does not persist."""
    env = environ if environ is not None else os.environ
    out = dict(action)
    kind = str(action.get("action") or "")
    if dry or kind in {"disabled", "skip", "sit", "slo"}:
        out["applied"] = False
        return out
    dispatch = str(env.get("SPECTRE_DISPATCH_BIN") or DISPATCH_BIN)
    notify = str(env.get("SPECTRE_NOTIFY_BIN") or NOTIFY_BIN)
    if kind == "goal":
        result = _run_cmd(
            [
                dispatch,
                "--dispatch",
                "goal",
                str(action.get("worker") or ""),
                str(action.get("text") or ""),
                "--target",
                str(action.get("target") or "efficient"),
            ],
            environ=env,
        )
        out["applied"] = bool(result.get("ok"))
        out["io"] = result
        return out
    if kind == "plan":
        result = _run_cmd(
            [
                dispatch,
                "--dispatch",
                "plan",
                str(action.get("worker") or ""),
                str(action.get("text") or "plan the next packets"),
            ],
            environ=env,
        )
        out["applied"] = bool(result.get("ok"))
        out["io"] = result
        return out
    if kind == "escalate":
        worker = str(action.get("worker") or "")
        ident = str(action.get("ident") or "")
        result = _run_cmd(
            [
                notify,
                "--agent",
                "loop",
                "--channel",
                "lobby",
                "--text",
                f"{worker}: no next_goal.json — escalating once then sitting ({ident})",
            ],
            timeout=20,
            environ=env,
        )
        out["applied"] = True
        out["io"] = result
        return out
    out["applied"] = False
    return out


def persist_from_actions(prev: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    escalated = dict(prev.get("escalated") or {})
    for action in actions:
        if action.get("action") == "escalate" and action.get("applied"):
            ident = str(action.get("ident") or "")
            if ident:
                escalated[ident] = True
    out = dict(prev)
    out["escalated"] = escalated
    return out


def _flag_value(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    idx = args.index(name)
    if idx + 1 >= len(args):
        return None
    return args[idx + 1]


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    dry = "--dry-run" in args
    environ = dict(os.environ)
    if not loop_enabled(environ):
        print(json.dumps({"ok": True, "disabled": True, "dry_run": dry}))
        return 0
    snap_file = _flag_value(args, "--snapshot-file")
    workers_override = _flag_value(args, "--workers-file")
    path = state_path(environ)
    prev = load_state(path)
    if snap_file is not None:
        snapshots = json.loads(Path(snap_file).read_text(encoding="utf-8"))
        workers = load_workers(Path(workers_override)) if workers_override else {name: {} for name in snapshots}
        planned = [
            plan_tick(
                name,
                workers.get(name) or {},
                snapshots[name],
                loop_on=True,
                astra_on=astra_enabled(environ),
                prev=prev,
            )
            for name in sorted(snapshots)
        ]
    else:
        workers = load_workers(Path(workers_override) if workers_override else workers_path())
        client = StateClient()
        planned = []
        for name, entry in sorted(workers.items()):
            snap = client.snapshot(name)
            planned.append(
                plan_tick(
                    name,
                    entry,
                    snap,
                    loop_on=True,
                    astra_on=astra_enabled(environ),
                    prev=prev,
                )
            )
    actions = [apply_action(item, dry=dry, environ=environ) for item in planned]
    if not dry:
        save_state(path, persist_from_actions(prev, actions))
    print(json.dumps({"ok": True, "dry_run": dry, "actions": actions}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
