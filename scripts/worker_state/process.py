"""Cheap process/transport corroboration. Authority 4 — never flips goal lifecycle."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .types import iso_from

_LAST: dict[str, tuple[bool, int, int]] = {}  # name -> (alive, nprocs, cpu)
_LAST_EMIT: dict[str, float] = {}
MIN_EMIT_SEC = 60.0


def pane_pid(tmux: str) -> int | None:
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux}:", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _stat_ticks(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    rparen = text.rfind(")")
    if rparen < 0:
        return None
    fields = text[rparen + 2 :].split()
    if len(fields) < 13:
        return None
    try:
        return int(fields[11]) + int(fields[12])
    except ValueError:
        return None


def _children(pid: int) -> list[int]:
    out: list[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
            except OSError:
                continue
            rparen = stat.rfind(")")
            if rparen < 0:
                continue
            fields = stat[rparen + 2 :].split()
            if len(fields) < 2:
                continue
            try:
                ppid = int(fields[1])
            except ValueError:
                continue
            if ppid == pid:
                out.append(int(entry.name))
    except OSError:
        return out
    return out


def sample_tree(root_pid: int) -> dict[str, Any] | None:
    if not Path(f"/proc/{root_pid}").is_dir():
        return None
    pids = [root_pid]
    seen = {root_pid}
    queue = [root_pid]
    while queue:
        children = _children(queue.pop())
        for child in children:
            if child in seen:
                continue
            seen.add(child)
            pids.append(child)
            queue.append(child)
    ticks = 0
    alive = 0
    for pid in pids:
        value = _stat_ticks(pid)
        if value is None:
            continue
        ticks += value
        alive += 1
    cmdline = ""
    try:
        cmdline = Path(f"/proc/{root_pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")[:120]
    except OSError:
        pass
    cgroup = ""
    try:
        cgroup = Path(f"/proc/{root_pid}/cgroup").read_text(encoding="utf-8").splitlines()[-1][:160]
    except OSError:
        pass
    return {
        "alive": alive > 0,
        "nprocs": alive,
        "cpu_ticks": ticks,
        "cmdline": cmdline.strip(),
        "cgroup": cgroup,
        "pid": root_pid,
    }


def evidence_for_worker(name: str, entry: dict, now: float) -> list[dict[str, Any]]:
    tmux = entry.get("tmux")
    if not tmux:
        return []
    pid = pane_pid(str(tmux))
    events: list[dict[str, Any]] = []
    if pid is None:
        tree = None
    else:
        tree = sample_tree(pid)
    alive = bool(tree and tree.get("alive"))
    nprocs = int((tree or {}).get("nprocs") or 0)
    cpu = int((tree or {}).get("cpu_ticks") or 0)
    prev = _LAST.get(name)
    last_emit = _LAST_EMIT.get(name, 0.0)
    cpu_delta = 0 if prev is None else max(0, cpu - prev[2])
    changed = prev is None or prev[0] != alive or prev[1] != nprocs
    due = now - last_emit >= MIN_EMIT_SEC
    if not changed and not due:
        return []
    _LAST[name] = (alive, nprocs, cpu)
    _LAST_EMIT[name] = now
    kind = "transport.reachable" if alive else "transport.down"
    events.append(
        {
            "event_id": f"transport-{name}-{kind}-{int(now)}",
            "worker": name,
            "kind": kind,
            "source": "tmux",
            "source_timestamp": iso_from(now),
            "payload": {"kind": "tmux"},
        }
    )
    if tree is not None:
        events.append(
            {
                "event_id": f"proc-{name}-{int(now)}",
                "worker": name,
                "kind": "process.sample",
                "source": "process",
                "source_timestamp": iso_from(now),
                "payload": {**tree, "cpu_delta": cpu_delta},
            }
        )
        if alive:
            events.append(
                {
                    "event_id": f"hb-{name}-{int(now)}",
                    "worker": name,
                    "kind": "heartbeat",
                    "source": "process",
                    "source_timestamp": iso_from(now),
                    "payload": {"pid": pid, "nprocs": nprocs},
                }
            )
    return events
