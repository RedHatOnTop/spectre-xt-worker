"""Bound native implementation launches using current Orca resource authority."""
from pathlib import Path

from orca_runtime import observe
from . import assignment, execution, worker_inventory, worker_launch, worker_reuse
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
    if not REQUEST_FIELDS <= set(request) or set(request) - REQUEST_FIELDS - {'retry_of'} - worker_reuse.REUSE_FIELDS:
        raise ValueError('worker launch requires exact native request fields')
    for field in ('launch_id', 'run_id', 'task_id'):
        identifier(request[field])
    if 'retry_of' in request:
        identifier(request['retry_of'])
    reuse = set(request) & worker_reuse.REUSE_FIELDS
    if reuse and reuse != worker_reuse.REUSE_FIELDS:
        raise ValueError('reader reuse requires the exact terminal, incarnation and previous Dispatch')
    for field in reuse:
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
        resource = worker_inventory.resource(row, rows)
        released = resource.get('releaseState') == 'released' and (row.get('terminalState') == 'released'
            or (row.get('resource') is None and resource.get('ownershipState') == 'released'
                and resource.get('originDispatchId') == row.get('dispatchId')))
        if released and row.get('dispatchStatus') != 'dispatched':
            continue
        handle = resource.get('terminalHandle') or row.get('agentTerminalHandle')
        identity = (resource.get('endpointId'), handle or identifier(row.get('dispatchId')))
        occupied = {**occupied, identity: resource.get('worktreeId')}
    return occupied


def ready_task(cli, request):
    command = ['orchestration', 'task-list', '--run', request['run_id'], '--full']
    page = observe.query(cli, command if request.get('retry_of') else [*command, '--ready'])
    if page.get('runId') != request['run_id'] or not isinstance(page.get('tasks'), list):
        raise ValueError('native task readiness scope cannot be confirmed')
    tasks = [row for row in page['tasks'] if row.get('id') == request['task_id'] and row.get('run_id') == request['run_id']]
    statuses = ('failed', 'blocked') if request.get('retry_of') else ('ready',)
    if len(tasks) != 1 or tasks[0].get('status') not in statuses:
        raise ValueError('implementation launch requires its exact ready native Task')
    if (request.get('retry_of') or tasks[0].get('result') or tasks[0].get('completed_at')) and not worker_reuse.reviewed_retry(cli, request, tasks[0]):
        raise ValueError('native Task has prior result evidence; coordinator review is required before launch')


def recover(root, cli, policy, run_id, launch_id, request_id, caller, cwd):
    from . import worker_recovery
    from .settings import read_private
    validate_policy(policy)
    for value in (run_id, launch_id, request_id):
        identifier(value)
    path = execution.private_directory(Path(root).absolute() / run_id)
    with lock(path / 'start.lock'):
        directory = path / launch_id
        intent = read_private(directory / 'intent.json')
        request = intent['request']
        validate_request(request, policy)
        current = owner(cli, request, caller, cwd)
        if (request['run_id'] != run_id or request['launch_id'] != launch_id
                or assignment.coordinator_identity(intent['owner']) != current):
            raise ValueError('native recovery requires its original operation and coordinator')
        report = worker_launch.prior(directory, request)
        if report['state'] != 'outcome_unknown':
            return report
        record = observe.query(cli, ['orchestration', 'request-show', '--request', request_id])
        if record.get('state') == 'completed':
            return worker_launch.reconcile(directory, cli, request, request_id, current)
        if record.get('state') != 'absent' or record.get('requestId') != request_id:
            raise ValueError('native recovery requires its original request readback; pending work stays owned')
        worker_recovery.refusal(intent, report, request_id)
        if worker_launch.unresolved(path) != [launch_id]:
            raise ValueError('other unknown native starts prevent validation recovery')
        rows = observe.collect_workers(cli, run_id=run_id)
        if not request.get('terminal'):
            raise ValueError('validation recovery requires the exact retained reader')
        reuse = worker_reuse.proof(cli, request, rows)
        if len(occupancy(rows, run_id)) > policy['max_workers']:
            raise ValueError('native recovery cannot exceed approved worker capacity')
        reused_key = (reuse['endpointId'], request['terminal'])
        if any(workspace is None or (key != reused_key and workspace == request['worktree_id'])
               for key, workspace in occupancy(rows, run_id).items()):
            raise ValueError('native recovery cannot overlap an unknown or existing workspace owner')
        ready_task(cli, request)
        if current != owner(cli, request, caller, cwd) or reuse != worker_reuse.proof(
                cli, request, observe.collect_workers(cli, run_id=run_id)):
            raise ValueError('native owner changed before original-request recovery')
        return worker_recovery.replay(directory, intent, report, request_id)


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
    reuse = worker_reuse.proof(cli, request, rows) if request.get('terminal') else None
    if len(occupied) > policy['max_workers'] or (len(occupied) == policy['max_workers'] and reuse is None):
        return {'state': 'capacity_wait', 'occupied': len(occupied), 'limit': policy['max_workers'], 'native_start': False}
    workspaces = observe.collect_workspaces(cli)
    if not any(row.get('worktreeId') == request['worktree_id'] for row in workspaces):
        raise ValueError('implementation workspace is not registered with Orca')
    reused_key = (reuse['endpointId'], request['terminal']) if reuse is not None else None
    conflicts = tuple(workspace for key, workspace in occupied.items()
                      if key != reused_key and workspace == request['worktree_id'])
    if (request['worktree_id'] == current['worktreeId'] or conflicts
            or any(value is None for value in occupied.values())):
        return {'state': 'workspace_wait', 'native_start': False}
    ready_task(cli, request)
    fresh = owner(cli, request, caller, cwd)
    if fresh != current:
        raise ValueError('native coordinator identity changed before worker launch')
    if reuse is not None and worker_reuse.proof(cli, request, observe.collect_workers(cli, run_id=request['run_id'])) != reuse:
        raise ValueError('native reader identity changed before reuse')
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
