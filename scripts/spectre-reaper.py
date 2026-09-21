#!/usr/bin/env python3
"""SSOT + listen-table reaper. Default dry-run."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ALLOW_PORTS = {6768, 7676, 9222, 9091}
NEVER_KILL = ("sshd", "tailscaled", "orca-ide", "spectre-state", "spectre-slack-bridge")


def pin_set(workers: dict[str, dict], cwd: str) -> set[str]:
    pins: set[str] = set()
    for entry in workers.values():
        if str(entry.get("cwd") or "") != cwd:
            continue
        if entry.get("terminal"):
            pins.add(str(entry["terminal"]))
        planner = entry.get("planner") or {}
        if isinstance(planner, dict) and planner.get("terminal"):
            pins.add(str(planner["terminal"]))
        targets = entry.get("targets") or {}
        if isinstance(targets, dict):
            for spec in targets.values():
                if isinstance(spec, dict) and spec.get("terminal"):
                    pins.add(str(spec["terminal"]))
    return pins


def decide(
    proc: dict[str, Any],
    *,
    listen_ports: set[int],
    pins: set[str],
    flash_done_age: float | None,
    apply: bool = False,
) -> str:
    """Return keep|term|kill. Pure."""
    cmd = str(proc.get("cmd") or "")
    handle = str(proc.get("handle") or "")
    comm = str(proc.get("comm") or "")
    if any(n in cmd or comm == n for n in NEVER_KILL):
        return "keep"
    if handle and handle in pins:
        if (
            flash_done_age is not None
            and flash_done_age >= 300
            and "dsh" in cmd
            and "--profile headless" in cmd
            and "--profile tui" not in cmd
        ):
            return "term"
        return "keep"
    if "dsh" in cmd and "--profile tui" in cmd:
        return "keep"
    if "KnotClient" in cmd or "quickPlayMultiplayer" in cmd:
        return "term"
    if "paper.jar" in cmd:
        return "term"
    ports = set(proc.get("listen") or [])
    if ports:
        if ports & ALLOW_PORTS:
            return "keep"
        if not listen_ports and not ports:
            return "keep"
        if ports and not (ports & ALLOW_PORTS):
            return "term"
    if proc.get("listen_unknown"):
        return "keep"
    return "keep"


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    apply = "--apply" in args
    fixture = None
    if "--fixture" in args:
        idx = args.index("--fixture")
        if idx + 1 >= len(args):
            print(json.dumps({"ok": False, "error": "usage: --fixture FILE"}))
            return 2
        fixture = Path(args[idx + 1])
    decisions: list[dict[str, Any]] = []
    if fixture is not None:
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        listen_ports = {int(p) for p in (payload.get("listen_ports") or [])}
        pins = {str(p) for p in (payload.get("pins") or [])}
        flash_done_age = payload.get("flash_done_age")
        for proc in payload.get("procs") or []:
            action = decide(
                proc,
                listen_ports=listen_ports,
                pins=pins,
                flash_done_age=flash_done_age,
                apply=apply,
            )
            decisions.append({"cmd": proc.get("cmd"), "handle": proc.get("handle"), "action": action})
    print(json.dumps({"ok": True, "dry_run": not apply, "decisions": decisions}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
