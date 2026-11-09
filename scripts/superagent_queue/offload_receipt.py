"""Keep wrapper execution separate from verified remote command completion."""
import hashlib
import os
from pathlib import Path
import subprocess

from . import execution, offload_recovery
from .model import encoded, identifier, timestamp
from .settings import read_private

SCHEMA_VERSION = 1
REMOTE_FIELDS = frozenset(('schema_version', 'job_id', 'command_sha256', 'returncode',
                           'spawned', 'started_at', 'finished_at', 'runtime'))


def binding(value):
    return execution.validate(value, routes=frozenset(('lightning',)))


def command_digest(command):
    if (not isinstance(command, list) or not command or any(
            not isinstance(word, str) or not word or '\x00' in word for word in command)):
        raise ValueError('offload command requires bounded nonempty argument words')
    return hashlib.sha256(encoded({'command': command}).encode()).hexdigest()


def returncode(value):
    if isinstance(value, bool) or not isinstance(value, int) or not -255 <= value <= 255:
        raise ValueError('offload exit requires an actual process return code')
    return value


def remote(value, job_id, digest):
    encoded(value)
    if (set(value) != REMOTE_FIELDS or type(value.get('schema_version')) is not int
            or value.get('schema_version') != SCHEMA_VERSION
            or value.get('job_id') != job_id or value.get('command_sha256') != digest):
        raise ValueError('remote receipt identity does not match the original offload command')
    returncode(value['returncode'])
    if (value['spawned'] is not True or value['started_at'] is None or value['finished_at'] is None
            or timestamp(value['finished_at']) < timestamp(value['started_at'])):
        raise ValueError('remote command receipt lacks a spawned chronological execution')
    process = value['runtime']
    if (not isinstance(process, dict) or set(process) != {'pid', 'boot_id', 'start_ticks'}
            or isinstance(process.get('pid'), bool) or not isinstance(process.get('pid'), int) or process['pid'] < 1
            or not isinstance(process.get('boot_id'), str) or not process['boot_id']
            or isinstance(process.get('start_ticks'), bool) or not isinstance(process.get('start_ticks'), int)
            or process['start_ticks'] < 0):
        raise ValueError('remote command receipt lacks its exact process identity')
    return value


def begin(root, job_id, native_binding, command, authority=None):
    identifier(job_id)
    binding(native_binding)
    digest = command_digest(command)
    root = execution.private_directory(root)
    directory = root / job_id
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError('offload job already has an intent; inspect the original attempt') from exc
    identity = {'job_id': job_id, 'binding': native_binding, 'command_sha256': digest}
    intent = {**identity, 'schema_version': SCHEMA_VERSION, 'command': command,
              'intent_id': hashlib.sha256(encoded(identity).encode()).hexdigest(),
              'created_at': timestamp(None), 'observer': execution.process(os.getpid()), 'execution_only': True}
    if authority is not None:
        intent = {**intent, 'authority': authority}
    execution.persist(directory, 'intent.json', intent)
    return directory, intent


def remote_result(directory, intent):
    try:
        value = read_private(directory / 'command-result.json')
        result = remote(value, intent['job_id'], intent['command_sha256'])
        return {'remote_observation': 'exited', 'remote_result': result}
    except (OSError, ValueError) as exc:
        return {'remote_observation': 'unverifiable', 'remote_result': None, 'remote_error': str(exc)}


def publish_start(child):
    accepted = True
    try:
        child.stdin.write('native-offload-started\n')
        child.stdin.flush()
    except BrokenPipeError:
        accepted = False
    try:
        child.stdin.close()
    except BrokenPipeError:
        accepted = False
    return accepted


