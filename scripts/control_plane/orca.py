"""Create Orca terminals the ADE actually renders."""
from __future__ import annotations

from . import tidy
from .io import run as run_command


# `worktree ps` has a row cap and no offset; a page without the worktree is re-read
# once with the reported total as the cap, never with an unbounded one.
MAX_WORKTREES = 1000


def listing(run, limit=None) -> dict:
    argv = ['orca-ide', 'worktree', 'ps', '--json', *(['--limit', str(limit)] if limit else [])]
    listed = run(argv, timeout=15)
    if not listed.get('ok'):
        return {'ok': False, 'error': 'orca_ps_failed'}
    body = listed.get('parsed', {}).get('result')
    rows = body.get('worktrees') if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return {'ok': False, 'error': 'orca_ps_invalid'}
    return {'ok': True, 'rows': rows, 'truncated': bool(body.get('truncated')),
            'total': body.get('totalCount')}


def matching(rows: list, cwd: str) -> list:
    return [row.get('worktreeId') for row in rows
            if isinstance(row, dict) and row.get('path') == cwd and not row.get('isArchived')]


def registered(cwd: str, run=run_command) -> dict:
    page = listing(run)
    if (page['ok'] and page['truncated'] and not matching(page['rows'], cwd)
            and type(page['total']) is int and len(page['rows']) < page['total'] <= MAX_WORKTREES):
        page = listing(run, page['total'])
    if not page['ok']:
        return page
    ids = matching(page['rows'], cwd)
    if len(ids) == 1 and isinstance(ids[0], str) and ids[0]:
        return {'ok': True, 'worktree_id': ids[0]}
    if not ids and page['truncated']:
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
