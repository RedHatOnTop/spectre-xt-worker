"""Bind durable session recovery to native retry lineage and actual OS exits."""
import hashlib
from pathlib import Path

from . import execution
from .model import encoded, identifier
from .settings import read_private

ROUTE_MODELS = {'clinepass': ('clinepass', 'cline-pass/deepseek-v4.1-flash-1017'),
                'opencode-go': ('opencode-go', 'glm-5.3-flash')}
SCOPE = ('run_id', 'task_id', 'worktree_id', 'workspace_path', 'route', 'packet_sha256')


def session_id(binding):
    identity = {key: binding[key] for key in ('run_id', 'task_id', 'worktree_id', 'workspace_path', 'route')}
    return 'session-native-' + hashlib.sha256(encoded(identity).encode()).hexdigest()


def absent(identity):
    if (not isinstance(identity, dict) or not {'pid', 'boot_id', 'start_ticks'} <= set(identity)
            or identity['boot_id'] != execution.BOOT_ID.read_text().strip()):
        raise ValueError('session recovery requires exact original process identity on the current boot')
    try:
        execution.process(identity['pid'])
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        raise ValueError('original session process cannot be verified') from exc
    raise ValueError('original session PID is still present or reused; recovery is unverifiable')


def exited(record):
    if record.get('observation') != 'exited' or record.get('returncode') is None:
        raise ValueError('session recovery requires a recorded actual OS exit')
    if record.get('spawned') is not True or record['observer']['boot_id'] != execution.BOOT_ID.read_text().strip():
        raise ValueError('session recovery requires a spawned attempt on the current verified boot')
    absent(record.get('child'))


def receipt(root, record):
    plan = record.get('session')
    if not isinstance(plan, dict) or plan.get('session_id') != session_id(record['binding']):
        raise ValueError('original attempt has no verified durable session binding')
    path = Path(root) / record['binding']['dispatch_id'] / 'session.json'
    try:
        value = read_private(path)
    except FileNotFoundError:
        if plan.get('resume') is not False:
            raise ValueError('original resumed session lacks its receipt; recovery is unverifiable')
        absent(record.get('observer'))
        try:
            path.with_name('session.json.claim').lstat()
        except FileNotFoundError:
            return {**plan, 'resume': False}
        raise ValueError('original session claim lacks its receipt; recovery is unverifiable')
    provider, model = ROUTE_MODELS[record['binding']['route']]
    expected = {'session_id': plan['session_id'], 'resumed': plan['resume'],
                'workspace_path': record['binding']['workspace_path'], 'provider': provider, 'model': model}
    if any(value.get(key) != item for key, item in expected.items()):
        raise ValueError('original session receipt changed its workspace, identity or approved route')
    absent(value.get('runtime'))
    return {**plan, 'resume': True}


def prepare(root, binding, dispatch):
    execution.validate(binding)
    previous = dispatch.get('retry_of_dispatch_id')
    if previous is None:
        if Path(root).exists():
            for directory in Path(root).iterdir():
                intent = read_private(directory / 'intent.json')
                if session_id(intent['binding']) == session_id(binding):
                    raise ValueError('existing task session requires an exact native retry link')
        return {'session_id': session_id(binding), 'resume': False,
                'root_dispatch_id': binding['dispatch_id'], 'retry_of_dispatch_id': None}
    identifier(previous)
    if previous == binding['dispatch_id']:
        raise ValueError('native retry cannot resume its own Dispatch')
    record = execution.inspect(root, previous)
    if any(record['binding'][key] != binding[key] for key in SCOPE):
        raise ValueError('native retry changed the original task, workspace, packet or approved route')
    exited(record)
    original = receipt(root, record)
    return {**original, 'retry_of_dispatch_id': previous}
