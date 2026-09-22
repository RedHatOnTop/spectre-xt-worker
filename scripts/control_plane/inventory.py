"""Process identity from argv and Orca terminal ancestry, never UI titles."""
from __future__ import annotations

import os
from pathlib import Path
import re


def option(argv: list[str], *names: str) -> str | None:
    for index, arg in enumerate(argv):
        if arg in names and index + 1 < len(argv):
            return argv[index + 1]
        for name in names:
            if arg.startswith(name + '='):
                return arg[len(name) + 1:]
    return None


def executable(argv: list[str]) -> str:
    if not argv:
        return ''
    first = Path(argv[0]).name
    if first.startswith(('python', 'node')) and len(argv) > 1:
        return Path(argv[1]).name
    return first


def model(argv: list[str]) -> str | None:
    exe = executable(argv)
    selected = option(argv, '-m', '--model')
    if exe in {'codex', 'codex-cli'} and selected == 'gpt-6-astra':
        return 'astra'
    if exe in {'qodercli', 'qoder', 'qoder-efficient'} and str(selected).lower() == 'efficient':
        return 'efficient'
    if exe in {'dsh-clinepass'} or (exe == 'dsh' and option(argv, '--profile') in {'headless', 'tui', 'minimal'}):
        return 'flash'
    if exe in {'mimo-clinepass', 'mimo', 'mimocode'}:
        return 'mimo'
    return None


def read_process(path: Path) -> dict | None:
    try:
        stat = path.joinpath('stat').read_text().rsplit(')', 1)[1].split()
        argv = path.joinpath('cmdline').read_bytes().decode('utf-8', 'replace').rstrip('\0').split('\0')
        status = path.joinpath('status').read_text()
        uid = int(re.search(r'^Uid:\s+(\d+)', status, re.M).group(1))
        rss = re.search(r'^VmRSS:\s+(\d+)', status, re.M)
        cwd = os.readlink(path / 'cwd')
        raw = path.joinpath('environ').read_bytes().split(b'\0')
        handle = next((v.split(b'=', 1)[1].decode() for v in raw
                       if v.startswith(b'ORCA_TERMINAL_HANDLE=')), '')
        return {'pid': int(path.name), 'ppid': int(stat[1]), 'start': int(stat[19]),
                'ticks': int(stat[11]) + int(stat[12]), 'uid': uid, 'argv': argv,
                'cmd': ' '.join(argv), 'comm': executable(argv), 'cwd': cwd,
                'handle': handle, 'rss_mib': int(rss.group(1)) / 1024 if rss else 0,
                'model': model(argv), 'process_state': stat[0]}
    except (OSError, ValueError, IndexError, AttributeError, UnicodeError):
        return None


def inherited_handle(proc: dict, by_pid: dict[int, dict]) -> str:
    current = proc
    seen = frozenset()
    while current and current['pid'] not in seen:
        if current['handle']:
            return current['handle']
        seen = seen | {current['pid']}
        current = by_pid.get(current['ppid'])
    return ''


def scan(root: Path = Path('/proc')) -> list[dict]:
    rows = [row for path in root.iterdir() if path.name.isdigit()
            if (row := read_process(path)) and row['uid'] == os.getuid()
            and row['process_state'] != 'Z']
    by_pid = {row['pid']: row for row in rows}
    return [{**row, 'handle': inherited_handle(row, by_pid)} for row in rows]


def listeners(text: str) -> dict[int, set[int]]:
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != 'LISTEN':
            continue
        try:
            port = int(parts[3].rsplit(':', 1)[1])
        except (ValueError, IndexError):
            continue
        for value in re.findall(r'pid=(\d+)', line):
            pid = int(value)
            result = {**result, pid: result.get(pid, set()) | {port}}
    return result
