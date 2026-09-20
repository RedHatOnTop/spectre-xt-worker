#!/usr/bin/env python3
"""Unattended occupancy loop. Reads snapshot.policy only. Kill switch SPECTRE_LOOP."""
from __future__ import annotations

import json
import os
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


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in args
    if not loop_enabled():
        print(json.dumps({"ok": True, "disabled": True, "dry_run": dry}))
        return 0
    workers = load_workers(workers_path())
    client = StateClient()
    prev: dict[str, Any] = {}
    if STATE_PATH.is_file():
        try:
            prev = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prev = {}
    actions = []
    for name, entry in sorted(workers.items()):
        snap = client.snapshot(name)
        actions.append(
            plan_tick(
                name,
                entry,
                snap,
                loop_on=loop_enabled() or dry,
                astra_on=astra_enabled(),
                prev=prev,
            )
        )
    print(json.dumps({"ok": True, "dry_run": dry, "actions": actions}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
