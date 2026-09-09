#!/usr/bin/env python3
"""Nudge Spectre Qoder TUIs back onto an open-ended /goal.

A self-declared complete is not a stop. History still showing Bash( is
not busy. Only a live Generating / goal-on footer is busy. Ready panes
get a critique-and-continue /goal --turns 9999.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ORCA = "/usr/bin/orca-ide"
LOG = Path("/work/logs/qoder-nudge.log")
_STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "remote-agent"
SENTINEL = _STATE / "qoder-efficient-billed"

LOOP = (
    "You are not allowed to mark this goal complete. UpdateGoal status "
    "complete, /goal complete, and stopping at a summary are failures. "
    "This is an open-ended agentic loop. If you just declared complete, "
    "that verdict is void: self-critique from evidence on disk only "
    "(captures, catalog rows, git diff — never from memory or the "
    "summary you just wrote), list the top gaps, then immediately ship "
    "the next slice. After every slice, verify, then continue. Never "
    "stop. Stay on Efficient. "
)

MISSIONS = {
    "/home/person/Projects/minecraft-server-project": (
        "Completely rebuild 태초마을 (Taecho Village), not the lobby. "
        "Authoritative docs: TAECHO_VILLAGE.md, SERVER_GAME_REWORK_PLAN.md §1, "
        "AGENTS.md vision loop (build→deploy→dusk capture→judge ONLY from the image; "
        "never claim it looks good from memory), existing taecho_*.py / taecho5 if present. "
        "Joseon coastal 읍성: harbor→town→civic terraces on axis x=150, seed "
        "-5411652232113409693, center (150,390). Do not flatten the whole site; "
        "keep 4 terraces and natural grade. Giwa 1:2 stair+slab, single-tone field, "
        "no empty 12x12 lots, no black-slab roofs, no lobby/flagship palace work. "
        "Use fill_deploy /fill boxes, not per-block setblock. After each slice, "
        "capture, self-check, then immediately start the next."
    ),
    "/work/korea-metro-twin": (
        "Research-only corpus for Daegu Metro Line 1 and Line 2. "
        "Read AGENTS.md. Fill catalog/ for every station, tunnel segment, and "
        "train class using public GIS, operator docs, CC/Wikimedia, YouTube "
        "(yt-dlp with rate limits). Do not bulk-scrape Google/Kakao/Naver "
        "로드뷰 tiles. Log grey sources in catalog/blocked.jsonl instead of "
        "downloading. Vision-describe stills. No 3D, no Minecraft, no twin "
        "implementation. After each catalog row, immediately fill the next gap. "
        "Never stop at a summary."
    ),
}

COMPLETE_RE = re.compile(
    r'UpdateGoal[\s\S]{0,120}complete|"status"\s*:\s*"complete"',
    re.IGNORECASE,
)
LIVE_GENERATING_RE = re.compile(r"Generating\.\.\.|esc to cancel")
LIVE_GOAL_ON_RE = re.compile(r"goal on \d+")
FOOTER_LINES = 20


def goal_text(path: str) -> str:
    mission = MISSIONS[path]
    return f"/goal {LOOP}Standing mission: {mission} --turns 9999"


def _footer(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-FOOTER_LINES:])


def classify(text: str) -> str:
    footer = _footer(text)
    if LIVE_GENERATING_RE.search(footer) or LIVE_GOAL_ON_RE.search(footer):
        return "busy"
    if "Efficient Model" not in text and "Type your message" not in text:
        return "splash"
    if COMPLETE_RE.search(text):
        return "complete"
    return "idle"


def should_nudge(text: str) -> bool:
    return classify(text) in ("idle", "complete")


def run(args: list[str]) -> dict:
    raw = subprocess.check_output(args, text=True)
    return json.loads(raw)


def screen_text(handle: str) -> str:
    data = run([ORCA, "terminal", "read", "--terminal", handle, "--screen", "--json"])
    tail = data.get("result", {}).get("terminal", {}).get("tail") or []
    if isinstance(tail, list):
        return "\n".join(tail)
    return str(tail)


def clear_composer(handle: str) -> None:
    run([ORCA, "terminal", "send", "--terminal", handle, "--interrupt", "--json"])
    run(
        [
            ORCA,
            "terminal",
            "send",
            "--terminal",
            handle,
            "--text",
            "\x15",
            "--json",
        ]
    )


def send_goal(handle: str, text: str) -> None:
    clear_composer(handle)
    run(
        [
            ORCA,
            "terminal",
            "send",
            "--terminal",
            handle,
            "--text",
            text,
            "--json",
        ]
    )
    # Long /goal pastes land in the composer; --enter on the same
    # send is dropped. Submit after the TUI accepts the text.
    time.sleep(0.4)
    run([ORCA, "terminal", "send", "--terminal", handle, "--enter", "--json"])


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    print(msg)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    dry_run = "--dry-run" in args
    if SENTINEL.exists():
        log("skip billed-sentinel")
        return 0
    listing = run([ORCA, "terminal", "list", "--json"])
    terminals = listing.get("result", {}).get("terminals") or []
    nudged = 0
    for term in terminals:
        path = term.get("worktreePath") or ""
        handle = term.get("handle")
        if not handle or path not in MISSIONS:
            continue
        text = screen_text(handle)
        state = classify(text)
        if not should_nudge(text):
            log(f"{state} {path}")
            continue
        if dry_run:
            log(f"dry-run {state} {path} {handle}")
            nudged += 1
            continue
        send_goal(handle, goal_text(path))
        nudged += 1
        log(f"nudged {state} {path} {handle}")
    if nudged == 0:
        log("idle-check none")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        log(f"failed {exc}")
        raise SystemExit(1)
