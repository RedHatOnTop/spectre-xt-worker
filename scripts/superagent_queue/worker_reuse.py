"""Preserve a settled native reader without releasing or duplicating its process."""
from pathlib import Path
import hashlib

from orca_runtime import observe
from . import assignment, execution, go_native, session_execution, worker_receipt

ROOT = Path.home() / '.local/state/remote-agent/superagent/executions'
ROUTES = {'dsh-worker': 'clinepass', 'dsh-worker-go': 'opencode-go'}
REUSE_FIELDS = frozenset(('terminal', 'reuse_of', 'terminal_incarnation'))


def proof(cli, request, rows):
    handle = request['terminal']
    matching = [row for row in rows if row.get('agentTerminalHandle') == handle]
    owned = [row for row in matching if row.get('dispatchId') == request['reuse_of']]
    if len(owned) != 1 or any(row.get('dispatchStatus') == 'dispatched' for row in matching):
        raise ValueError('reader reuse requires one settled exact native owner without active work')
    row = owned[0]
    resource = row.get('resource') or {}
    if (row.get('runId') != request['run_id'] or row.get('dispatchStatus') != 'completed'
            or row.get('workerState') != 'succeeded' or resource.get('ownershipState') != 'owned'
            or resource.get('releaseState') not in ('retained', 'not_requested')
            or not isinstance(resource.get('endpointId'), str) or not resource['endpointId']
            or resource.get('ownerDispatchId') != request['reuse_of']
            or resource.get('terminalHandle') != handle
            or resource.get('worktreeId') != request['worktree_id']):
        raise ValueError('reader reuse requires exact completed native custody')
    terminal = observe.query(cli, ['terminal', 'show', '--terminal', handle]).get('terminal')
    if (not isinstance(terminal, dict) or terminal.get('handle') != handle
            or not terminal.get('connected') or not terminal.get('writable')
            or terminal.get('worktreeId') != request['worktree_id']
            or terminal.get('incarnationId') != request['terminal_incarnation']
            or resource.get('endpointIncarnation') != f'{terminal.get("ptyId")}:{terminal.get("incarnationId")}'
            or terminal.get('agentIdentity') != worker_receipt.resolve_native(cli, request['agent'])):
        raise ValueError('reader reuse terminal identity or approved harness changed')
    record = execution.inspect(ROOT, request['reuse_of'])
    expected = {'run_id': request['run_id'], 'task_id': row.get('taskId'),
                'dispatch_id': request['reuse_of'], 'terminal_handle': handle,
                'worktree_id': request['worktree_id'], 'incarnation_id': request['terminal_incarnation'],
                'pane_key': assignment.terminal_pane(terminal), 'route': ROUTES[request['agent']]}
    if (any(record.get('binding', {}).get(key) != value for key, value in expected.items())
            or record.get('returncode') != 0 or record.get('error')):
        raise ValueError('reader reuse lacks its original successful OS execution binding')
    session_execution.exited(record)
    session_execution.absent(record['observer'])
    return {**assignment.coordinator_identity(terminal), 'endpointId': resource['endpointId']}


def stopped_execution(request, task, worker, cli):
    if (worker.get('state') != 'stopped' or worker.get('stage') != 'process_stopped'
            or request['agent'] != 'dsh-worker-go' or not isinstance(task.get('spec'), str)):
        return False
    record = execution.inspect(ROOT, request['retry_of'])
    binding = record.get('binding') or {}
    expected = {'run_id': request['run_id'], 'task_id': request['task_id'],
                'dispatch_id': request['retry_of'], 'worktree_id': request['worktree_id'],
                'route': ROUTES[request['agent']],
                'packet_sha256': hashlib.sha256(task['spec'].encode()).hexdigest()}
    if any(binding.get(key) != value for key, value in expected.items()):
        return False
    session_execution.exited(record)
    session_execution.absent(record['observer'])
    plan = go_native.prepare(ROOT, {**binding, 'dispatch_id': request['launch_id']},
                             {'retry_of_dispatch_id': request['retry_of']}, cli)
    return plan.get('resume') is True


def reviewed_retry(cli, request, task):
    if not request.get('retry_of') or task.get('result'):
        return False
    value = observe.query(cli, ['orchestration', 'worker-show', '--dispatch', request['retry_of'], '--full'])
    dispatch, worker = value.get('dispatch', {}), value.get('worker', {})
    resource = value.get('terminalResource', {})
    timestamp_matches = dispatch.get('completedAt') == task.get('completed_at')
    if task.get('status') in ('failed', 'blocked'):
        latest = observe.query(cli, ['orchestration', 'dispatch-show', '--task', request['task_id']]).get('dispatch', {})
        timestamp_matches = (latest.get('id') == request['retry_of'] and latest.get('status') == 'failed'
                             and latest.get('run_id') == request['run_id'] and latest.get('task_id') == request['task_id'])
    valid = (dispatch.get('id') == request['retry_of'] and dispatch.get('runId') == request['run_id']
            and dispatch.get('taskId') == request['task_id'] and dispatch.get('status') == 'failed'
            and timestamp_matches
            and type(dispatch.get('failureCount')) is int and 0 < dispatch['failureCount'] < 3
            and worker.get('worktreeId') == request['worktree_id']
            and resource.get('releaseState') == 'released' and resource.get('ownershipState') == 'released')
    return valid and (worker.get('stage') == 'agent_readiness' or stopped_execution(request, task, worker, cli))
