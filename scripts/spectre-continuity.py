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
for _parent in (HERE, Path("/usr/local/lib/spectre-worker-state")):
    if (_parent / "worker_state").is_dir() and str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))
        break

from worker_state.client import StateClient
from worker_state.qoder_jsonl import load_workers, workers_path
from worker_state.view import snapshot_to_pos

BRIDGE_BIN = "spectre-slack-bridge"
REPO_BRIDGE = HERE / "slack-bridge.mjs"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spectre-continuity")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers-file", default=None)
    parser.add_argument("--bridge-bin", default=BRIDGE_BIN)
    args = parser.parse_args(argv)
    path = Path(args.workers_file).expanduser() if args.workers_file else workers_path()
    workers = load_workers(path)
    client = StateClient()
    actions = []
    for name in sorted(workers):
        snap = client.snapshot(name, time())
        pos = snapshot_to_pos(snap)
        policy = pos.get("policy") or {}
        if snap.get("debug", {}).get("unavailable"):
            actions.append({"worker": name, "action": "skip", "reason": "api_unavailable"})
            continue
        if policy.get("continuity_recovery_allowed"):
            actions.append({"worker": name, "action": "resume", "reason": snap.get("reason")})
            if not args.dry_run:
                _resume(args.bridge_bin, name)
        else:
            actions.append(
                {
                    "worker": name,
                    "action": "skip",
                    "reason": "policy",
                    "state": pos.get("goal_state") or pos.get("state"),
                }
            )
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
