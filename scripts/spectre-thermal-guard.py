#!/usr/bin/env python3
"""Thermal and compile-farm guard for the Spectre worker box.

Runs as a user timer every 60 s. Samples package temperature plus the hottest
processes into a JSONL log, stops build-class workloads and CPU/memory-intensive
non-agent work (AGENTS.md: the box is an agent runtime, not a compile farm) and,
at CRIT, the hottest non-agent process. Signals nothing unless `--apply` is
given; `--check` prints one JSON object for verification gates.

Exit codes: 0 ok, 1 warn or a blocked build/intensive workload, 2 crit.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

for parent in (Path(__file__).resolve().parent,
               Path(__file__).resolve().parent.parent / 'lib/spectre-worker-state',
               Path('/usr/local/lib/spectre-worker-state')):
    if (parent / 'control_plane').is_dir():
        sys.path.insert(0, str(parent))
        break

from control_plane import inventory, thermal

NOTIFY = '/usr/local/bin/spectre-slack-notify'


def collect(state: dict, now: float) -> dict:
    procs = inventory.scan()
    uptime_s = thermal.read_uptime_s()
    cpu = thermal.cpu_percent(state.get('ticks') or {}, procs, now, uptime_s)
    temp_c = thermal.read_temp_c()
    return {
        'procs': procs,
        'cpu': cpu,
        'temp_c': temp_c,
        'policy': thermal.read_policy(),
        'ticks': {str(p['pid']): [int(p.get('ticks') or 0), int(p.get('start') or 0), now]
                  for p in procs},
    }


def terminate(proc: dict) -> dict:
    """SIGTERM a sampled process after re-confirming identity in /proc."""
    pid = int(proc.get('pid') or 0)
    result = {'pid': pid, 'comm': proc.get('comm'), 'family': proc.get('family'),
              'reason': proc.get('reason'), 'ok': False}
    if pid <= 1 or pid in {os.getpid(), os.getppid(), os.getsid(0)}:
        return {**result, 'error': 'refused_self_or_init'}
    fresh = inventory.read_process(Path('/proc') / str(pid))
    if not fresh:
        return {**result, 'error': 'gone'}
    if int(fresh.get('start') or 0) != int(proc.get('start') or 0):
        return {**result, 'error': 'pid_reused'}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return {**result, 'error': f'kill_failed:{exc.__class__.__name__}'}
    return {**result, 'ok': True, 'cmd': ' '.join(shlex.split(fresh.get('cmd') or ''))[:200]}


def notify(channel: str, text: str, *, recovery: bool = False, dry_run: bool = False) -> bool:
    if dry_run or not Path(NOTIFY).exists():
        return False
    argv = [NOTIFY, '--agent', 'spectre', '--channel', channel]
    if recovery:
        argv.append('--recovery')
    argv += ['--text', text]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def describe(row: dict) -> str:
    cmd = ' '.join(shlex.split(str(row.get('cmd') or '')))[:160]
    detail = f"`{row.get('family') or 'hot'}` pid {row.get('pid')} ({row.get('comm')}) {cmd}"
    if row.get('cpu_pct') is not None or row.get('rss_mib') is not None:
        detail += f" [cpu {row.get('cpu_pct')}%, rss {row.get('rss_mib')} MiB]"
    return detail


def run_once(args, state: dict, now: float) -> tuple[dict, int]:
    data = collect(state, now)
    allow = thermal.read_allow(args.allow_file)
    builds = thermal.build_workloads(data['procs'], allow)
    streaks = thermal.next_streaks(state.get('cpu_streaks') or {}, data['cpu'])
    intensive = thermal.intensive_workloads(data['procs'], data['cpu'], streaks, allow)
    level = thermal.level_for(data['temp_c'], args.warn_c, args.crit_c)
    history = [*state.get('levels', [])[-5:], level]
    hot = thermal.escalated(history, 'crit', args.streak)

    actions: list[dict] = []
    if args.apply:
        for row in builds:
            actions = [*actions, terminate(row)]
        for row in intensive:
            actions = [*actions, terminate(row)]
        if hot:
            owned = {int(row['pid']) for row in builds} | {int(row['pid']) for row in intensive}
            for row in thermal.crit_targets(data['procs'], data['cpu'], allow=allow):
                if int(row['pid']) in owned:
                    continue
                actions = [*actions, terminate({**row, 'reason': 'thermal_crit'})]

    killed_builds = [a for a in actions if a.get('ok') and str(a.get('reason', '')).startswith('build_workload')]
    killed_intensive = [a for a in actions if a.get('ok') and str(a.get('reason', '')).startswith('intensive_workload')]
    killed_hot = [a for a in actions if a.get('ok') and a.get('reason') == 'thermal_crit']
    sent = state.get('notified') or {}
    notified = []
    top = '; '.join(f"{c['comm']}({c['cpu_pct']}%)" for c in thermal.consumers(data['procs'], data['cpu']))

    def fire(kind: str, channel: str, text: str, *, recovery: bool = False) -> None:
        nonlocal sent, notified
        if not args.notify:
            return
        if not recovery and not thermal.due(sent.get(kind), now):
            return
        if notify(channel, text, recovery=recovery, dry_run=args.dry_run):
            sent = {**sent, kind: now}
            notified = [*notified, kind]

    if killed_builds:
        fire('build', 'fleet',
             ':rotating_light: compile-farm block on spectre: ' +
             ', '.join(describe(a) for a in killed_builds) +
             '. The Spectre is an agent runtime, not a compile farm (AGENTS.md); '
             'offload heavy builds (RUNBOOK 7.21).')
    if killed_intensive:
        fire('intensive', 'fleet',
             ':outbox_tray: spectre offload candidate stopped: ' +
             ', '.join(describe(a) for a in killed_intensive) +
             ' — this box is an agent runtime; run heavy work in the Studio '
             '(spectre-offload, RUNBOOK 7.21)')
    if level == 'crit' and thermal.escalated(history, 'crit', args.streak):
        fire('crit', 'alerts',
             f":fire: spectre thermal CRIT {data['temp_c']:.0f}C (crit {args.crit_c:.0f}C, "
             f"max_perf_pct {data['policy'].get('max_perf_pct')}). "
             f"stopped: {'; '.join(describe(a) for a in killed_hot) or 'nothing eligible'}. top: {top}")
    elif level == 'warn':
        fire('warn', 'alerts',
             f":thermometer: spectre thermal warn {data['temp_c']:.0f}C (warn {args.warn_c:.0f}C). "
             f"top: {top}" + (f"; blocked builds: {len(builds)}" if builds else ''))
    elif state.get('level') in ('warn', 'crit'):
        fire('recovery', 'alerts',
             f"spectre thermal back to {data['temp_c']:.0f}C", recovery=True)

    sample = {
        'ts': thermal.now_iso(now), 'at': round(now, 1),
        'temp_c': data['temp_c'], 'level': level, 'policy': data['policy'],
        'consumers': thermal.consumers(data['procs'], data['cpu']),
        'build_workloads': [{'pid': b['pid'], 'comm': b.get('comm'), 'family': b.get('family'),
                             'cmd': ' '.join(shlex.split(str(b.get('cmd') or '')))[:200]}
                            for b in builds],
        'intensive_workloads': [{'pid': i['pid'], 'comm': i.get('comm'), 'reason': i.get('reason'),
                                 'cpu_pct': i.get('cpu_pct'), 'rss_mib': i.get('rss_mib'),
                                 'cmd': ' '.join(shlex.split(str(i.get('cmd') or '')))[:200]}
                                for i in intensive],
        'allow': sorted(allow), 'actions': actions, 'notified': notified,
        'apply': bool(args.apply),
    }
    thermal.append_jsonl(args.state_dir / 'thermal.jsonl', sample)
    thermal.rotate(args.state_dir / 'thermal.jsonl')
    if killed_builds or killed_intensive or killed_hot:
        thermal.append_jsonl(args.state_dir / 'thermal-kills.jsonl',
                             {'ts': sample['ts'], 'temp_c': data['temp_c'], 'level': level,
                              'actions': [a for a in actions if a.get('ok')]})

    next_state = {
        'ticks': data['ticks'], 'levels': history[-10:], 'level': level,
        'notified': sent, 'last_sample': sample['ts'], 'last_temp_c': data['temp_c'],
        'last_actions': actions,
        'cpu_streaks': {str(pid): count for pid, count in streaks.items()},
    }
    code = 2 if level == 'crit' else (1 if (level == 'warn' or builds or intensive) else 0)
    return next_state, code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--once', action='store_true', help='sample and enforce (default output: JSON)')
    mode.add_argument('--check', action='store_true', help='one-shot status for verification gates')
    parser.add_argument('--apply', action='store_true', help='signal processes; without it, dry-run')
    parser.add_argument('--dry-run', action='store_true', help='never post to Slack')
    parser.add_argument('--no-notify', action='store_true', help='never post to Slack')
    parser.add_argument('--state-dir', type=Path, default=thermal.STATE_DIR)
    parser.add_argument('--allow-file', type=Path,
                        default=Path.home() / '.config/remote-agent/thermal-allow')
    parser.add_argument('--warn-c', type=float, default=thermal.WARN_C)
    parser.add_argument('--crit-c', type=float, default=thermal.CRIT_C)
    parser.add_argument('--streak', type=int, default=thermal.HOT_STREAK)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    args.notify = not args.no_notify
    args.state_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.state_dir / 'thermal-guard.json'
    state = thermal.load_state(state_path)
    # Wall clock throughout: cooldowns and CPU deltas must survive a reboot.
    now = time.time()

    if args.check:
        args.apply = False
        args.notify = False
        next_state, code = run_once(args, state, now)
        report = {'ok': code == 0, 'temp_c': next_state.get('last_temp_c'), 'level': next_state.get('level'),
                  'policy': thermal.read_policy(), 'warn_c': args.warn_c, 'crit_c': args.crit_c,
                  'last_actions': next_state.get('last_actions'), 'apply': False}
        print(json.dumps(report, default=str))
        return code

    next_state, code = run_once(args, state, now)
    thermal.save_state(state_path, next_state)
    if args.json:
        print(json.dumps({'level': next_state.get('level'), 'temp_c': next_state.get('last_temp_c'),
                          'actions': next_state.get('last_actions')}, default=str))
    return code


if __name__ == '__main__':
    sys.exit(main())
