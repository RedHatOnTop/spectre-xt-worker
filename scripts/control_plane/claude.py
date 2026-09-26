"""Operator-driven handoff of the top-level seat to Claude Opus in a visible Orca terminal.

Claude is the best seat on the ladder, so nothing in the loop requests this handoff:
the operator starts it, watches the tab, and confirms each step. The pending
handoff lives under `claude_handoff` in the loop state because the loop clears
`handoff` whenever a relay is healthy.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import time

from . import inventory, kimi, orca, pins, planning, runtime, seat
from .io import locked, read_json, run as run_command, write_json

MODEL = 'claude-opus-5-5'
PROVIDER = seat.seats_for_owner(seat.OWNER_CLAUDE)[0]['provider']
TITLE = 'claude-planner'
PLANNER_MODELS = frozenset(seat.PLANNER_ROLES.values())


def command(binary: str) -> str:
    # Operator decision 2026-09-26: the planner runs unattended, so it does not stop
    # for permission prompts. The login is the box's own Claude Code (Max) login.
    return f'exec {shlex.quote(binary)} --model {MODEL} --dangerously-skip-permissions'


def cold_start_prompt(root: Path) -> str:
    return (f'Read {seat.brief_path(root)} and continue the top-level session as the '
            'Claude planner seat. Do not restart completed work; follow the open decisions '
            'in the brief, then wait for planning requests in this terminal.')


def context(workers_file: Path, loop_state: Path, seat_root: Path, client, now: float,
            env: dict) -> dict:
    if not seat_root.is_absolute():
        raise ValueError('SPECTRE_SEAT_ROOT must be absolute')
    if not runtime.enabled(env, 'SPECTRE_CLAUDE_ENABLED'):
        raise ValueError('SPECTRE_CLAUDE_ENABLED is off')
    workers = read_json(workers_file).get('workers')
    if not isinstance(workers, dict):
        raise ValueError('worker registry is invalid')
    runtime.validate_workers(workers)
    entry = workers.get('minecraft')
    if not entry or not entry.get('planner'):
        raise ValueError('minecraft planner worker missing')
    state, ws, pending = loop_worker(loop_state)
    snapshot = client.snapshot('minecraft')
    goal = snapshot.get('goal', {}).get('state')
    if goal in {'UNKNOWN', 'ASSIGNING'} or ws.get('planning'):
        raise ValueError('planner_request_in_flight')
    current = seat.load(seat.seat_path(seat_root))
    if current['owner'] == seat.OWNER_CLAUDE:
        raise ValueError('seat_owned_by_claude')
    if not seat.can_handoff(current, now)[0]:
        raise ValueError('handoff_day_cap')
    return {'entry': entry, 'state': state, 'ws': ws, 'pending': pending, 'seat': current,
            'snapshot': snapshot}


def loop_worker(loop_state: Path) -> tuple[dict, dict, dict]:
    state = read_json(loop_state)
    loop_workers = state.get('workers', {})
    ws = loop_workers.get('minecraft', {}) if isinstance(loop_workers, dict) else None
    if not isinstance(ws, dict):
        raise ValueError('loop worker state is invalid')
    pending = ws.get('claude_handoff') or {}
    if not isinstance(pending, dict):
        raise ValueError('pending Claude handoff is invalid')
    return state, ws, pending


def remember(loop_state: Path, info: dict, pending: dict | None) -> None:
    write_json(loop_state, runtime.worker_state(info['state'], 'minecraft',
                                               {**info['ws'], 'claude_handoff': pending}))


def launch(workers_file: Path, loop_state: Path, seat_root: Path, client, *, now=None,
           env=None, run=run_command, scan=inventory.scan, which=shutil.which,
           dry=False) -> dict:
    now = time.time() if now is None else now
    env = dict(os.environ) if env is None else env
    with locked(loop_state.with_suffix('.lock')):
        info = context(workers_file, loop_state, seat_root, client, now, env)
        cwd = info['entry']['cwd']
        if info['pending'].get('terminal'):
            return {'ok': False, 'error': 'claude_terminal_already_created',
                    'terminal': info['pending']['terminal']}
        processes = scan()
        running = [row['pid'] for row in processes
                   if row.get('model') in PLANNER_MODELS and row.get('cwd') == cwd]
        if running:
            return {'ok': False, 'error': 'top_level_agent_already_running', 'pids': running}
        efficient = pins.pick(kimi.orca_terminals(run), processes, cwd, 'efficient')
        if not efficient['ok']:
            return {'ok': False, 'error': 'efficient_pin_required_before_second_terminal'}
        binary = which('claude', path=env.get('PATH'))
        # Planner identity is the executable's basename, so run the `claude` entry point
        # itself rather than the versioned file it may link to.
        if not binary or Path(binary).name != 'claude':
            raise ValueError('Claude Code executable is not on PATH')
        if dry:
            return {'ok': True, 'dry_run': True, 'worktree': cwd, 'command': command(binary)}
        snapshot = info['snapshot']
        brief = seat.write_brief(
            seat.brief_path(seat_root),
            f"Goal ID: {snapshot.get('goal', {}).get('goal_id') or 'unknown'}",
            ['The operator moved the top-level seat to Claude Opus on the Max subscription.'],
            [f"Authoritative state: {snapshot.get('goal', {}).get('state')}"])
        created = orca.create(cwd, TITLE, command(binary), run)
        if not created['ok']:
            return created
        handle = created['handle']
        if not kimi.HANDLE_PATTERN.fullmatch(handle):
            return {'ok': False, 'error': 'orca_handle_missing; inspect terminal list before retry'}
        remember(loop_state, info, {'to': seat.OWNER_CLAUDE,
                                    'origin': planning.identity(snapshot),
                                    'brief': brief['path'], 'terminal': handle,
                                    'launched_at': now})
        return {'ok': True, 'terminal': handle, 'worktree': cwd,
                'next': 'send_prompt_after_observing_input'}


def send_prompt(workers_file: Path, loop_state: Path, seat_root: Path, client, handle: str,
                *, observed_input=False, now=None, env=None, run=run_command,
                scan=inventory.scan) -> dict:
    # A fresh Claude Code tab can open on a trust or bypass-permissions dialog; typing
    # the prompt into one would answer it.
    if not observed_input:
        raise ValueError('explicit observed-input confirmation required')
    if not kimi.HANDLE_PATTERN.fullmatch(handle):
        raise ValueError('invalid terminal handle')
    now = time.time() if now is None else now
    env = dict(os.environ) if env is None else env
    with locked(loop_state.with_suffix('.lock')):
        info = context(workers_file, loop_state, seat_root, client, now, env)
        pending = info['pending']
        if pending.get('terminal') != handle:
            raise ValueError('terminal does not match pending Claude handoff')
        if pending.get('prompt_sent_at'):
            return {'ok': False, 'error': 'claude_prompt_already_sent'}
        if pending.get('prompt_uncertain_at'):
            return {'ok': False, 'error': 'claude_prompt_uncertain; inspect terminal before retry'}
        if not kimi.live_terminal(kimi.orca_terminals(run), scan(), info['entry']['cwd'],
                                  handle, 'claude'):
            return {'ok': False, 'error': 'claude_terminal_unconfirmed'}
        sent = run(['orca-ide', 'terminal', 'send', '--terminal', handle,
                    '--text', cold_start_prompt(seat_root), '--enter', '--json'], timeout=15)
        if not sent.get('ok'):
            if sent.get('uncertain'):
                remember(loop_state, info, {**pending, 'prompt_uncertain_at': now})
            return {'ok': False, 'error': 'claude_prompt_send_unconfirmed'}
        remember(loop_state, info, {**pending, 'prompt_sent_at': now})
        return {'ok': True, 'terminal': handle, 'next': 'confirm_after_observing_reply'}


def confirm(workers_file: Path, loop_state: Path, seat_root: Path, client, handle: str,
            *, observed_ready=False, now=None, env=None, run=run_command,
            scan=inventory.scan) -> dict:
    if not observed_ready:
        raise ValueError('explicit observed-ready confirmation required')
    if not kimi.HANDLE_PATTERN.fullmatch(handle):
        raise ValueError('invalid terminal handle')
    now = time.time() if now is None else now
    env = dict(os.environ) if env is None else env
    with locked(loop_state.with_suffix('.lock')):
        info = context(workers_file, loop_state, seat_root, client, now, env)
        pending = info['pending']
        if pending.get('terminal') != handle or not pending.get('prompt_sent_at'):
            raise ValueError('prompted Claude terminal required')
        if not kimi.live_terminal(kimi.orca_terminals(run), scan(), info['entry']['cwd'],
                                  handle, 'claude'):
            return {'ok': False, 'error': 'claude_terminal_unconfirmed'}
        updated = seat.transition(info['seat'], now, to=seat.OWNER_CLAUDE,
                                  brief=pending.get('brief') or str(seat.brief_path(seat_root)))
        if not updated['ok']:
            return updated
        committed = seat.commit_if_current(seat.seat_path(seat_root), info['seat'],
                                           {**updated['seat'], 'terminal': handle})
        if not committed['ok']:
            return committed
        try:
            kimi.set_planner(workers_file, {'terminal': handle, 'harness': seat.OWNER_CLAUDE,
                                            'model': MODEL, 'provider': PROVIDER})
        except (OSError, ValueError, KeyError):
            return {'ok': False, 'error': 'registry_update_failed',
                    'seat_committed': True, 'terminal': handle}
        try:
            remember(loop_state, info, None)
        except OSError:
            return {'ok': False, 'error': 'loop_state_update_failed',
                    'seat_committed': True, 'terminal': handle}
        return {'ok': True, 'owner': seat.OWNER_CLAUDE, 'terminal': handle}


def cancel(loop_state: Path, *, observed_closed=False, scan=inventory.scan) -> dict:
    """Drop an abandoned handoff once its Claude process is gone; the seat never moved."""
    if not observed_closed:
        raise ValueError('explicit observed-closed confirmation required')
    with locked(loop_state.with_suffix('.lock')):
        state, ws, pending = loop_worker(loop_state)
        if not pending:
            return {'ok': False, 'error': 'no_pending_claude_handoff'}
        handle = pending.get('terminal')
        running = [row['pid'] for row in scan()
                   if handle and row.get('handle') == handle and row.get('model') == 'claude']
        if running:
            return {'ok': False, 'error': 'claude_terminal_still_running', 'terminal': handle,
                    'pids': running}
        remember(loop_state, {'state': state, 'ws': ws}, None)
        return {'ok': True, 'cancelled': handle}


def release(workers_file: Path, provider_state: Path, loop_state: Path, seat_root: Path,
            *, observed_stopped=False, now=None, scan=inventory.scan) -> dict:
    return kimi.release(workers_file, provider_state, loop_state, seat_root,
                        observed_stopped=observed_stopped, now=now, scan=scan,
                        owner=seat.OWNER_CLAUDE)
