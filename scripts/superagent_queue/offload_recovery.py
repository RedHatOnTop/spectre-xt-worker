"""Append verified late remote outcomes without rewriting wrapper history."""
import hashlib
from pathlib import Path

from . import execution
from .model import encoded, timestamp
from .settings import read_private

FIELDS = frozenset(('schema_version', 'intent_id', 'wrapper_exit_sha256', 'remote_result', 'recorded_at'))


def wrapper_digest(result):
    return hashlib.sha256(encoded({key: value for key, value in result.items()
                                  if key not in ('observation', 'reconciliation')}).encode()).hexdigest()


def read(directory, result):
    from .offload_receipt import remote
    try:
        record = read_private(directory / 'remote-exit.json')
    except FileNotFoundError:
        return None
    if (not isinstance(record, dict) or set(record) != FIELDS
            or type(record.get('schema_version')) is not int or record['schema_version'] != 1
            or record.get('intent_id') != result['intent_id']
            or record.get('wrapper_exit_sha256') != wrapper_digest(result)
            or record.get('recorded_at') is None):
        raise ValueError('offload reconciliation identity does not match its original wrapper exit')
    timestamp(record['recorded_at'])
    remote(record['remote_result'], result['job_id'], result['command_sha256'])
    return record


def reconcile(root, job_id, now=None):
    from .offload_receipt import inspect, remote
    result = inspect(root, job_id)
    if result['observation'] != 'exited':
        raise ValueError('offload reconciliation requires the recorded actual wrapper exit')
    directory = Path(root).absolute() / job_id
    receipt = remote(read_private(directory / 'command-result.json'), job_id, result['command_sha256'])
    if result.get('remote_observation') == 'exited' and result['remote_result'] != receipt:
        raise ValueError('offload reconciliation conflicts with the original remote result')
    existing = result.get('reconciliation')
    if existing is not None:
        if existing['remote_result'] != receipt:
            raise ValueError('offload reconciliation conflicts with the recorded remote result')
        return existing
    record = {'schema_version': 1, 'intent_id': result['intent_id'],
              'wrapper_exit_sha256': wrapper_digest(result), 'remote_result': receipt,
              'recorded_at': timestamp(now)}
    try:
        execution.persist(directory, 'remote-exit.json', record)
    except FileExistsError:
        existing = read(directory, result)
        if existing['remote_result'] != receipt:
            raise ValueError('offload reconciliation conflicts with the recorded remote result')
        return existing
    return record


def publish(journal, target, job_id, caller, cli, studio=None):
    from orca_runtime import observe
    from . import driver, offload_receipt
    from .target import owner
    current = owner(target, observe.collect(cli, run_ids=(target['run_id'],), include_workers=False))
    if (caller.get('ORCA_TERMINAL_HANDLE') != target['terminal_handle']
            or caller.get('ORCA_PANE_KEY') != current.pane_key):
        raise ValueError('offload reconciliation requires the exact current coordinator pane')
    root = journal.path.parent / 'offloads'
    record = offload_receipt.inspect(root, job_id)
    if record['binding']['run_id'] != target['run_id']:
        raise ValueError('offload receipt belongs to another native Run')
    if studio is not None:
        from .offload_transport import fetch
        proof = fetch(root, job_id, studio)['reconciliation']
    else:
        proof = reconcile(root, job_id)
    queued = driver.ingest_offloads(journal, target)
    return {'job_id': job_id, 'reconciliation': proof, 'queued': queued,
            'native_mutations': False, 'remote_execution': False, 'capacity_released': False}
