"""Observe native Dispatches without classifying provider transcripts."""
import json
import re
import subprocess
import time

QUERY_TIMEOUT = 30
PAGE_LIMIT = 100
COORDINATOR_FRESH_SECONDS = 120
MAX_WORKSPACE_ROWS = 10000


def run_scope(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', value):
        raise ValueError('native run scope requires a bounded identifier')
    return value


def query(cli, args):
    result = subprocess.run([cli, *args, '--json'], capture_output=True, text=True,
                            timeout=QUERY_TIMEOUT, check=False)
    if result.returncode:
        raise RuntimeError(f'{cli} {" ".join(args)} exited {result.returncode}\n'
                           f'{result.stdout}{result.stderr}')
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError(f'Invalid Orca JSON\n{result.stdout}{result.stderr}') from exc
    if not isinstance(payload, dict) or payload.get('ok') is False:
        raise RuntimeError(result.stdout)
    value = payload.get('result')
    if not isinstance(value, dict):
        raise ValueError('Orca response has no result object')
    return value


def collect_workers(cli, run_id=None):
    workers, cursors = [], ()
    while True:
        args = ['orchestration', 'worker-list', '--include-remote', '--limit', str(PAGE_LIMIT), '--full']
        if run_id is not None:
            args = [*args, '--run', run_scope(run_id)]
        page = query(cli, [*args, '--cursor', cursors[-1]] if cursors else args)
        if run_id is not None and (page.get('scope') or {}).get('run') != run_id:
            raise ValueError('Orca worker inventory does not match its requested Run scope')
        if page.get('partialHostErrors') or page.get('warnings'):
            raise RuntimeError(json.dumps(page, ensure_ascii=False))
        if not isinstance(page.get('workers'), list) or not isinstance(page.get('page'), dict):
            raise ValueError('Orca worker inventory lacks current pagination')
        if run_id is not None and any(row.get('runId') != run_id for row in page['workers']):
            raise ValueError('Orca worker row is outside its requested Run scope')
        workers = [*workers, *page['workers']]
        if page['page'].get('hasMore') is False:
            return workers
        cursor = page['page'].get('nextCursor')
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ValueError('Orca pagination cursor missing or repeated')
        cursors = (*cursors, cursor)


def collect_workspaces(cli):
    page = query(cli, ['worktree', 'ps'])
    if page.get('truncated') is True:
        total = page.get('totalCount')
        if isinstance(total, bool) or not isinstance(total, int) or not 1 <= total <= MAX_WORKSPACE_ROWS:
            raise ValueError('Orca workspace inventory is incomplete without a bounded total count')
        page = query(cli, ['worktree', 'ps', '--limit', str(total)])
        if page.get('truncated') is not False:
            raise ValueError('Orca workspace inventory remains incomplete after expansion')
    rows = page.get('worktrees')
    if not isinstance(rows, list):
        raise ValueError('Orca workspace inventory is missing')
    return rows


def collect(cli, run_ids=None, include_workers=True):
    ids = None if run_ids is None else tuple(run_scope(value) for value in run_ids)
    if ids is not None and len(set(ids)) != len(ids):
        raise ValueError('Orca inventory requires unique Run scopes')
    return {
        'runtime': query(cli, ['status']),
        'worktrees': collect_workspaces(cli),
        'terminals': query(cli, ['terminal', 'list']).get('terminals', []),
        'runs': collect_runs(cli) if ids is None else collect_runs(cli, run_ids=ids),
        'workers': ([] if not include_workers else collect_workers(cli) if ids is None
                    else [row for run_id in ids for row in collect_workers(cli, run_id=run_id)]),
    }


def show_run(cli, run_id):
    """Read the current full Run record. The concise view has no coordinator."""
    run_id = run_scope(run_id)
    run = query(cli, ['orchestration', 'run-show', '--id', run_id, '--full']).get('run')
    if not isinstance(run, dict) or run.get('id') != run_id:
        raise ValueError('Orca Run identity does not match its requested scope')
    return run


def collect_runs(cli, run_ids=None):
    if run_ids is not None:
        runs = []
        for run_id in run_ids:
            runs = [*runs, show_run(cli, run_id)]
        return runs
    runs, cursors = [], ()
    while True:
        args = ['orchestration', 'run-list', '--limit', str(PAGE_LIMIT)]
        page = query(cli, [*args, '--cursor', str(cursors[-1])] if cursors else args)
        if not isinstance(page.get('runs'), list):
            raise ValueError('Orca run inventory is missing')
        runs = [*runs, *page['runs']]
        cursor = page.get('nextCursor')
        if cursor is None:
            return runs
        if cursor in cursors:
            raise ValueError('Orca run pagination cursor repeated')
        cursors = (*cursors, cursor)


def active(row):
    return (row.get('terminalState') == 'active'
            and (row.get('projection') or {}).get('outcome') not in ('succeeded', 'failed'))


def current_attempts(rows):
    selected, seen = [], frozenset()
    for index, row in enumerate(rows):
        key = (row.get('runId'), row.get('taskId') or row.get('dispatchId') or index)
        if key not in seen:
            selected = [*selected, row]
            seen = seen | {key}
    return selected


def worker_issues(row, terminals):
    projection = row.get('projection') or {}
    liveness = projection.get('liveness') or {}
    evidence = projection.get('evidence') or {}
    attention = projection.get('attention') or {}
    identity = {'dispatch_id': row.get('dispatchId'), 'run_id': row.get('runId')}
    if projection.get('outcome') == 'succeeded' and row.get('dispatchStatus') == 'completed':
        return []
    if not projection:
        return [{**identity, 'code': 'native_projection_unavailable'}]
    if attention.get('requiresAction'):
        return [{**identity, 'code': 'native_attention', 'projection': projection}]
    if not active(row):
        return []
    if liveness.get('verdict') != 'live':
        return [{**identity, 'code': 'agent_not_proven_live', 'liveness': liveness}]
    if evidence.get('liveStatus') != 'fresh' and liveness.get('source') != 'execution_host':
        return [{**identity, 'code': 'agent_status_not_fresh'}]
    if (projection.get('host') or {}).get('kind') == 'remote':
        return []
    if not any(t.get('handle') == row.get('agentTerminalHandle') and t.get('connected')
               for t in terminals):
        return [{**identity, 'code': 'local_terminal_inventory_mismatch'}]
    return []


def workspace_id(row):
    return row.get('worktreeId') or ((row.get('projection') or {}).get('workspace') or {}).get('id')


def coordinators(data, now):
    result = []
    for run in data.get('runs', []):
        if run.get('legacy'):
            continue
        terminals = [t for t in data.get('terminals', [])
                     if t.get('handle') == run.get('coordinator_handle') and t.get('connected')]
        for terminal in terminals:
            pane = terminal.get('paneKey')
            if not pane and terminal.get('tabId') and terminal.get('leafId'):
                pane = f'{terminal["tabId"]}:{terminal["leafId"]}'
            agents = [a for w in data.get('worktrees', [])
                      if w.get('worktreeId') == terminal.get('worktreeId')
                      for a in w.get('agents', []) if pane and a.get('paneKey') == pane]
            fresh = any(a.get('state') == 'working' and isinstance(a.get('updatedAt'), (int, float))
                        and 0 <= now - a['updatedAt'] / 1000 <= COORDINATOR_FRESH_SECONDS
                        for a in agents)
            result = [*result, {'run_id': run.get('id'), 'terminal_handle': terminal.get('handle'),
                                'worktree_id': terminal.get('worktreeId'), 'proven_working': fresh}]
    return result


def assess(data, required_workspaces, now=None):
    runtime = data.get('runtime', {}).get('runtime', {})
    workers = current_attempts(data.get('workers', []))
    terminals = data.get('terminals', [])
    owners = coordinators(data, time.time() if now is None else now)
    issues = ([] if runtime.get('reachable') and runtime.get('state') == 'ready'
              else [{'code': 'runtime_unavailable'}])
    issues = [*issues, *(issue for row in workers for issue in worker_issues(row, terminals))]
    for path in required_workspaces:
        ids = [w.get('worktreeId') for w in data.get('worktrees', []) if w.get('path') == path]
        runs = [c['run_id'] for c in owners if c['worktree_id'] in ids]
        assigned = [w for w in workers if active(w) and (workspace_id(w) in ids or w.get('runId') in runs)]
        working = any(c['worktree_id'] in ids and c['proven_working'] for c in owners)
        if not assigned and not working:
            issues = [*issues, {'code': 'required_workspace_unassigned', 'path': path}]
    return {
        'ok': not issues,
        'status': 'attention_required' if issues else ('observed' if any(active(w) for w in workers) or any(c['proven_working'] for c in owners) else 'idle'),
        'authority': 'orca-native', 'issues': issues, 'workers': data.get('workers', []), 'coordinators': owners,
        'current_dispatch_ids': [w.get('dispatchId') for w in workers],
        'automatic_terminal_mutations': False,
    }
