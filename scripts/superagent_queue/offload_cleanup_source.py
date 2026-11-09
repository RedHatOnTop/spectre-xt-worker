"""Publish immutable cleanup and resource receipts separately from earlier execution evidence."""
import hashlib
from pathlib import Path

from . import offload_artifact, offload_cleanup, offload_recovery, offload_transport
from .model import encoded
from .settings import read_private


def read(root, record):
    directory = Path(root).absolute() / record['job_id'] / 'cleanup'
    try:
        finished = read_private(directory / 'exit.json')
    except FileNotFoundError:
        return None
    intent = read_private(directory / 'intent.json')
    if (not isinstance(intent, dict) or set(intent) != {'job_id', 'review', 'transport', 'wrapper_exit_sha256', 'request'}
            or intent['job_id'] != record['job_id']
            or intent['wrapper_exit_sha256'] != offload_recovery.wrapper_digest(record)
            or not isinstance(finished, dict) or set(finished) != {'intent_sha256', 'receipt'}
            or finished['intent_sha256'] != hashlib.sha256(encoded(intent).encode()).hexdigest()):
        raise ValueError('cleanup source does not match its original native execution')
    transport = offload_transport.location(directory.parent, record, intent['transport']['studio'])
    if transport != intent['transport']:
        raise ValueError('cleanup source transport differs from its original job')
    identity = {key: value for key, value in intent.items() if key != 'request'}
    request = {'schema_version': 1,
               'cleanup_id': 'cleanup-native-' + hashlib.sha256(encoded(identity).encode()).hexdigest(),
               'job_id': record['job_id'], 'command_sha256': record['command_sha256'],
               'remote_dir': transport['remote_dir'], 'remote_result_sha256': hashlib.sha256(
                   encoded(offload_artifact.remote_result(record)).encode()).hexdigest()}
    if encoded(intent['request']) != encoded(request):
        raise ValueError('cleanup source request differs from its original operation')
    offload_cleanup.result(finished['receipt'], intent)
    return intent, finished


def events(root, record):
    value = read(root, record)
    if value is None:
        return ()
    intent, finished = value
    sha = hashlib.sha256(encoded(finished).encode()).hexdigest()
    content = {**record['binding'], 'job_id': record['job_id'], 'admission': 'review_only',
               'cleanup_sha256': sha, 'remote_tree_removed': True, 'native_mutations': False,
               'capacity_released': False, 'receipt': finished['receipt']}
    event = {'source': 'offload-cleanup', 'event_id': 'cleanup-' + record['intent_id'], 'content': content}
    try:
        resources = read_private(Path(root) / record['job_id'] / 'resources/release.json')
    except FileNotFoundError:
        return (event,)
    capacity = read_private(Path(root) / record['job_id'] / 'capacity.json')
    expected = {'capacity_sha256': hashlib.sha256(encoded(capacity).encode()).hexdigest(), 'cleanup_sha256': sha}
    if (not isinstance(resources, dict) or resources.get('intent') != expected
            or resources.get('capacity_released') is not True or resources.get('native_mutations') is not False
            or resources.get('studio_state') not in ('stopped', 'retained_shared', 'retained_external')):
        raise ValueError('resource source does not match its exact cleanup and capacity release')
    resource = {'source': 'offload-resource', 'event_id': 'resource-' + record['intent_id'],
                'content': {**content, 'capacity_released': True, 'resources': resources}}
    return event, resource
