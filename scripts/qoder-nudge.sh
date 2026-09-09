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
        "ZERO-DEFECT inspection of the EXISTING 태초마을 on the Spectre "
        "review server (cwd /home/person/mc-taecho-review, game 25569). "
        "Do not flatten, do not full-rebuild, do not touch the flagship lobby. "
        "Authoritative: TAECHO_VILLAGE.md (esp §0 defects, §2 terraces/axis, "
        "§3 giwa, §4 palette, §7 audit), SERVER_GAME_REWORK_PLAN.md §1, "
        "AGENTS.md vision loop. Seed -5411652232113409693, center (150,390), "
        "axis x=150. Work envelope x 62..224, z 302..486. "
        "Keep the 4 terraces and natural grade outside them: harbor y65 "
        "z306-340, town y67 z342-412, civic y73 z414-458, fields y71/69 "
        "x64-94. "
        "Method: pick ONE defect class per slice. Prove it from a FRESH dusk "
        "capture (time 13000, weather clear) — BlueMap tile AND/OR FPVCamera. "
        "Judge ONLY what the image shows. Quote the FAIL with a block coord "
        "and the pixel evidence. Fix with fill_deploy /fill boxes (tile-wise "
        "forceload; never one-shot the whole site; never per-block setblock). "
        "Re-capture the SAME view. If the FAIL is still visible, the slice "
        "is not done. Append each defect to "
        "servers-network/captures/taecho_v4/DEFECTS.md "
        "(id, xyz, class, before hash, after hash, PASS/FAIL). "
        "Walk in this order, then loop: (1) giwa roofs — 1:2 stair+slab, "
        "single-tone tuff_bricks field, deepslate ONLY at eave/ridge, no "
        "black-slab plates, no 2-color banding, no pyramid, no hay-only "
        "cartoon thatch; (2) empty lots / T0 매물 vs accidental 12x12 dirt; "
        "every lived-in 채 has 온돌+부엌 interior; (3) terrace edges — "
        "retaining walls only on terrace columns, no stone scatter on "
        "natural grade, no 1-block chatter, no canyon black holes; "
        "(4) roads face every building, 4-tier hierarchy, no building on "
        "the road, no house overlapping a lot; (5) palette — calcite "
        "회벽, dark_oak/crimson 기둥, no nether-brick leftover, no "
        "exposed dirt skirts; (6) landmarks present at spec heights "
        "(종루/누각/석탑/문루/당산/등대); (7) harbor program, 연무장, "
        "홍살문, 장승, no v3 dock scar. "
        "A verify_taecho.py PASS is not a visual PASS. Coarse BlueMap "
        "zoom that cannot resolve the defect is an invalid capture. "
        "After every slice, start the next defect. Never stop at a summary."
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
