"""Record one native-bound OS execution without deriving native Task outcomes."""
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .model import encoded, identifier, timestamp
from .settings import read_private

BINDING_FIELDS = frozenset(('run_id', 'task_id', 'dispatch_id', 'terminal_handle', 'pane_key',
                            'worktree_id', 'workspace_path', 'incarnation_id', 'route', 'packet_sha256'))
ROUTES = frozenset(('clinepass', 'opencode-go'))
PROC_ROOT = Path('/proc')
BOOT_ID = PROC_ROOT / 'sys/kernel/random/boot_id'


def validate(binding, routes=ROUTES):
    encoded(binding)
    if set(binding) != BINDING_FIELDS:
        raise ValueError('execution requires exact native binding fields')
    for key in ('run_id', 'task_id', 'dispatch_id', 'terminal_handle', 'incarnation_id'):
        identifier(binding[key])
    for key in ('pane_key', 'worktree_id', 'workspace_path'):
        value = binding[key]
        if not isinstance(value, str) or not value or '\x00' in value or len(value) > 4096:
            raise ValueError('execution requires bounded pane and workspace identities')
    if not Path(binding['workspace_path']).is_absolute():
        raise ValueError('execution requires an absolute workspace path')
    if (not isinstance(binding['route'], str) or binding['route'] not in routes
            or not isinstance(binding['packet_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', binding['packet_sha256'])):
        raise ValueError('execution requires an approved route and immutable packet digest')
    return binding


def process(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise ValueError('process identity requires a positive PID')
    text = (PROC_ROOT / str(pid) / 'stat').read_text()
    fields = text[text.rindex(')') + 2:].split()
    return {'pid': pid, 'boot_id': BOOT_ID.read_text().strip(), 'start_ticks': int(fields[19]),
            'state': fields[0]}


def observe_process(identity):
    try:
        current = process(identity['pid'])
    except (OSError, ValueError, KeyError):
        return 'unverifiable'
    if any(current[key] != identity.get(key) for key in ('pid', 'boot_id', 'start_ticks')):
        return 'unverifiable'
    return 'exited' if current['state'] == 'Z' else 'live'


def private_directory(root):
    root = Path(root).absolute()
    if root != root.resolve():
        raise ValueError('execution storage cannot contain symlinks')
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    entry = root.stat()
    if entry.st_uid != os.getuid() or stat.S_IMODE(entry.st_mode) != 0o700:
        raise ValueError('execution storage must be private and owned by the caller')
    return root


def persist(directory, name, value):
    body = encoded(value) + '\n'
    fd, temporary = tempfile.mkstemp(prefix=f'.{name}.', dir=directory)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, directory / name)
    finally:
        Path(temporary).unlink(missing_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def begin(root, binding, session=None):
    validate(binding)
    root = private_directory(root)
    directory = root / binding['dispatch_id']
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError('native Dispatch already has an execution intent; inspect the original attempt') from exc
    intent = {'binding': binding, 'intent_id': hashlib.sha256(encoded(binding).encode()).hexdigest(),
              'created_at': timestamp(None), 'observer': process(os.getpid()), 'execution_only': True}
    if session is not None:
        intent = {**intent, 'session': session}
    persist(directory, 'intent.json', intent)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return directory, intent


def run(root, binding, command, env=None, session=None):
    if (not isinstance(command, list) or not command or any(
            not isinstance(word, str) or not word or '\x00' in word for word in command)):
        raise ValueError('execution requires a nonempty command argument list')
    directory, intent = begin(root, binding, session)
    try:
        child = subprocess.Popen(command, env=env, start_new_session=True)
    except OSError as exc:
        result = {**intent, 'finished_at': timestamp(None), 'returncode': 127, 'error': str(exc),
                  'spawned': False}
        persist(directory, 'exit.json', result)
        return result
    try:
        identity = process(child.pid)
    except OSError as exc:
        identity = {'pid': child.pid, 'observation_error': str(exc)}
    started = {'intent_id': intent['intent_id'], 'child': identity, 'started_at': timestamp(None)}
    persist(directory, 'started.json', started)
    code = child.wait()
    result = {**intent, **started, 'finished_at': timestamp(None), 'returncode': code, 'spawned': True}
    persist(directory, 'exit.json', result)
    return result


def inspect(root, dispatch_id):
    identifier(dispatch_id)
    directory = Path(root).absolute() / dispatch_id
    intent = read_private(directory / 'intent.json')
    validate(intent['binding'])
    if intent.get('intent_id') != hashlib.sha256(encoded(intent['binding']).encode()).hexdigest():
        raise ValueError('execution intent identity does not match its native binding')
    if intent['binding']['dispatch_id'] != dispatch_id:
        raise ValueError('execution receipt belongs to another native Dispatch')
    try:
        result = read_private(directory / 'exit.json')
    except FileNotFoundError:
        result = None
    if result is not None:
        if (result.get('intent_id') != intent['intent_id'] or result.get('binding') != intent['binding']
                or result.get('session') != intent.get('session')):
            raise ValueError('execution exit identity does not match the original intent')
        code = result.get('returncode')
        if isinstance(code, bool) or not isinstance(code, int) or not -255 <= code <= 255:
            raise ValueError('execution exit requires an actual process return code')
        if result.get('finished_at') is None or timestamp(result['finished_at']) < timestamp(intent['created_at']):
            raise ValueError('execution exit predates its original intent')
        return {**result, 'intent': intent, 'observation': 'exited'}
    try:
        started = read_private(directory / 'started.json')
    except FileNotFoundError:
        return {'intent': intent, 'observation': 'unverifiable', 'execution_only': True}
    if started.get('intent_id') != intent['intent_id']:
        raise ValueError('execution child identity does not match the original intent')
    return {**started, 'intent': intent, 'observation': observe_process(started['child']),
            'execution_only': True}
