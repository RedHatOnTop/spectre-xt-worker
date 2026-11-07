"""Execute only receipt-backed remote cleanup and reconcile lost responses without blind replay."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from uuid import uuid4

from . import execution, offload_artifact, offload_receipt, offload_recovery, offload_review, offload_transport, settings
from .model import encoded, identifier, MAX_JSON_BYTES, timestamp
from .settings import read_private

HELPER = Path(__file__).resolve().parents[1] / 'offload-cleanup.py'
HELPER_SHA256 = '7f9f77fe286d5cecfb7768aef06031c650b6cc5a5f07bf5f478bb01da2b3d7bc'
RESULT_FIELDS = frozenset(('schema_version', 'state', 'request_sha256', 'tree_removed', 'studio_stopped', 'finished_at'))


def query(command, request):
    return subprocess.run(command, input=encoded(request), text=True, capture_output=True, timeout=300, check=False)


def command(studio, action):
    if HELPER.absolute() != HELPER.resolve() or hashlib.sha256(HELPER.read_bytes()).hexdigest() != HELPER_SHA256:
        raise ValueError('remote cleanup helper differs from reviewed bytes')
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'LogLevel=ERROR', studio,
            'python3 -c ' + shlex.quote(HELPER.read_text()), action]


def result(value, intent):
    if (not isinstance(value, dict) or set(value) != RESULT_FIELDS
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('request_sha256') != hashlib.sha256(encoded(intent['request']).encode()).hexdigest()
            or value.get('state') != 'completed' or value.get('tree_removed') is not True
            or value.get('studio_stopped') is not False or value.get('finished_at') is None):
        raise ValueError('remote cleanup receipt does not match its original operation')
    timestamp(value['finished_at'])
    return value


def observe_remote(directory, intent, action):
    response = query(command(intent['transport']['studio'], action), intent['request'])
    if response.returncode:
        raise RuntimeError(f'remote cleanup {action} exited {response.returncode}\nstdout:\n{response.stdout}\nstderr:\n{response.stderr}')
    if len(response.stdout.encode()) > MAX_JSON_BYTES:
        raise ValueError('remote cleanup response exceeds the byte limit')
    value = json.loads(response.stdout)
    if not isinstance(value, dict):
        raise ValueError('remote cleanup response must be an exact object')
    if action == 'inspect' and value.get('state') in ('pending', 'absent'):
        if value.get('tree_removed') is not False:
            raise ValueError('remote cleanup inspection has invalid removal evidence')
        if value.get('state') == 'pending' and value.get('resume_ready') is True:
            if value.get('request_sha256') != hashlib.sha256(encoded(intent['request']).encode()).hexdigest():
                raise ValueError('remote cleanup resume does not identify its original claim')
            return observe_remote(directory, intent, 'apply')
        return None
    value = result(value, intent)
    execution.persist(directory, 'exit.json', {'intent_sha256': hashlib.sha256(encoded(intent).encode()).hexdigest(),
                                             'receipt': value})
    return value


def perform(directory, intent, reconcile):
    try:
        existing = read_private(directory / 'exit.json')
    except FileNotFoundError:
        pass
    else:
        if not isinstance(existing, dict) or existing.get('intent_sha256') != hashlib.sha256(encoded(intent).encode()).hexdigest():
            raise ValueError('cleanup result differs from its original intent')
        return result(existing['receipt'], intent)
    if (directory / 'started.json').exists():
        started = read_private(directory / 'started.json')
        if (not isinstance(started, dict) or set(started) != {'intent_sha256', 'observer'}
                or started.get('intent_sha256') != hashlib.sha256(encoded(intent).encode()).hexdigest()):
            raise ValueError('cleanup started receipt does not identify the original intent')
        if not reconcile:
            raise ValueError('cleanup requires explicit reconciliation of its original remote operation')
    if reconcile:
        return observe_remote(directory, intent, 'inspect')
    execution.persist(directory, 'started.json', {'intent_sha256': hashlib.sha256(encoded(intent).encode()).hexdigest(),
                                                'observer': execution.process(os.getpid())})
    return observe_remote(directory, intent, 'apply')


def publish(journal, target, job_id, data, caller, cli, studio, reconcile=False):
    identifier(job_id)
    root = journal.path.parent / 'offloads'
    directory = execution.private_directory(root / job_id / 'cleanup')
    with settings.lock(directory / '.lock'):
        review = offload_review.prepare(journal, target, job_id, data, caller, cli)
        record = offload_receipt.inspect(root, job_id)
        transport = offload_transport.location(root / job_id, record, studio)
        remote = offload_artifact.remote_result(record)
        identity = {'job_id': job_id, 'review': review, 'transport': transport,
                    'wrapper_exit_sha256': offload_recovery.wrapper_digest(record)}
        cleanup_id = 'cleanup-native-' + hashlib.sha256(encoded(identity).encode()).hexdigest()
        request = {'schema_version': 1, 'cleanup_id': cleanup_id, 'job_id': job_id,
                   'command_sha256': record['command_sha256'], 'remote_dir': transport['remote_dir'],
                   'remote_result_sha256': hashlib.sha256(encoded(remote).encode()).hexdigest()}
        intent = {**identity, 'request': request}
        try:
            execution.persist(directory, 'intent.json', intent)
        except FileExistsError:
            if encoded(read_private(directory / 'intent.json')) != encoded(intent):
                raise ValueError('offload review or transport changed from its original cleanup intent')
        try:
            receipt = perform(directory, intent, reconcile)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            try:
                execution.persist(directory, 'failure-' + str(uuid4()) + '.json',
                    {'error': str(exc), 'native_mutations': False, 'capacity_released': False})
            except (OSError, ValueError) as evidence_error:
                exc.add_note(f'cleanup failure evidence could not be saved: {evidence_error}')
            raise
        resources = None
        if receipt is not None and (root / job_id / 'capacity.json').exists():
            from .offload_capacity import release
            resources = release(root, job_id, read_private(directory / 'exit.json'))
        return {'job_id': job_id, 'state': 'completed' if receipt else 'reconciling',
                'remote_tree_removed': receipt is not None, 'receipt': receipt,
                'capacity_released': resources is not None and resources['capacity_released'],
                'resources': resources, 'native_mutations': False}