def run(root, job_id, native_binding, command, wrapper, env=None, authority=None):
    command_digest(wrapper)
    directory, intent = begin(root, job_id, native_binding, command, authority=authority)
    environment = {**(os.environ if env is None else env), 'SPECTRE_OFFLOAD_JOB_ID': job_id,
                   'SPECTRE_OFFLOAD_COMMAND_SHA256': intent['command_sha256'],
                   'SPECTRE_OFFLOAD_COMMAND_RECEIPT': str(directory / 'command-result.json')}
    started = {}
    with (directory / 'stdout.log').open('x') as stdout, (directory / 'stderr.log').open('x') as stderr:
        for output in (stdout, stderr):
            os.fchmod(output.fileno(), 0o600)
        try:
            child = subprocess.Popen(wrapper, env=environment, stdout=stdout, stderr=stderr,
                                     stdin=subprocess.PIPE if authority is not None else subprocess.DEVNULL,
                                     start_new_session=True, text=True)
        except OSError as exc:
            result = {**intent, 'finished_at': timestamp(None), 'returncode': 127,
                      'spawned': False, 'error': str(exc), **remote_result(directory, intent)}
            execution.persist(directory, 'exit.json', result)
            return result
        try:
            identity = execution.process(child.pid)
        except OSError as exc:
            identity = {'pid': child.pid, 'observation_error': str(exc)}
        started = {'intent_id': intent['intent_id'], 'child': identity, 'started_at': timestamp(None)}
        try:
            execution.persist(directory, 'started.json', started)
        except OSError:
            if authority is not None:
                try:
                    child.stdin.close()
                finally:
                    child.wait()
            raise
        if authority is not None and not publish_start(child):
            code = child.wait()
            result = {**intent, **started, 'finished_at': timestamp(None), 'returncode': code,
                      'spawned': True, 'error': 'wrapper exited before durable start signal',
                      **remote_result(directory, intent)}
            execution.persist(directory, 'exit.json', result)
            return result
        code = child.wait()
        for output in (stdout, stderr):
            output.flush()
            os.fsync(output.fileno())
    result = {**intent, **started, 'finished_at': timestamp(None), 'returncode': code,
              'spawned': True, **remote_result(directory, intent)}
    execution.persist(directory, 'exit.json', result)
    return result


def inspect(root, job_id):
    identifier(job_id)
    directory = Path(root).absolute() / job_id
    intent = read_private(directory / 'intent.json')
    binding(intent['binding'])
    digest = command_digest(intent['command'])
    identity = {'job_id': job_id, 'binding': intent['binding'], 'command_sha256': digest}
    if (intent.get('schema_version') != SCHEMA_VERSION or intent.get('job_id') != job_id
            or intent.get('command_sha256') != digest
            or intent.get('intent_id') != hashlib.sha256(encoded(identity).encode()).hexdigest()):
        raise ValueError('offload intent identity does not match its original binding')
    try:
        result = read_private(directory / 'exit.json')
    except FileNotFoundError:
        result = None
    if result is not None:
        if any(result.get(key) != value for key, value in intent.items()):
            raise ValueError('offload exit identity does not match the original intent')
        returncode(result.get('returncode'))
        if (result.get('finished_at') is None or timestamp(result['finished_at']) < timestamp(intent['created_at'])
                or not isinstance(result.get('spawned'), bool)):
            raise ValueError('offload exit predates or lacks its actual attempt')
        if result.get('remote_observation') == 'exited':
            remote(result.get('remote_result'), job_id, digest)
        elif result.get('remote_observation') != 'unverifiable' or result.get('remote_result') is not None:
            raise ValueError('offload exit has an invalid remote observation')
        return {**result, 'observation': 'exited', 'reconciliation': offload_recovery.read(directory, result)}
    try:
        started = read_private(directory / 'started.json')
    except FileNotFoundError:
        return {**intent, 'observation': 'unverifiable'}
    if started.get('intent_id') != intent['intent_id']:
        raise ValueError('offload child identity does not match its original intent')
    return {**intent, **started, 'observation': execution.observe_process(started.get('child', {}))}


def reconcile(root, job_id, now=None):
    return offload_recovery.reconcile(root, job_id, now=now)
