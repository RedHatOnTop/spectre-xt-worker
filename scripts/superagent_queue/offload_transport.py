"""Read only the recorded remote receipt from the existing approved Studio."""
import json
from pathlib import Path
import re
import subprocess

from . import execution, offload_receipt
from .model import MAX_JSON_BYTES, timestamp
from .settings import read_private

FIELDS = frozenset(('schema_version', 'job_id', 'command_sha256', 'studio', 'remote_dir', 'recorded_at'))
STUDIO = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
DIRECTORY = re.compile(r'[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*')


def location(directory, record, studio):
    value = read_private(directory / 'transport.json')
    if (not isinstance(value, dict) or set(value) != FIELDS or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or value.get('job_id') != record['job_id']
            or value.get('command_sha256') != record['command_sha256']
            or not isinstance(value.get('studio'), str) or not STUDIO.fullmatch(value['studio'])
            or value['studio'] != studio or not isinstance(value.get('remote_dir'), str)
            or not DIRECTORY.fullmatch(value['remote_dir'])
            or any(part in ('.', '..') for part in value['remote_dir'].split('/'))
            or value.get('recorded_at') is None):
        raise ValueError('offload transport does not match its recorded command and approved Studio')
    timestamp(value['recorded_at'])
    return value


def fetch(root, job_id, studio):
    record = offload_receipt.inspect(root, job_id)
    if record['observation'] != 'exited':
        raise ValueError('offload fetch requires the recorded actual wrapper exit')
    directory = Path(root).absolute() / job_id
    target = location(directory, record, studio)
    path = f'{target["remote_dir"]}/.spectre-offload/command-result.json'
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'LogLevel=ERROR',
               studio, f'head -c {MAX_JSON_BYTES + 1} -- "$HOME"/{path}']
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(f'offload receipt fetch exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}')
    if len(result.stdout.encode()) > MAX_JSON_BYTES:
        raise ValueError('remote offload receipt exceeds the byte limit')
    value = offload_receipt.remote(json.loads(result.stdout), job_id, record['command_sha256'])
    try:
        execution.persist(directory, 'command-result.json', value)
    except FileExistsError:
        if read_private(directory / 'command-result.json') != value:
            raise ValueError('fetched offload receipt conflicts with its original local result')
    proof = offload_receipt.reconcile(root, job_id)
    return {'remote_result': value, 'reconciliation': proof, 'remote_execution': False,
            'capacity_released': False, 'receipt_read': True}
