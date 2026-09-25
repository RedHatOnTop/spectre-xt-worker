"""Create Orca terminals the ADE actually renders."""
from __future__ import annotations

from . import tidy
from .io import run as run_command


def registered(cwd: str, run=run_command) -> dict:
    listed = run(['orca-ide', 'worktree', 'ps', '--json'], timeout=15)
    if not listed.get('ok'):
        return {'ok': False, 'error': 'orca_ps_failed'}
    body = listed.get('parsed', {}).get('result')
    rows = body.get('worktrees') if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return {'ok': False, 'error': 'orca_ps_invalid'}
    ids = [row.get('worktreeId') for row in rows
           if isinstance(row, dict) and row.get('path') == cwd and not row.get('isArchived')]
    if len(ids) == 1 and isinstance(ids[0], str) and ids[0]:
        return {'ok': True, 'worktree_id': ids[0]}
    if not ids and body.get('truncated'):
        return {'ok': False, 'error': 'orca_ps_truncated'}
    return {'ok': False, 'error': 'orca_worktree_unregistered', 'rows': len(ids)}


def create(cwd: str, title: str, command: str, run=run_command) -> dict:
    """Only a registered worktree renders, and only the active selector run from
    inside it binds to that registration; `path:<dir>` synthesizes an id the UI
    never shows."""
    found = registered(cwd, run)
    if not found['ok']:
        return found
    created = run(['orca-ide', 'terminal', 'create', '--worktree', 'active', '--title', title,
                   '--command', command, '--json'], timeout=15, cwd=cwd)
    if not created.get('ok'):
        return {'ok': False, 'error': ('orca_create_uncertain; inspect terminal list before retry'
                                       if created.get('uncertain') else 'orca_create_failed')}
    result = created.get('parsed', {}).get('result', {})
    terminal = result.get('terminal') or result
    handle = terminal.get('handle')
    if not isinstance(handle, str) or not handle.startswith('term_'):
        return {'ok': False, 'error': 'orca_handle_missing; inspect terminal list before retry'}
    if terminal.get('worktreeId') != found['worktree_id']:
        closed = tidy.close(handle, run)
        return {'ok': False, 'error': 'orca_terminal_invisible', 'handle': handle,
                'closed': closed['ok'], 'worktree_id': terminal.get('worktreeId'),
                'expected': found['worktree_id']}
    return {'ok': True, 'handle': handle, 'worktree_id': found['worktree_id'],
            'surface': terminal.get('surface')}
