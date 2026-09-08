#!/usr/bin/env python3
"""Nudge idle Spectre Qoder TUI sessions back onto an open-ended /goal.

Skip a pane that is already Thinking or running a tool. Idle composer
gets /goal ... --turns 9999 so Efficient keeps shipping the next slice.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ORCA = "/usr/bin/orca-ide"
LOG = Path("/work/logs/qoder-nudge.log")

GOALS = {
    "/home/person/Projects/minecraft-server-project": (
        "/goal Completely rebuild 태초마을 (Taecho Village), not the lobby. "
        "Authoritative docs: TAECHO_VILLAGE.md, SERVER_GAME_REWORK_PLAN.md §1, "
        "AGENTS.md vision loop (build→deploy→dusk capture→judge ONLY from the image; "
        "never claim it looks good from memory), existing taecho_*.py / taecho5 if present. "
        "Joseon coastal 읍성: harbor→town→civic terraces on axis x=150, seed "
        "-5411652232113409693, center (150,390). Do not flatten the whole site; "
        "keep 4 terraces and natural grade. Giwa 1:2 stair+slab, single-tone field, "
        "no empty 12x12 lots, no black-slab roofs, no lobby/flagship palace work. "
        "Use fill_deploy /fill boxes, not per-block setblock. After each slice, "
        "capture, self-check, then immediately start the next. Never stop at a "
        "summary. Stay on Efficient. --turns 9999"
    ),
    "/home/person/Projects/orca-rust": (
        "/goal Execute docs/PORT-PLAN.md to completion, currently Stage 2 AT-SPI, "
        "then every later stage. cargo -j1 only. After each stage, start the next "
        "immediately. Never stop at a summary. Stay on Efficient. --turns 9999"
    ),
}

BUSY = ("Thinking", "Bash(", "Read(", "Write(", "Edit(", "Glob(", "Grep(")


def run(args: list[str]) -> dict:
    raw = subprocess.check_output(args, text=True)
    return json.loads(raw)


def screen_text(handle: str) -> str:
    data = run([ORCA, "terminal", "read", "--terminal", handle, "--screen", "--json"])
    tail = data.get("result", {}).get("terminal", {}).get("tail") or []
    if isinstance(tail, list):
        return "\n".join(tail)
    return str(tail)


def idle(text: str) -> bool:
    if any(marker in text for marker in BUSY):
        return False
    return "Type your message" in text


def send_goal(handle: str, text: str) -> None:
    run(
        [
            ORCA,
            "terminal",
            "send",
            "--terminal",
            handle,
            "--text",
            text,
            "--enter",
            "--json",
        ]
    )


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    print(msg)


def main() -> int:
    listing = run([ORCA, "terminal", "list", "--json"])
    terminals = listing.get("result", {}).get("terminals") or []
    nudged = 0
    for term in terminals:
        path = term.get("worktreePath") or ""
        handle = term.get("handle")
        goal = GOALS.get(path)
        if not handle or not goal:
            continue
        text = screen_text(handle)
        if not idle(text):
            log(f"busy {path}")
            continue
        send_goal(handle, goal)
        nudged += 1
        log(f"nudged {path} {handle}")
    if nudged == 0:
        log("idle-check none")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        log(f"failed {exc}")
        raise SystemExit(1)
