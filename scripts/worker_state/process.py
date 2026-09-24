"""Cheap process/transport corroboration. Authority 4 — never flips goal lifecycle."""
from __future__ import annotations

import os
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


def _stat_ticks(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    try:
        text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
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


def _children(pid: int, proc_root: Path = Path("/proc")) -> list[int]:
    task_root = proc_root / str(pid) / "task"
    try:
        tasks = list(task_root.iterdir())
    except OSError:
        tasks = []
    found: set[int] = set()
    readable = False
    for task in tasks:
        if not task.name.isdigit():
            continue
        try:
            listed = (task / "children").read_text(encoding="utf-8")
        except OSError:
            continue
        readable = True
        found.update(int(value) for value in listed.split() if value.isdigit())
    if readable:
        return sorted(found)
    out: list[int] = []
    try:
        for entry in proc_root.iterdir():
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


def _cmdline(pid: int, proc_root: Path) -> str:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes().replace(b"\x00", b" ")
        return raw.decode("utf-8", "replace")
    except OSError:
        return ""


def _environ_handle(pid: int, proc_root: Path) -> str:
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return ""
    for part in raw.split(b"\0"):
        if part.startswith(b"ORCA_TERMINAL_HANDLE="):
            return part.split(b"=", 1)[1].decode("utf-8", "replace")
    return ""


def _cwd(pid: int, proc_root: Path) -> str | None:
    try:
        return os.readlink(proc_root / str(pid) / "cwd")
    except OSError:
        return None


def _is_headless_dsh(cmd: str) -> bool:
    if "--profile tui" in cmd:
        return False
    return "dsh" in cmd and "--profile headless" in cmd


def _tree_pids(root_pid: int, proc_root: Path) -> list[int]:
    pids = [root_pid]
    seen = {root_pid}
    queue = [root_pid]
    while queue:
        for child in _children(queue.pop(), proc_root):
            if child in seen:
                continue
            seen.add(child)
            pids.append(child)
            queue.append(child)
    return pids


def sample_tree(root_pid: int, proc_root: Path = Path("/proc")) -> dict[str, Any] | None:
    if not (proc_root / str(root_pid)).is_dir():
        return None
    pids = _tree_pids(root_pid, proc_root)
    ticks = 0
    alive = 0
    for pid in pids:
        value = _stat_ticks(pid, proc_root)
        if value is None:
            continue
        ticks += value
        alive += 1
    cmdline = _cmdline(root_pid, proc_root)[:120]
    cgroup = ""
    try:
        cgroup = (proc_root / str(root_pid) / "cgroup").read_text(encoding="utf-8").splitlines()[-1][:160]
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


def flash_pin(entry: dict) -> str:
    targets = entry.get("targets")
    if not isinstance(targets, dict):
        return ""
    flash = targets.get("flash")
    if not isinstance(flash, dict):
        return ""
    return str(flash.get("terminal") or "")


def _cwd_matches(got: str | None, wanted: str | None) -> bool:
    if not wanted:
        return True
    if got is None:
        return True
    left = got.rstrip("/")
    right = str(wanted).rstrip("/")
    if left == right:
        return True
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except OSError:
        return False


def _headless_in_tree(root_pid: int, proc_root: Path) -> int | None:
    if not (proc_root / str(root_pid)).is_dir():
        return None
    for pid in _tree_pids(root_pid, proc_root):
        if _is_headless_dsh(_cmdline(pid, proc_root)):
            return pid
    return None


def find_headless_dsh(
    proc_root: Path = Path("/proc"),
    *,
    pin: str | None = None,
    cwd: str | None = None,
    root_pid: int | None = None,
) -> int | None:
    """Pin-tree headless dsh only. Never a full /proc cmdline walk."""
    if root_pid is not None:
        found = _headless_in_tree(root_pid, proc_root)
        if found is not None:
            return found
        if _is_headless_dsh(_cmdline(root_pid, proc_root)):
            return root_pid
        return None
    wanted = str(pin or "")
    if not wanted:
        return None
    try:
        ents = list(proc_root.iterdir())
    except OSError:
        return None
    for ent in ents:
        if not ent.name.isdigit():
            continue
        pid = int(ent.name)
        if _environ_handle(pid, proc_root) != wanted:
            continue
        if not _cwd_matches(_cwd(pid, proc_root), cwd):
            continue
        found = _headless_in_tree(pid, proc_root)
        if found is not None:
            return found
        if _is_headless_dsh(_cmdline(pid, proc_root)):
            return pid
    return None


def _dead_sample(pid: int | None) -> dict[str, Any]:
    return {
        "alive": False,
        "nprocs": 0,
        "cpu_ticks": 0,
        "cmdline": "",
        "cgroup": "",
        "pid": pid,
    }


def find_efficient_pid(entry: dict, proc_root: Path) -> int | None:
    pin = (entry.get("targets", {}).get("efficient") or {}).get("terminal") or entry.get("terminal")
    if not pin:
        return None
    matches = set()
    for path in proc_root.iterdir():
        if not path.name.isdigit() or _environ_handle(int(path.name), proc_root) != pin:
            continue
        for pid in _tree_pids(int(path.name), proc_root):
            args = _cmdline(pid, proc_root).split()
            if not args or not any(Path(arg).name in {"qodercli", "qoder-efficient"} for arg in args[:2]):
                continue
            efficient = any(arg in {"-m", "--model"} and index + 1 < len(args)
                            and args[index + 1].lower() == "efficient" for index, arg in enumerate(args))
            if efficient and _cwd_matches(_cwd(pid, proc_root), entry.get("cwd")):
                matches.add(pid)
    leaves = matches - {pid for pid in matches if set(_children(pid, proc_root)) & matches}
    return next(iter(leaves)) if len(leaves) == 1 else None


def evidence_for_worker(
    name: str,
    entry: dict,
    now: float,
    *,
    target: str = "efficient",
    proc_root: Path | None = None,
) -> list[dict[str, Any]]:
    root = proc_root or Path("/proc")
    tmux = entry.get("tmux")
    source = "tmux"
    pid: int | None = None
    emit_dead = str(target or "efficient") == "flash"
    if tmux:
        pid = pane_pid(str(tmux))
    elif str(target or "efficient") == "flash":
        source = "process"
        raw = entry.get("_pid")
        root_pid = int(raw) if raw else None
        pid = find_headless_dsh(
            root,
            pin=flash_pin(entry),
            cwd=str(entry.get("cwd") or "") or None,
            root_pid=root_pid,
        )
    elif entry.get("terminal") or entry.get("_pid"):
        source = "process"
        raw = entry.get("_pid")
        pid = int(raw) if raw else find_efficient_pid(entry, root)
    else:
        return []
    events: list[dict[str, Any]] = []
    if pid is None:
        tree = _dead_sample(None) if emit_dead else None
    else:
        tree = sample_tree(pid, root)
        if tree is None and emit_dead:
            tree = _dead_sample(pid)
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
            "source": source,
            "source_timestamp": iso_from(now),
            "payload": {"kind": "tmux" if tmux else "dsh" if target == "flash" else "orca"},
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
