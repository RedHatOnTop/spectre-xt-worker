"""Retain one native start operation; uncertainty never permits a second start."""
import hashlib
import json
import os
import subprocess

from . import assignment, execution, worker_receipt
from .model import encoded, identifier
from .settings import read_private

START_TIMEOUT_SECONDS = 90


def output(value):
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''


def request_digest(request):
    return hashlib.sha256(encoded(request).encode()).hexdigest()


def prior(directory, request):
    try:
        intent = read_private(directory / 'intent.json')
    except FileNotFoundError:
        return None
    if intent['request'] != request:
        raise ValueError('launch identity conflicts with its original request')
    try:
        reconciled = read_private(directory / 'reconciliation.json')
    except FileNotFoundError:
        reconciled = None
    if reconciled is not None:
        if reconciled.get('request_sha256') != request_digest(request):
            raise ValueError('native launch reconciliation identity changed')
        return {**reconciled['report'], 'replayed': True}
    try:
        receipt = read_private(directory / 'receipt.json')
    except FileNotFoundError:
        return {'state': 'outcome_unknown', 'launch_id': request['launch_id'], 'replayed': True,
                'reason': 'original native start has no response; reconcile without another start'}
    if receipt.get('request_sha256') != request_digest(request):
        raise ValueError('native launch receipt identity differs from its original request')
    return {**receipt['report'], 'replayed': True}


def unresolved(root):
    pending = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        intent = read_private(directory / 'intent.json')
        if prior(directory, intent['request'])['state'] == 'outcome_unknown':
            pending = [*pending, intent['request']['launch_id']]
    return pending


def unobserved(root, rows):
    waiting = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        intent = read_private(directory / 'intent.json')
        report = prior(directory, intent['request'])
        matching = [row for row in rows if row.get('dispatchId') == report['dispatch_id']
                    and row.get('runId') == intent['request']['run_id']
                    and row.get('taskId') == intent['request']['task_id']]
        if len(matching) != 1:
            waiting = [*waiting, intent['request']['launch_id']]
    return waiting


def matching(request, value, native_agent=None):
    return worker_receipt.matches(request, value, native_agent or request['agent'])


def result(request, response, code, stdout, stderr, native_agent=None):
    value = response.get('result') if isinstance(response, dict) else None
    exact = matching(request, value, native_agent)
    ready = exact and response.get('ok') is True and code == 0 and value['state'] == 'ready'
    failed = exact and response.get('ok') is False and code != 0 and value['state'] == 'failed'
    return {'state': 'ready' if ready else 'failed' if failed else 'outcome_unknown', 'launch_id': request['launch_id'],
            'dispatch_id': value.get('dispatchId') if ready or failed else None, 'exit_code': code,
            'native_response': response, 'stdout': stdout, 'stderr': stderr, 'replayed': False}


def reconcile(directory, cli, request, request_id, owner):
    from orca_runtime import observe
    intent = read_private(directory / 'intent.json')
    if (intent['request'] != request or assignment.coordinator_identity(intent['owner'])
            != assignment.coordinator_identity(owner)):
        raise ValueError('launch reconciliation requires its original coordinator and request')
    record = observe.query(cli, ['orchestration', 'request-show', '--request', identifier(request_id)])
    value = record.get('receipt')
    native_agent = intent.get('native_agent') or worker_receipt.resolve_native(cli, request['agent'])
    if (record.get('requestId') != request_id or record.get('state') != 'completed'
            or record.get('method') != 'orchestration.workerStart' or not matching(request, value, native_agent)
            or value['mutation']['requestId'] != request_id):
        raise ValueError('native launch reconciliation does not match its original operation')
    report = {'state': value['state'], 'launch_id': request['launch_id'], 'dispatch_id': value['dispatchId'],
              'native_request_id': request_id, 'native_receipt': value, 'replayed': True,
              'native_start': False}
    proof = {'request_sha256': request_digest(request), 'report': report}
    try:
        execution.persist(directory, 'reconciliation.json', proof)
    except FileExistsError:
        if read_private(directory / 'reconciliation.json') != proof:
            raise ValueError('native launch reconciliation conflicts with its original proof')
    return report


def submit(root, cli, request, owner, occupied):
    directory = execution.private_directory(root / request['launch_id'])
    native_agent = worker_receipt.resolve_native(cli, request['agent'])
    command = [cli, 'orchestration', 'worker-start', '--task', request['task_id'], '--run', request['run_id'],
               '--from', owner['handle'], '--worktree', 'id:' + request['worktree_id'],
               '--json', '--full']
    command = [*command, '--terminal', request['terminal']] if request.get('terminal') else [
        *command, '--agent', request['agent']]
    if request.get('retry_of'):
        command = [*command, '--retry-of', request['retry_of']]
    intent = {'request': request, 'owner': owner, 'command': command, 'native_agent': native_agent,
              'observer': execution.process(os.getpid())}
    execution.persist(directory, 'intent.json', intent)
    try:
        child = subprocess.run(command, text=True, capture_output=True, check=False, timeout=START_TIMEOUT_SECONDS)
        try:
            response = json.loads(child.stdout)
        except ValueError:
            response = None
        report = result(request, response, child.returncode, child.stdout, child.stderr, native_agent)
    except (OSError, subprocess.TimeoutExpired) as error:
        report = {'state': 'outcome_unknown', 'launch_id': request['launch_id'], 'dispatch_id': None,
                  'exit_code': None, 'native_response': None, 'stdout': output(getattr(error, 'stdout', None)),
                  'stderr': output(getattr(error, 'stderr', None)), 'error': str(error), 'replayed': False}
    report = {**report, 'occupied_before': occupied}
    execution.persist(directory, 'receipt.json', {'request_sha256': request_digest(request), 'report': report})
    return report
