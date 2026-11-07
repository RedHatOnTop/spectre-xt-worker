"""Recover an exact native validation refusal without replacing its launch journal."""
import json
import os
import subprocess

from . import assignment, execution, session_execution, worker_launch


def refusal(intent, report, request_id):
    response = report.get('native_response')
    error = response.get('error') if isinstance(response, dict) else None
    data = error.get('data') if isinstance(error, dict) else None
    if (report.get('state') != 'outcome_unknown' or not isinstance(data, dict)
            or error.get('code') != 'task_not_startable'
            or data.get('orchestrationRequestId') != request_id
            or data.get('originalCommand') != intent['command']):
        raise ValueError('native recovery requires its exact recorded validation refusal and original command')


def replay(directory, intent, report, request_id):
    refusal(intent, report, request_id)
    session_execution.absent(intent['observer'])
    marker = request_id + '.recovery-intent.json'
    execution.persist(directory, marker, {'request_id': request_id, 'observer': execution.process(os.getpid())})
    command = [*intent['command'], '--retry-request', request_id]
    try:
        child = worker_launch.subprocess.run(command, text=True, capture_output=True, check=False,
                                            timeout=worker_launch.START_TIMEOUT_SECONDS)
        try:
            response = json.loads(child.stdout)
        except ValueError:
            response = None
        result = worker_launch.result(intent['request'], response, child.returncode,
                                      child.stdout, child.stderr, intent['native_agent'])
    except (OSError, subprocess.TimeoutExpired) as error:
        result = {'state': 'outcome_unknown', 'launch_id': intent['request']['launch_id'],
                  'dispatch_id': None, 'error': str(error), 'stdout': worker_launch.output(getattr(error, 'stdout', None)),
                  'stderr': worker_launch.output(getattr(error, 'stderr', None))}
    result = {**result, 'native_request_id': request_id, 'original_request_recovery': True}
    if result['state'] in ('ready', 'failed') and response['result']['mutation']['requestId'] != request_id:
        result = {**result, 'state': 'outcome_unknown', 'reason': 'native recovery changed its original request ID'}
    proof = {'request_sha256': worker_launch.request_digest(intent['request']), 'report': result}
    execution.persist(directory, request_id + '.recovery-receipt.json', proof)
    if result['state'] in ('ready', 'failed'):
        execution.persist(directory, 'reconciliation.json', proof)
    return result
