"""Recover returned bytes from an existing job; never replay execution or release capacity."""
import json
from pathlib import Path
import shlex
import subprocess
from uuid import uuid4

from . import execution, offload_artifact, offload_receipt, offload_transport, settings
from .model import encoded, identifier, MAX_JSON_BYTES


def query(command, **options):
    return subprocess.run(command, text=True, capture_output=True, check=False, timeout=300, **options)


def checked(result):
    if result.returncode:
        raise RuntimeError(f'artifact fetch exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}')
    return result


def fetch_manifest(record, target, spec, runner):
    words = ['--job', record['job_id'], '--sha256', record['command_sha256'],
             *[word for path in spec['artifacts'] for word in ('--artifact', path)]]
    arguments = 'capture --root "$HOME"/' + shlex.quote(target['remote_dir']) + ' ' + shlex.join(words)
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'LogLevel=ERROR',
               target['studio'], 'python3 - ' + arguments]
    result = checked(query(command, input=offload_artifact.HELPER.read_text()))
    if len(result.stdout.encode()) > MAX_JSON_BYTES:
        raise ValueError('remote artifact manifest exceeds the byte limit')
    value = json.loads(result.stdout)
    identity = runner.selection(record['job_id'], record['command_sha256'], spec['artifacts'])
    if (not isinstance(value, dict) or set(value) != set(identity) | {'entries'}
            or encoded({key: value.get(key) for key in identity}) != encoded(identity)
            or not isinstance(value['entries'], list) or len(value['entries']) > runner.MAX_ENTRIES):
        raise ValueError('remote artifact manifest differs from its original selection')
    encoded(value)
    return value


def transfer(record, target, spec, attempt):
    runner = offload_artifact.helper()
    artifact_root = runner.prepare(attempt, record['job_id'], record['command_sha256'], spec['artifacts'])
    value = fetch_manifest(record, target, spec, runner)
    runner.persist(artifact_root / 'manifest.json', value)
    for path in spec['artifacts']:
        parent = artifact_root / Path(path).parent
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        source = f'{target["studio"]}:~/{target["remote_dir"]}/{path}'
        command = ['rsync', '-az', '--protect-args', '--chmod=D700,F600',
                   '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o LogLevel=ERROR', source, str(parent) + '/']
        checked(query(command))
    runner.verify(artifact_root, record['job_id'], record['command_sha256'], spec['artifacts'])
    return artifact_root


def fetch(root, job_id, studio):
    identifier(job_id)
    directory = Path(root).absolute() / job_id
    with settings.lock(directory / 'artifact-recovery.lock'):
        return recover(root, job_id, studio)


def recover(root, job_id, studio):
    record = offload_receipt.inspect(root, job_id)
    if record['observation'] != 'exited':
        raise ValueError('artifact fetch requires the recorded actual wrapper exit')
    directory = Path(root).absolute() / job_id
    target = offload_transport.location(directory, record, studio)
    offload_artifact.remote_result(record)
    existing = offload_artifact.observe(root, record)
    if existing is not None:
        return {'artifact': existing, 'remote_execution': False, 'capacity_released': False}
    spec = offload_artifact.packet(record)
    recovery = execution.private_directory(directory / 'artifact-recovery')
    for previous in sorted(recovery.iterdir()):
        if (previous / job_id / 'verified.json').exists():
            proof = offload_artifact.capture(root, record, previous / job_id)
            return {'artifact': proof, 'remote_execution': False, 'capacity_released': False}
    attempt_id = 'recovery-' + str(uuid4())
    attempt = execution.private_directory(recovery / attempt_id)
    execution.persist(attempt, 'intent.json', {'schema_version': 1, 'attempt_id': attempt_id,
        'job_id': job_id, 'intent_id': record['intent_id'], 'command_sha256': record['command_sha256']})
    try:
        artifact_root = transfer(record, target, spec, attempt)
        proof = offload_artifact.capture(root, record, artifact_root)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        try:
            execution.persist(attempt, 'failure.json', {'error': str(exc), 'remote_execution': False,
                                                      'capacity_released': False})
        except (OSError, ValueError) as evidence_error:
            exc.add_note(f'artifact failure evidence could not be saved: {evidence_error}')
        raise
    return {'artifact': proof, 'remote_execution': False, 'capacity_released': False}
