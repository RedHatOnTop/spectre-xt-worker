"""Launch an operator-requested planner only into a visible Orca terminal."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import time

from . import cline_free, inventory, orca, pins, providers, runtime, seat
from .io import locked, read_json, run as run_command, write_json, write_text


def launch(workers_file: Path, modes: Path, provider_state: Path, *, dry=False,
           proc_root=Path('/proc'), run=run_command, now=None) -> dict:
    now = time.time() if now is None else now
    with locked(provider_state.with_suffix('.launch.lock')):
        processes = inventory.scan(proc_root)
        existing = [row['pid'] for row in processes if row['model'] == 'astra']
        if existing:
            return {'ok': False, 'error': 'astra_already_running', 'pids': existing}
        registry = read_json(workers_file)
        runtime.validate_workers(registry['workers'])
        entry = registry['workers'].get('minecraft')
        if not entry:
            raise ValueError('minecraft worker missing')
        seat_root = Path(os.environ.get('SPECTRE_SEAT_ROOT', provider_state.parent))
        if not seat_root.is_absolute():
            raise ValueError('SPECTRE_SEAT_ROOT must be absolute')
        owner = seat.load(seat.seat_path(seat_root))['owner']
        if owner != seat.OWNER_ASTRA:
            return {'ok': False, 'error': f'seat_owned_by_{owner}'}
        health = read_json(provider_state)
        if now - health.get('checked_at', 0) > 600 or not health.get('ok'):
            if dry:
                return {'ok': False, 'error': 'provider_health_required', 'dry_run': True}
            health = providers.choose(read_json(modes / 'providers.json'), health, now,
                                      lambda row: providers.probe(row, modes),
                                      kimi_fn=lambda: providers.kimi_candidate(os.environ,
                                          lambda: cline_free.status(cline_free.load(), now)))
            write_json(provider_state, health)
        if not health.get('ok') or health.get('cooldown_until', 0) > now:
            return {'ok': False, 'error': 'provider_unavailable'}
        ident = health['id']
        if ident == providers.KIMI_FREE:
            return {'ok': False, 'error': 'kimi_handoff_required'}
        command = ['codex-mode', 'chatgpt'] if ident == 'openai' else ['codex-mode', 'api', ident, 'gpt-6-astra']
        if dry:
            return {'ok': True, 'dry_run': True, 'provider': ident, 'worktree': entry['cwd']}
        switched = run(command, timeout=15, environ={'CODEX_HOME': str(modes.parent)})
        if not switched['ok']:
            return {'ok': False, 'error': 'codex_mode_failed'}
        active = modes / 'active-provider'
        write_text(active, ident + '\n')
        return create_terminal(workers_file, entry, processes, ident, modes, run)


def create_terminal(workers_file, entry, processes, ident, modes, run):
    listed = run(['orca-ide', 'terminal', 'list', '--json'])
    if not listed['ok']:
        return {'ok': False, 'error': 'orca_list_failed'}
    terminals = listed['parsed'].get('result', {}).get('terminals', [])
    efficient = pins.pick(terminals, processes, entry['cwd'], 'efficient')
    if not efficient['ok']:
        return {'ok': False, 'error': 'efficient_pin_required_before_second_terminal'}
    command = f'. {shlex.quote(str(modes / "env.sh"))} && CODEX_HOME={shlex.quote(str(modes.parent))} exec codex -m gpt-6-astra'
    created = orca.create(entry['cwd'], 'astra-planner', command, run)
    if not created['ok']:
        return created
    handle = created['handle']
    with locked(workers_file.with_suffix('.lock')):
        registry = read_json(workers_file)
        current = registry['workers']['minecraft']
        targets = current.get('targets') or {}
        updated = {**current, 'terminal': efficient['handle'],
                   'targets': {**targets, 'efficient': {**targets.get('efficient', {}), 'terminal': efficient['handle']}},
                   'planner': {'terminal': handle, 'harness': seat.OWNER_ASTRA,
                               'model': 'gpt-6-astra', 'provider': ident}}
        write_json(workers_file, {**registry, 'workers': {**registry['workers'], 'minecraft': updated}})
    return {'ok': True, 'terminal': handle, 'provider': ident, 'worktree': entry['cwd'],
            'worktree_id': created['worktree_id']}
