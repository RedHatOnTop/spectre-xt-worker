#!/usr/bin/env python3
"""Process-backed SSOT reaper; dry-run unless explicitly invoked with --apply."""
from __future__ import annotations

import argparse
import json
import os
import math
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import time

for parent in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent / 'lib/spectre-worker-state',
               Path('/usr/local/lib/spectre-worker-state')):
    if (parent / 'control_plane').is_dir():
        sys.path.insert(0, str(parent))
        break

from control_plane import inventory, runtime, tidy
from control_plane.io import locked, read_json, write_json, run
from worker_state.client import StateClient
from worker_state.process import pane_pid
from worker_state.qoder_jsonl import load_workers, workers_path
from worker_state.types import parse_ts

ALLOW_PORTS = {6768, 7676, 9222, 9091}
NEVER_KILL = ('sshd', 'tailscaled', 'orca-ide', 'spectre-state', 'spectre-slack-bridge',
              'devspace', 'spectre-reaper', 'spectre-loop', 'codex-provider-health')
RSS_MIB = 256
CPU_PERCENT = 5
FLASH_GRACE = 300


def pin_set(workers: dict, cwd: str) -> set[str]:
    entries = [entry for entry in workers.values() if entry.get('cwd') == cwd]
    return {pin for entry in entries for pin in [entry.get('terminal'),
        (entry.get('planner') or {}).get('terminal'),
        *[target.get('terminal') for target in (entry.get('targets') or {}).values()]] if pin}


def decide(proc, *, listen_ports, pins, flash_done_age, apply=False) -> str:
    cmd, comm = str(proc.get('cmd', '')), str(proc.get('comm', ''))
    try:
        argv = proc.get('argv') or shlex.split(cmd)
    except ValueError:
        return 'keep'
    ports = set(proc.get('listen', []))
    if ports & ALLOW_PORTS or proc.get('protected') or proc.get('listen_unknown'):
        return 'keep'
    if any(name in cmd or comm == name for name in NEVER_KILL) or 'gpt-6-astra' in cmd:
        return 'keep'
    if proc.get('handle') in pins:
        if (flash_done_age is not None and flash_done_age >= FLASH_GRACE
                and inventory.model(argv) == 'flash' and inventory.option(argv, '--profile') == 'headless'):
            return 'term'
        return 'keep'
    if 'dsh' in cmd and '--profile tui' in cmd:
        return 'keep'
    if 'KnotClient' in cmd or 'quickPlayMultiplayer' in cmd or 'paper.jar' in cmd:
        return 'term'
    if ports or proc.get('rss_mib', 0) >= proc.get('rss_limit', RSS_MIB) or proc.get('cpu_hot_twice'):
        return 'term'
    if proc.get('age', 0) >= FLASH_GRACE and (proc.get('duplicate') or proc.get('untitled')):
        return 'term'
    if (inventory.model(argv) == 'flash' and inventory.option(argv, '--profile') == 'headless'
            and proc.get('unpinned_flash_done_age', 0) >= FLASH_GRACE):
        return 'term'
    return 'keep'


def terminate(observed: dict) -> None:
    pid = observed['pid']
    if pid <= 1 or pid in {os.getpid(), os.getppid()}:
        raise ValueError('refusing own process or init')
    fd = os.pidfd_open(pid)
    try:
        current = inventory.read_process(Path('/proc') / str(pid))
        if current is None or current['start'] != observed['start'] or current['uid'] != os.getuid():
            raise ValueError('process identity changed; refusing signal')
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    finally:
        os.close(fd)


def ancestor_protected(proc: dict, rows: dict, roots: set[int]) -> bool:
    current, visited = proc, frozenset()
    while current and current['pid'] not in visited:
        if current['pid'] in roots:
            return True
        visited = visited | {current['pid']}
        current = rows.get(current['ppid'])
    return False


def flash_age(entry, snapshot, proc, now, *, unpinned=False):
    pin = (entry.get('targets', {}).get('flash') or {}).get('terminal')
    if not unpinned and (not pin or proc.get('handle') != pin):
        return None
    goal, execution = snapshot.get('goal', {}), snapshot.get('execution', {})
    if goal.get('state') not in {'COMPLETED', 'FAILED'} or execution.get('target') != 'flash':
        return None
    value = execution.get('last_progress_at')
    stamp = value if isinstance(value, (float, int)) else parse_ts(value)
    return now - stamp if stamp is not None else None


def tree_listeners(processes, listen):
    by_pid = {row['pid']: row for row in processes}
    result = dict(listen)
    for pid, ports in listen.items():
        current, seen = by_pid.get(pid), frozenset()
        while current and current['pid'] not in seen:
            ident = current['pid']
            result = {**result, ident: result.get(ident, set()) | ports}
            seen = seen | {ident}
            current = by_pid.get(current['ppid'])
    return result


def hygiene_fields(proc, entry, snapshot, terminals, now):
    terminal = next((row for row in terminals if row.get('handle') == proc['handle']
                     and row.get('worktreePath') == entry.get('cwd')), {})
    title = terminal.get('title', '')
    untitled = bool(terminal) and (title in {'', 'bash', 'person@spectre'}
                                  or title.startswith('(person@spectre shell, untitled)'))
    age = time.clock_gettime(time.CLOCK_BOOTTIME) - proc['start'] / os.sysconf('SC_CLK_TCK')
    return {'age': age, 'duplicate': bool(entry) and proc['comm'] in {
        'qodercli', 'qoder', 'qoder-efficient', 'codex', 'codex-cli', 'codex.js'},
        'untitled': untitled and proc['comm'] in {'bash', 'sh', 'zsh', 'fish', 'dash'},
        'unpinned_flash_done_age': flash_age(entry, snapshot, proc, now, unpinned=True) or 0}


