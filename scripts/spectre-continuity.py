#!/usr/bin/env python3
"""Continuity: resume a stalled worker only when spectre-state policy allows it."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from time import time

HERE = Path(__file__).resolve().parent
for _parent in (HERE, HERE.parent / "lib/spectre-worker-state", Path("/usr/local/lib/spectre-worker-state")):
    if (_parent / "worker_state").is_dir() and str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))
        break

from worker_state.client import StateClient
from worker_state.qoder_jsonl import load_workers, workers_path
from worker_state.view import snapshot_to_pos

BRIDGE_BIN = "spectre-slack-bridge"
REPO_BRIDGE = HERE / "slack-bridge.mjs"


def plan_actions(snapshots: dict[str, dict]) -> list[dict]:
    """Pure: snapshot.policy → resume/skip. No I/O."""
    actions = []
    for name in sorted(snapshots):
        snap = snapshots[name] if isinstance(snapshots.get(name), dict) else {}
        pos = snapshot_to_pos(snap)
        policy = pos.get("policy") or {}
        if snap.get("debug", {}).get("unavailable"):
            actions.append({"worker": name, "action": "skip", "reason": "api_unavailable"})
            continue
        if policy.get("continuity_recovery_allowed"):
            actions.append({"worker": name, "action": "resume", "reason": snap.get("reason")})
        else:
            actions.append(
                {
                    "worker": name,
                    "action": "skip",
                    "reason": "policy",
                    "state": pos.get("goal_state") or pos.get("state"),
                }
            )
    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spectre-continuity")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers-file", default=None)
    parser.add_argument("--snapshot-file", default=None)
    parser.add_argument("--bridge-bin", default=BRIDGE_BIN)
    args = parser.parse_args(argv)
    if args.snapshot_file:
        snapshots = json.loads(Path(args.snapshot_file).read_text(encoding="utf-8"))
        actions = plan_actions(snapshots)
        print(json.dumps({"actions": actions}, indent=2, sort_keys=True))
        return 0
    path = Path(args.workers_file).expanduser() if args.workers_file else workers_path()
    workers = load_workers(path)
    client = StateClient()
    snapshots = {name: client.snapshot(name, time()) for name in workers}
    actions = plan_actions(snapshots)
    if not args.dry_run:
        for item in actions:
            if item.get("action") == "resume":
                _resume(args.bridge_bin, item["worker"])
    print(json.dumps({"actions": actions}, indent=2, sort_keys=True))
    return 0


def _resume(bridge_bin: str, worker: str) -> None:
    from shutil import which

    if which(bridge_bin):
        cmd = [bridge_bin, "--dispatch", "resume", worker]
    else:
        cmd = ["node", str(REPO_BRIDGE), "--dispatch", "resume", worker]
    subprocess.run(cmd, check=False, timeout=60)


if __name__ == "__main__":
    sys.exit(main())
