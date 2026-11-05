"""Bound native implementation launches using current Orca resource authority."""
from pathlib import Path

from orca_runtime import observe
from . import assignment, execution, worker_launch
from .model import encoded, identifier
from .settings import lock

AGENTS = frozenset(('dsh-worker', 'dsh-worker-go'))
REQUEST_FIELDS = frozenset(('launch_id', 'run_id', 'task_id', 'agent', 'worktree_id'))


def validate_policy(policy):
    encoded(policy)
    if (set(policy) != {'schema_version', 'max_workers', 'agents'} or type(policy['schema_version']) is not int
            or policy['schema_version'] != 1 or type(policy['max_workers']) is not int
            or not 1 <= policy['max_workers'] <= 2 or not isinstance(policy['agents'], list)
            or not policy['agents'] or any(not isinstance(agent, str) or agent not in AGENTS for agent in policy['agents'])
            or len(set(policy['agents'])) != len(policy['agents'])):
        raise ValueError('worker policy requires schema 1, at most two workers and approved DSH agents')
    return policy


def validate_request(request, policy):
    encoded(request)
    if set(request) != REQUEST_FIELDS:
        raise ValueError('worker launch requires exact native request fields')
    for field in ('launch_id', 'run_id', 'task_id'):
        identifier(request[field])
    if not isinstance(request['agent'], str) or request['agent'] not in policy['agents']:
        raise ValueError('worker launch agent is not permitted by the approved policy')
    if (not isinstance(request['worktree_id'], str) or not request['worktree_id']
            or '\x00' in request['worktree_id'] or len(request['worktree_id']) > 4096):
        raise ValueError('worker launch requires its exact registered workspace')


def owner(cli, request, caller, cwd):
    run = observe.show_run(cli, request['run_id'])
    if run.get('legacy') or run.get('coordinator_handle') != caller.get('ORCA_TERMINAL_HANDLE'):
        raise ValueError('worker launch requires the exact current native coordinator')
    return assignment.coordinator_identity(assignment.current_terminal(cli, caller, cwd))


def occupancy(rows, run_id):
    occupied = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('runId') != run_id:
            raise ValueError('worker inventory is outside its current Run')
        resource = row.get('resource') or {}
        released = row.get('terminalState') == 'released' and resource.get('releaseState') == 'released'
        if released and row.get('dispatchStatus') != 'dispatched':
            continue
        handle = resource.get('terminalHandle') or row.get('agentTerminalHandle')
        identity = (resource.get('endpointId'), handle or identifier(row.get('dispatchId')))
        occupied = {**occupied, identity: resource.get('worktreeId')}
    return occupied


def ready_task(cli, request):
    page = observe.query(cli, ['orchestration', 'task-list', '--run', request['run_id'], '--ready', '--full'])
    if page.get('runId') != request['run_id'] or not isinstance(page.get('tasks'), list):
        raise ValueError('native task readiness scope cannot be confirmed')
    tasks = [row for row in page['tasks'] if row.get('id') == request['task_id'] and row.get('run_id') == request['run_id']]
    if len(tasks) != 1 or tasks[0].get('status') != 'ready':
        raise ValueError('implementation launch requires its exact ready native Task')
    if tasks[0].get('result') or tasks[0].get('completed_at'):
        raise ValueError('native Task has prior result evidence; coordinator review is required before launch')


def launch_locked(root, cli, policy, request, caller, cwd):
    original = worker_launch.prior(root / request['launch_id'], request)
    if original is not None:
        owner(cli, request, caller, cwd)
        return original
    pending = worker_launch.unresolved(root)
    if pending:
        return {'state': 'reconciliation_required', 'launch_ids': pending, 'native_start': False}
    current = owner(cli, request, caller, cwd)
    rows = observe.collect_workers(cli, run_id=request['run_id'])
    unobserved = worker_launch.unobserved(root, rows)
    if unobserved:
        return {'state': 'reconciliation_required', 'launch_ids': unobserved, 'native_start': False}
    occupied = occupancy(rows, request['run_id'])
    if len(occupied) >= policy['max_workers']:
        return {'state': 'capacity_wait', 'occupied': len(occupied), 'limit': policy['max_workers'], 'native_start': False}
    workspaces = observe.collect_workspaces(cli)
    if not any(row.get('worktreeId') == request['worktree_id'] for row in workspaces):
        raise ValueError('implementation workspace is not registered with Orca')
    if (request['worktree_id'] == current['worktreeId'] or request['worktree_id'] in occupied.values()
            or any(value is None for value in occupied.values())):
        return {'state': 'workspace_wait', 'native_start': False}
    ready_task(cli, request)
    fresh = owner(cli, request, caller, cwd)
    if fresh != current:
        raise ValueError('native coordinator identity changed before worker launch')
    return worker_launch.submit(root, cli, request, current, len(occupied))


def start(root, cli, policy, request, caller, cwd):
    validate_policy(policy)
    validate_request(request, policy)
    path = execution.private_directory(Path(root).absolute() / request['run_id'])
    try:
        with lock(path / 'start.lock'):
            return launch_locked(path, cli, policy, request, caller, cwd)
    except BlockingIOError:
        return {'state': 'launch_busy', 'native_start': False}


def reconcile(root, cli, policy, run_id, launch_id, request_id, caller, cwd):
    validate_policy(policy)
    for value in (run_id, launch_id, request_id):
        identifier(value)
    path = execution.private_directory(Path(root).absolute() / run_id)
    try:
        with lock(path / 'start.lock'):
            directory = path / launch_id
            from .settings import read_private
            intent = read_private(directory / 'intent.json')
            request = intent['request']
            validate_request(request, policy)
            if request['run_id'] != run_id or request['launch_id'] != launch_id:
                raise ValueError('launch reconciliation identity differs from its original operation')
            current = owner(cli, request, caller, cwd)
            return worker_launch.reconcile(directory, cli, request, request_id, current)
    except BlockingIOError:
        return {'state': 'launch_busy', 'native_start': False}
