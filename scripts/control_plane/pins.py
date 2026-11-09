"""Reconcile native terminal pins from process identity, preserving ambiguous pins."""
from __future__ import annotations

from . import seat


def pick(terminals: list[dict], processes: list[dict], cwd: str, role: str) -> dict:
    live = [row for row in terminals if row.get('worktreePath') == cwd
            and row.get('connected') and row.get('writable')]
    handles = {row['handle'] for row in processes if row['cwd'] == cwd and row.get('model') == role}
    matches = [row for row in live if row['handle'] in handles]
    if len(matches) != 1:
        return {'ok': False, 'error': f'{role}: {len(matches)} matching live terminals'}
    return {'ok': True, 'handle': matches[0]['handle']}


def sync(workers: dict, terminals: list[dict], processes: list[dict]) -> tuple[dict, list[dict]]:
    result, changes = {}, []
    for name, entry in workers.items():
        if entry.get('tmux'):
            result = {**result, name: entry}
            continue
        efficient = pick(terminals, processes, entry['cwd'], 'efficient')
        updated = {**entry, 'terminal': efficient['handle']} if efficient['ok'] else dict(entry)
        if efficient['ok'] and entry.get('targets', {}).get('efficient'):
            updated = {**updated, 'targets': {**entry['targets'], 'efficient': {
                **entry['targets']['efficient'], 'terminal': efficient['handle']}}}
        changes = [*changes, {'worker': name, 'role': 'efficient', **efficient}]
        if name == 'minecraft' and entry.get('planner'):
            harness = entry['planner'].get('harness') or seat.OWNER_ASTRA
            role = seat.planner_role(harness)
            planner = (pick(terminals, processes, entry['cwd'], role) if role else
                       {'ok': False, 'error': f'unknown planner harness {harness!r}'})
            if planner['ok']:
                updated = {**updated, 'planner': {**entry['planner'], 'terminal': planner['handle']}}
            changes = [*changes, {'worker': name, 'role': role or 'planner', **planner}]
        result = {**result, name: updated}
    return result, changes


def bind_flash(workers, terminals, processes, handle):
    entry = workers.get('minecraft', {})
    matches = [term for term in terminals if term.get('handle') == handle
               and term.get('worktreePath') == entry.get('cwd')
               and term.get('connected') and term.get('writable')]
    tree = [proc for proc in processes if proc.get('handle') == handle]
    shells = [proc for proc in tree if proc.get('comm') in {'bash', 'sh', 'zsh', 'dash', 'fish'}
              and proc.get('cwd') == entry.get('cwd')]
    if len(matches) != 1 or not shells or any(proc.get('model') for proc in tree):
        raise ValueError('Flash pin must be a live idle shell in the minecraft worktree')
    targets = entry.get('targets') or {}
    flash = {**targets.get('flash', {}), 'terminal': handle,
             'model': 'cline-pass/deepseek-v4.1-flash', 'harness': 'dsh-clinepass',
             'wrapper': '/usr/local/bin/dsh-clinepass', 'profile': 'headless'}
    return {**workers, 'minecraft': {**entry, 'targets': {**targets, 'flash': flash}}}
