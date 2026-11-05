"""Bind exact offload argv to current native authority and the existing Studio."""
import hashlib
import json
from pathlib import PurePosixPath

from orca_runtime import observe
from . import assignment, offload_receipt
from .model import encoded

ROUTES = frozenset(('lightning',))


def spec(value):
    encoded(value)
    if (set(value) != {'schema_version', 'command', 'artifacts'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ValueError('offload packet requires exact schema, argv and artifact fields')
    offload_receipt.command_digest(value['command'])
    artifacts = value['artifacts']
    if not isinstance(artifacts, list) or any(not isinstance(path, str) or not path or '\x00' in path
            or PurePosixPath(path).is_absolute() or any(part in ('.', '..', '') for part in path.split('/'))
            for path in artifacts) or len(set(artifacts)) != len(artifacts):
        raise ValueError('offload artifacts must be unique safe relative paths')
    return value


def prepare(cli, task_id, dispatch_id, packet, preamble, config, caller, cwd=None):
    binding, preview, dispatch = assignment.prepare(cli, task_id, dispatch_id, packet, 'lightning',
                                                   caller, cwd=cwd, routes=ROUTES)
    assignment.received_preamble(assignment.packet_text(preamble), preview, dispatch)
    text = assignment.packet_text(packet)
    if hashlib.sha256(text.encode()).hexdigest() != binding['packet_sha256']:
        raise ValueError('offload packet changed while validating native authority')
    value = spec(json.loads(text))
    coordinators = [row for row in config['coordinators'] if row['run_id'] == binding['run_id']]
    studios = [row['studio'] for row in config.get('offload', []) if row['run_id'] == binding['run_id']]
    if len(coordinators) != 1 or len(studios) != 1:
        raise ValueError('offload requires one approved native Run and existing Studio')
    from .offload_transport import STUDIO
    if not isinstance(studios[0], str) or not STUDIO.fullmatch(studios[0]):
        raise ValueError('offload requires an exact existing Studio')
    run = observe.show_run(cli, binding['run_id'])
    if run.get('legacy') or run.get('coordinator_handle') != coordinators[0]['terminal_handle']:
        raise ValueError('offload approved coordinator no longer owns this native Run')
    return binding, value, studios[0]


def confirm(cli, binding, packet, preamble, config, caller, cwd=None):
    current, _, studio = prepare(cli, binding['task_id'], binding['dispatch_id'], packet, preamble,
                                 config, caller, cwd=cwd)
    if current != binding:
        raise ValueError('offload native binding changed before remote execution')
    return studio
