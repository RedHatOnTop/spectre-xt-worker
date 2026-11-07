"""Run the existing OpenCode Go account with native authority and immutable OS receipts."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from orca_runtime import observe
from . import assignment, execution, session_execution
from .model import encoded, identifier, timestamp
from .settings import read_private

MODEL = 'opencode-go/glm-5.3-flash'
SESSION = re.compile(r'ses_[A-Za-z0-9_-]{1,120}\Z')


def command(binary, packet, session):
    if session is not None and not SESSION.fullmatch(session):
        raise ValueError('OpenCode Go requires a validated exact session ID')
    args = [str(binary), 'run',
            'Execute the attached current native Task, obey its constraints, and report through its native worker_done command.',
            '--pure', '--model', MODEL, '--agent', 'build', '--format', 'json',
            '--file', str(packet)]
    if session is not None:
        args = [*args, '--session', session]
    return args


def session_id(event):
    value = event.get('sessionID')
    if value is None and isinstance(event.get('part'), dict):
        value = event['part'].get('sessionID')
    if value is not None and (not isinstance(value, str) or not SESSION.fullmatch(value)):
        raise ValueError('OpenCode Go emitted an invalid session ID')
    return value


def session_pointer(root, binding):
    folder = execution.private_directory(Path(root).absolute().parent / 'go-sessions')
    scope = {key: binding[key] for key in session_execution.SCOPE if key != 'packet_sha256'}
    return folder / (hashlib.sha256(encoded(scope).encode()).hexdigest() + '.json')


def failed_before_execution(cli, previous, binding):
    value = observe.query(cli, ['orchestration', 'worker-show', '--dispatch', previous, '--full'])
    dispatch, worker = value.get('dispatch', {}), value.get('worker', {})
    resource = value.get('terminalResource', {})
    if (dispatch.get('id') != previous or dispatch.get('runId') != binding['run_id']
            or dispatch.get('taskId') != binding['task_id'] or dispatch.get('status') != 'failed'
            or worker.get('stage') != 'agent_readiness' or worker.get('worktreeId') != binding['worktree_id']
            or resource.get('releaseState') != 'released'):
        raise ValueError('native retry lacks a released exact pre-execution failure')


def prepare(root, binding, dispatch, cli):
    pointer = session_pointer(root, binding)
    previous = dispatch.get('retry_of_dispatch_id')
    if previous is None:
        if pointer.exists():
            raise ValueError('existing OpenCode Go session requires an exact native retry link')
        return {'session_id': None, 'resume': False, 'backend': 'opencode-go', 'model': MODEL}
    identifier(previous)
    if previous == binding['dispatch_id']:
        raise ValueError('native retry cannot resume its own Dispatch')
    if not (Path(root) / previous).exists():
        failed_before_execution(cli, previous, binding)
        if pointer.exists():
            raise ValueError('unclaimed native retry conflicts with an existing Go session')
        return {'session_id': None, 'resume': False, 'backend': 'opencode-go', 'model': MODEL}
    old = execution.inspect(root, previous)
    if any(old['binding'][key] != binding[key] for key in session_execution.SCOPE):
        raise ValueError('native Go retry changed its original scope or approved route')
    session_execution.exited(old)
    session_execution.absent(old['observer'])
    value = read_private(Path(root) / previous / 'session.json')
    saved = read_private(pointer)
    if (value.get('session_id') != saved.get('session_id') or value.get('model') != MODEL
            or value.get('backend') != 'opencode-go' or saved.get('scope') != {
                key: binding[key] for key in session_execution.SCOPE if key != 'packet_sha256'}):
        raise ValueError('native Go retry lacks its exact preserved session receipt')
    session_id({'sessionID': value['session_id']})
    return {**value, 'resume': True}


def record_session(root, directory, binding, plan, value):
    if plan.get('session_id') is not None and value != plan['session_id']:
        raise ValueError('OpenCode Go changed the original session during resume')
    receipt = {'session_id': value, 'resume': plan['resume'], 'backend': 'opencode-go', 'model': MODEL}
    execution.persist(directory, 'session.json', receipt)
    pointer = session_pointer(root, binding)
    saved = {'session_id': value, 'scope': {
        key: binding[key] for key in session_execution.SCOPE if key != 'packet_sha256'}}
    try:
        execution.persist(pointer.parent, pointer.name, saved)
    except FileExistsError:
        if read_private(pointer) != saved:
            raise ValueError('OpenCode Go session conflicts with its immutable original binding')


def drain(root, directory, binding, plan, child, secrets):
    current, error, output_open = None, None, True
    for line in child.stdout:
        clean = line
        for secret in secrets:
            clean = clean.replace(secret, '<redacted>')
        if output_open:
            try:
                print(clean, end='', flush=True)
            except BrokenPipeError as exc:
                error, output_open = str(exc), False
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        try:
            value = session_id(event)
            if value is not None and current is None:
                record_session(root, directory, binding, plan, value)
                current = value
            elif value is not None and current != value:
                raise ValueError('OpenCode Go emitted conflicting session IDs')
            if event.get('type') == 'error':
                error = 'OpenCode Go emitted an error event; inspect the retained terminal output'
        except (OSError, ValueError) as exc:
            error = str(exc)
    return current, error


def execute(root, binding, dispatch, packet, cli, binary, source=None):
    plan = prepare(root, binding, dispatch, cli)
    directory, intent = execution.begin(root, binding, session=plan)
    args = command(binary, packet, plan['session_id'])
    assignment.confirm(cli, binding, source or packet, os.environ)
    text = assignment.packet_text(packet)
    secrets = tuple(assignment.CAPABILITY.findall(text))
    env = {key: value for key, value in os.environ.items()
           if key not in ('OPENCODE_CONFIG', 'OPENCODE_CONFIG_CONTENT', 'OPENCODE_CONFIG_DIR',
                          'OPENCODE_DB', 'OPENCODE_MODELS_PATH', 'OPENCODE_MODELS_URL')}
    env = {**env, 'OPENCODE_DISABLE_AUTOUPDATE': 'true', 'OPENCODE_DISABLE_TERMINAL_TITLE': 'true'}
    try:
        child = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 text=True, encoding='utf-8', errors='replace', start_new_session=True)
    except OSError as exc:
        result = {**intent, 'returncode': 127, 'spawned': False, 'error': str(exc),
                  'finished_at': timestamp(None)}
        execution.persist(directory, 'exit.json', result)
        return result
    try:
        started = {'child': execution.process(child.pid), 'started_at': timestamp(None)}
        execution.persist(directory, 'started.json', {**started, 'intent_id': intent['intent_id']})
        with child.stdout:
            session, error = drain(root, directory, binding, plan, child, secrets)
    finally:
        try:
            if not child.stdout.closed:
                with child.stdout:
                    for _ in child.stdout:
                        pass
        finally:
            code = child.wait()
    result = {**intent, **started, 'returncode': code, 'spawned': True, 'finished_at': timestamp(None),
              'session_id': session, 'error': error or (None if session else 'Go CLI exited without a session receipt')}
    execution.persist(directory, 'exit.json', result)
    return result