def observe(workers, processes, listen, previous, client, now, terminals=()):
    roots = {os.getpid(), os.getppid()}
    roots = roots | {pid for entry in workers.values() if entry.get('tmux')
                     if (pid := pane_pid(entry['tmux'])) is not None}
    by_pid = {row['pid']: row for row in processes}
    listen = tree_listeners(processes, listen)
    snapshots = {name: client.snapshot(name) for name in workers}
    all_pins = {pin for entry in workers.values() for pin in pin_set(workers, entry['cwd'])}
    decisions, samples = [], {}
    rss_limit = threshold('SPECTRE_REAPER_RSS_MIB', RSS_MIB)
    cpu_limit = threshold('SPECTRE_REAPER_CPU_PERCENT', CPU_PERCENT)
    for proc in processes:
        key = f"{proc['pid']}:{proc['start']}"
        old = previous.get('samples', {}).get(key, {})
        elapsed = now - old.get('at', now)
        cpu = (100 * (proc['ticks'] - old.get('ticks', proc['ticks'])) / os.sysconf('SC_CLK_TCK') / elapsed
               if elapsed > 0 else 0)
        entry_name = next((name for name, entry in workers.items() if entry['cwd'] == proc['cwd']), None)
        entry = workers.get(entry_name, {})
        snapshot = snapshots.get(entry_name, {})
        unknown = snapshot.get('goal', {}).get('state') == 'UNKNOWN'
        enriched = {**proc, **hygiene_fields(proc, entry, snapshot, terminals, now),
                    'listen': listen.get(proc['pid'], set()), 'rss_limit': rss_limit,
                    'protected': unknown or ancestor_protected(proc, by_pid, roots),
                    'cpu_hot_twice': cpu >= cpu_limit and old.get('hot', False)}
        action = decide(enriched, listen_ports=set(), pins=all_pins,
                        flash_done_age=flash_age(entry, snapshot, proc, now))
        decisions = [*decisions, {**enriched, 'action': action}]
        samples = {**samples, key: {'at': now, 'ticks': proc['ticks'], 'hot': cpu >= cpu_limit}}
    return decisions, {'samples': samples}


def threshold(name, default):
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be positive and finite')
    return value


def summary(row):
    return {key: sorted(row[key]) if isinstance(row[key], set) else row[key]
            for key in ('pid', 'comm', 'handle', 'rss_mib', 'listen', 'action') if key in row}


def live(args):
    workers = load_workers(args.workers_file)
    runtime.validate_workers(workers)
    if not workers:
        raise ValueError('worker registry missing or empty')
    proc = subprocess.run(['ss', '-ltnpH'], capture_output=True, text=True, timeout=5, check=True)
    # Missing process ownership makes the entire listen table unsafe to act on.
    if any(line.strip() and 'pid=' not in line for line in proc.stdout.splitlines()):
        raise ValueError('listener ownership incomplete; refusing reap')
    listed = run(['orca-ide', 'terminal', 'list', '--json'], timeout=8)
    if not listed['ok']:
        raise ValueError('Orca inventory unavailable; refusing reap')
    terminals = listed['parsed'].get('result', {}).get('terminals', [])
    with locked(args.state.with_suffix('.lock')):
        rows, state = observe(workers, inventory.scan(), inventory.listeners(proc.stdout),
                              read_json(args.state), StateClient(timeout=2), time.time(), terminals)
        actions = []
        for row in rows:
            if args.apply and row['action'] == 'term':
                try:
                    terminate(row)
                except (OSError, ValueError) as exc:
                    actions = [*actions, {**summary(row), 'error': str(exc)}]
                    continue
            actions = [*actions, summary(row)]
        write_json(args.state, state)
    packet_dir = Path(os.environ.get('SPECTRE_PACKET_DIR',
                                     str(Path.home() / '.local/state/remote-agent/packets')))
    pinned = set()
    for entry in workers.values():
        pinned |= pin_set(workers, entry.get('cwd') or '')
    tabs = tidy.select(terminals, pinned, packet_dir, time.time())
    tab_actions = []
    for row in tabs:
        if args.apply:
            result = tidy.close(row['handle'])
            tab_actions = [*tab_actions, {**row, **result}]
        else:
            tab_actions = [*tab_actions, {**row, 'ok': True, 'dry_run': True}]
    killed = [row for row in actions if args.apply and row['action'] == 'term' and 'error' not in row]
    notice = runtime.notify(run, dict(os.environ), 'reaper', json.dumps(killed), channel='fleet') if killed else {'ok': True}
    return {'ok': notice['ok'] and not any('error' in row for row in actions + tab_actions),
            'dry_run': not args.apply, 'decisions': actions, 'orca_tabs': tab_actions,
            'notification': notice}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--workers-file', type=Path, default=workers_path())
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/state/remote-agent/reaper.json')
    args = parser.parse_args(argv)
    if args.fixture and args.apply:
        parser.error('--fixture cannot be used with --apply')
    try:
        if args.fixture:
            payload = read_json(args.fixture)
            rows = [{**row, 'action': decide(row, listen_ports=set(payload.get('listen_ports', [])),
                pins=set(payload.get('pins', [])), flash_done_age=payload.get('flash_done_age'))}
                for row in payload.get('procs', [])]
            output = {'ok': True, 'dry_run': True, 'decisions': rows}
        else:
            output = live(args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        output = {'ok': False, 'error': str(exc)}
    print(json.dumps(output, sort_keys=True))
    return 0 if output['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
